"""Durable jobs around the existing CLI; no browser-supplied shell commands."""
from __future__ import annotations

import fcntl
import json
import mimetypes
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .models import now_iso
from .notifications import Notifications
from .store import WorkStore

AGENTS = ("codex", "opencode", "antigravity")
ACTIVE = {"queued", "running", "stopping"}
REPOSITORY_SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
REPOSITORY_SECRET_NAMES = {".env", "credentials.json", "secrets.json"}
REPOSITORY_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
REPOSITORY_FILE_LIMIT = 512 * 1024
REPOSITORY_ASSET_LIMIT = 10 * 1024 * 1024


class Conflict(ValueError):
    pass


def text_field(data, name, maximum=20000, required=False):
    value = data.get(name, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{name} の形式または長さが不正です")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} を入力してください")
    return value


class WebService:
    def __init__(self, work_dir: Path, agent="codex", push_subject=None):
        if agent not in AGENTS:
            raise ValueError("Web UIで使えるエージェントは codex / opencode / antigravity です")
        self.work_dir = work_dir.resolve()
        self.directory = self.work_dir / ".web"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.instance_lock = (self.directory / "service.lock").open("a")
        try:
            fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.instance_lock.close()
            raise Conflict("このwork-dirのWebサービスは既に起動しています") from error
        self.store = WorkStore(self.directory, "")
        self.default_agent = agent
        self.lock = threading.RLock()
        self.processes = {}
        self.cancels = {}
        self.threads = {}
        self.closing = False
        self.configs = self._read("settings.json", {})
        self.jobs = self._read("jobs.json", {})
        for job in self.jobs.values():
            if job["status"] in ACTIVE:
                job.update(status="interrupted", ended_at=now_iso(),
                           error="サービスの中断を検出しました。状態を確認して再開してください。")
        self._save_jobs()
        self.notifications = Notifications(self.directory, push_subject)
        self.notifications.start()

    def _read(self, path, default):
        try:
            return self.store.read_json(path)
        except FileNotFoundError:
            return default

    def _save_jobs(self):
        self.store.write_json("jobs.json", self.jobs)

    def close(self):
        with self.lock:
            self.closing = True
            for job in list(self.jobs.values()):
                if job["status"] in ACTIVE:
                    self._stop(job)
            threads = list(self.threads.values())
        for thread in threads:
            thread.join(timeout=7)
        self.notifications.close()
        self.instance_lock.close()

    def _work(self, initiative_id):
        if not initiative_id:
            raise ValueError("案件IDが必要です")
        return WorkStore(self.work_dir, initiative_id)

    def _config(self, initiative_id):
        return self.configs.get(initiative_id, {
            "agent": self.default_agent,
            "repository": str(self._work(initiative_id).path / "repo"),
        })

    def _repository_root(self, initiative_id):
        # detail() also verifies that the initiative belongs to this service.
        self.detail(initiative_id, include_evidence=False)
        root = Path(self._config(initiative_id)["repository"]).resolve()
        if not root.is_dir():
            raise FileNotFoundError("リポジトリが見つかりません")
        return root

    @staticmethod
    def _repository_visible(relative):
        parts = Path(relative).parts
        if any(part in REPOSITORY_SKIP_DIRS for part in parts):
            return False
        name = parts[-1].lower() if parts else ""
        if name in REPOSITORY_SECRET_NAMES or name.startswith(".env."):
            return False
        return Path(name).suffix not in REPOSITORY_SECRET_SUFFIXES

    def _repository_path(self, initiative_id, relative):
        if not isinstance(relative, str) or not relative or len(relative) > 4096:
            raise ValueError("ファイルのパスが不正です")
        root = self._repository_root(initiative_id)
        path = (root / relative).resolve()
        if root not in path.parents or not self._repository_visible(path.relative_to(root)):
            raise ValueError("このファイルは表示できません")
        if not path.is_file():
            raise FileNotFoundError("ファイルが見つかりません")
        return root, path

    def repository(self, initiative_id):
        root = self._repository_root(initiative_id)
        files = []
        truncated = False
        for directory, names, filenames in os.walk(root):
            names[:] = sorted(name for name in names if name not in REPOSITORY_SKIP_DIRS)
            for name in sorted(filenames):
                path = Path(directory) / name
                relative = path.relative_to(root)
                if not self._repository_visible(relative) or path.is_symlink():
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                files.append({"path": relative.as_posix(), "size": size,
                              "html": path.suffix.lower() in (".html", ".htm")})
                if len(files) >= 4000:
                    truncated = True
                    break
            if truncated:
                break
        return {"repository": str(root), "files": files, "truncated": truncated}

    def repository_file(self, initiative_id, relative):
        _, path = self._repository_path(initiative_id, relative)
        size = path.stat().st_size
        with path.open("rb") as stream:
            raw = stream.read(REPOSITORY_FILE_LIMIT + 1)
        if b"\0" in raw:
            raise ValueError("バイナリファイルはソース表示できません")
        try:
            content = raw[:REPOSITORY_FILE_LIMIT].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("UTF-8のテキストファイルだけ表示できます") from error
        return {"path": relative, "content": content, "size": size,
                "truncated": len(raw) > REPOSITORY_FILE_LIMIT,
                "language": path.suffix.lower().removeprefix(".") or "text",
                "html": path.suffix.lower() in (".html", ".htm")}

    def repository_asset(self, initiative_id, relative):
        _, path = self._repository_path(initiative_id, relative)
        if path.stat().st_size > REPOSITORY_ASSET_LIMIT:
            raise ValueError("プレビュー用ファイルが大きすぎます")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return path.read_bytes(), mime

    def _latest(self, initiative_id):
        jobs = [j for j in self.jobs.values() if j["initiative_id"] == initiative_id]
        return jobs[-1].copy() if jobs else None

    def _idle(self, initiative_id, repository=None):
        if self.closing:
            raise Conflict("サービスを停止しています")
        for job in self.jobs.values():
            if job["status"] in ACTIVE and (job["initiative_id"] == initiative_id
                    or repository and job["repository"] == repository):
                raise Conflict("この案件またはリポジトリは既に実行中です")

    def _duplicate(self, initiative_id, request_id):
        for job in self.jobs.values():
            if job["initiative_id"] == initiative_id and job["request_id"] == request_id:
                return job.copy()

    def configure(self, initiative_id, data):
        with self.lock:
            self.detail(initiative_id)
            self._idle(initiative_id)
            config = self._config(initiative_id).copy()
            agent = text_field(data, "agent", 30, True)
            if agent not in AGENTS:
                raise ValueError("対応していないエージェントです")
            repository = text_field(data, "repository", 4096, True)
            path = Path(repository).expanduser().resolve()
            if not path.is_dir():
                raise ValueError("リポジトリのディレクトリが存在しません")
            config.update(agent=agent, repository=str(path))
            self.configs[initiative_id] = config
            self.store.write_json("settings.json", self.configs)
            return config

    def create(self, data):
        with self.lock:
            request_id = text_field(data, "request_id", 100, True)
            # A retried create must return the original job, including its generated ID.
            for job in self.jobs.values():
                if job["action"] == "init" and job["request_id"] == request_id:
                    return job.copy()
            initiative_id = text_field(data, "id", 128) or "INIT-" + uuid.uuid4().hex[:12]
            work = self._work(initiative_id)
            if initiative_id in self.configs or (work.path / "initiative.json").exists():
                raise Conflict("この案件IDは既に使われています")
            agent = text_field(data, "agent", 30) or self.default_agent
            if agent not in AGENTS:
                raise ValueError("対応していないエージェントです")
            repository = text_field(data, "repository", 4096)
            if repository:
                repo = Path(repository).expanduser().resolve()
                if not repo.is_dir():
                    raise ValueError("既存リポジトリのディレクトリが存在しません")
            else:
                repo = work.path / "repo"
            config = {"request": text_field(data, "request", required=True),
                      "requirements": text_field(data, "requirements"),
                      "constraints": text_field(data, "constraints"),
                      "agent": agent, "repository": str(repo), "create_repository": not repository}
            self._idle(initiative_id, str(repo))
            self.configs[initiative_id] = config
            self.store.write_json("settings.json", self.configs)
            return self._enqueue(initiative_id, "init", request_id, config, [])

    def act(self, initiative_id, action, data):
        with self.lock:
            request_id = text_field(data, "request_id", 100, True)
            duplicate = self._duplicate(initiative_id, request_id)
            if duplicate:
                return duplicate
            detail = self.detail(initiative_id)
            config = self._config(initiative_id).copy()
            self._idle(initiative_id, config["repository"])
            state = detail["state"]
            extra = []
            if action == "human-check":
                revision = text_field(data, "revision", 64, True)
                if state["state"] != "AWAITING_HUMAN_CHECK" or revision != detail["revision"]:
                    raise Conflict("確認後に状態が更新されています。最新の結果を確認してください")
                result = text_field(data, "result", 16, True)
                if result not in ("ACCEPT", "FEEDBACK"):
                    raise ValueError("承認または差し戻しを選んでください")
                extra = [result, "--expected-revision", revision]
                if result == "FEEDBACK":
                    extra += ["--feedback", text_field(data, "feedback", required=True)]
            elif action == "run":
                if state["state"] in ("AWAITING_HUMAN_CHECK", "COMPLETED"):
                    raise Conflict("この状態では実行できません。最新の状態を確認してください")
                if not (self._work(initiative_id).path / "state.json").exists():
                    if not config.get("request"):
                        raise ValueError("案件の初期化情報がありません")
                    action = "init"
            else:
                raise ValueError("不明な操作です")
            return self._enqueue(initiative_id, action, request_id, config, extra)

    def _enqueue(self, initiative_id, action, request_id, config, extra):
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "initiative_id": initiative_id, "action": action,
               "request_id": request_id, "status": "queued", "created_at": now_iso(),
               "repository": config["repository"], "error": None, "returncode": None}
        self.jobs[job_id] = job
        self.cancels[job_id] = threading.Event()
        self._save_jobs()
        thread = threading.Thread(target=self._execute, args=(job_id, config.copy(), extra),
                                  daemon=True, name=f"pipeline-{initiative_id}")
        self.threads[job_id] = thread
        thread.start()
        return job.copy()

    def _base_command(self, job, config):
        return [sys.executable, "-m", "dev_pipeline.cli", "--work-dir", str(self.work_dir),
                "--repo", config["repository"], "--agent", config["agent"]]

    def _process(self, job_id, command, output):
        with self.lock:
            if self.cancels[job_id].is_set():
                return -signal.SIGTERM
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                       cwd=Path(__file__).resolve().parent.parent,
                                       start_new_session=True)
            self.processes[job_id] = process
        stopping_at = None
        while process.poll() is None:
            if self.cancels[job_id].is_set():
                if stopping_at is None:
                    self._signal(process, signal.SIGTERM)
                    stopping_at = time.monotonic()
                elif time.monotonic() - stopping_at > 3:
                    self._signal(process, signal.SIGKILL)
            time.sleep(0.1)
        if self.cancels[job_id].is_set():
            # The CLI can exit before a descendant that ignores SIGTERM. Kill the
            # remaining members of our own process group before releasing the job.
            self._signal(process, signal.SIGTERM)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            self._signal(process, signal.SIGKILL)
        with self.lock:
            self.processes.pop(job_id, None)
        return process.returncode

    @staticmethod
    def _signal(process, sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def _execute(self, job_id, config, extra):
        job = self.jobs[job_id]
        initiative_id = job["initiative_id"]
        log = self.directory / "job-logs" / f"{job_id}.log"
        log.parent.mkdir(exist_ok=True)
        code = None
        error = None
        try:
            with self.lock:
                job.update(status="stopping" if self.cancels[job_id].is_set() else "running", started_at=now_iso())
                self._save_jobs()
            with log.open("w", encoding="utf-8") as output:
                base = self._base_command(job, config)
                action = job["action"]
                if action == "init":
                    if config.get("create_repository"):
                        repo = Path(config["repository"])
                        repo.mkdir(parents=True, exist_ok=True)
                        if not (repo / ".git").exists():
                            code = self._process(job_id, ["git", "init", str(repo)], output)
                            if code:
                                raise ValueError("新規リポジトリのGit初期化に失敗しました")
                    command = base + ["init", "--id", initiative_id, "--request", config["request"]]
                    for key, option in (("requirements", "--requirement"), ("constraints", "--constraint")):
                        for line in config.get(key, "").splitlines():
                            if line.strip():
                                command += [option, line.strip()]
                elif action == "human-check":
                    command = base + ["human-check", initiative_id] + extra
                else:
                    command = base + ["run", initiative_id, "--until-ready"]
                code = self._process(job_id, command, output)
                if code == 0 and action == "human-check" and not self.cancels[job_id].is_set():
                    state = self._work(initiative_id).load_state()
                    if state.state != "COMPLETED":
                        code = self._process(job_id, base + ["run", initiative_id, "--until-ready"], output)
        except Exception as exc:
            error = str(exc)
        with self.lock:
            stopped = self.cancels[job_id].is_set()
            status = "stopped" if stopped else "succeeded" if code == 0 and not error else "failed"
            job.update(status=status, ended_at=now_iso(), returncode=code,
                       error=error or ("実行に失敗しました。実行ログを確認してください。" if status == "failed" else None))
            self._save_jobs()
            self.cancels.pop(job_id, None)
            self.threads.pop(job_id, None)
        if status == "failed":
            self.notifications.notify("実行エラー", f"{initiative_id} の実行が停止しました。", initiative_id)
        elif status == "stopped":
            self.notifications.notify("実行を停止しました", f"{initiative_id} は保存済み状態から再開できます。", initiative_id)
        else:
            try:
                state = self._work(initiative_id).load_state()
                title = "承認待ち" if state.state == "AWAITING_HUMAN_CHECK" else "案件が完了しました"
                self.notifications.notify(title, f"{initiative_id} / {state.current_phase or '完了'}", initiative_id)
            except (ValueError, OSError):
                pass

    def _stop(self, job):
        if job["status"] not in ACTIVE:
            raise Conflict("現在実行中のジョブはありません")
        self.cancels[job["id"]].set()
        job["status"] = "stopping"
        process = self.processes.get(job["id"])
        if process:
            self._signal(process, signal.SIGTERM)
        self._save_jobs()
        return job.copy()

    def stop(self, initiative_id):
        with self.lock:
            job = self._latest(initiative_id)
            if not job:
                raise Conflict("現在実行中のジョブはありません")
            return self._stop(self.jobs[job["id"]])

    @staticmethod
    def _text(path, limit=100000):
        try:
            with path.open("rb") as stream:
                size = path.stat().st_size
                if size > limit:
                    stream.seek(-limit, 2)
                content = stream.read(limit).decode("utf-8", errors="replace")
            return ("［末尾100KBを表示］\n" if size > limit else "") + content
        except FileNotFoundError:
            return ""

    def detail(self, initiative_id, include_evidence=True):
        with self.lock:
            work = self._work(initiative_id)
            try:
                state = work.load_state()
                initiative = work.load_initiative()
                phases = [work.load_phase(p).as_dict() for p in initiative.phase_order]
                result = {"initiative": initiative.as_dict(), "state": state.as_dict(),
                          "revision": state.revision(), "phases": phases,
                          "usage": work.load_token_usage()}
            except FileNotFoundError:
                config = self.configs.get(initiative_id)
                if not config:
                    raise FileNotFoundError("案件が見つかりません")
                result = {"initiative": {"id": initiative_id, "goal": config.get("request", "初期化中")},
                          "state": {"state": "PLANNING", "current_phase": None, "current_run": 0},
                          "revision": None, "phases": [], "usage": {}}
            job = self._latest(initiative_id)
            if not include_evidence:
                return {**result, "job": job}
            result.update(job=job, config=self._config(initiative_id), evidence=[], checkpoint="",
                          job_log=self._text(self.directory / "job-logs" / f"{job['id']}.log") if job else "")
            for phase in result["phases"]:
                evidence = {"phase_id": phase["id"], "verification": None, "review": None,
                            "verification_log": ""}
                for key, suffix in (("verification", "verification"), ("review", "adversarial-review")):
                    try:
                        evidence[key] = work.read_json(f"evidence/{phase['id']}-{suffix}.json")
                    except FileNotFoundError:
                        pass
                evidence["verification_log"] = self._text(work.path / "evidence" / f"{phase['id']}-verification.log")
                result["evidence"].append(evidence)
            phase_id = result["state"].get("current_phase")
            if phase_id:
                checkpoints = sorted((work.path / "runs").glob(f"{phase_id}-run-*.md"),
                                     key=lambda p: int(p.stem.rsplit("-", 1)[1]))
                if checkpoints:
                    result["checkpoint"] = self._text(checkpoints[-1])
            return result

    def list_initiatives(self):
        with self.lock:
            ids = set(self.configs)
            ids.update(p.name for p in self.work_dir.iterdir()
                       if p.is_dir() and not p.name.startswith(".") and (p / "initiative.json").exists())
            result = []
            for initiative_id in sorted(ids):
                try:
                    detail = self.detail(initiative_id, include_evidence=False)
                    result.append({k: detail[k] for k in ("initiative", "state", "job", "phases")})
                except (OSError, ValueError, TypeError) as error:
                    result.append({"initiative": {"id": initiative_id, "goal": "状態の読み取りエラー"},
                                   "state": {"state": "FAILED"}, "job": None, "phases": [], "error": str(error)})
            return result
