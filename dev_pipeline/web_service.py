"""Durable jobs around the existing CLI; no browser-supplied shell commands."""
from __future__ import annotations

import fcntl
import json
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
from .reporting import (
    available_actions,
    default_repository,
    initiative_exists,
    list_initiative_ids,
    read_pipeline,
)
from .store import JsonStore

AGENTS = ("codex", "opencode", "antigravity")
ACTIVE = {"queued", "running", "stopping"}


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
        self.store = JsonStore(self.directory)
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

    def _config(self, initiative_id):
        return self.configs.get(initiative_id, {
            "agent": self.default_agent,
            "repository": str(default_repository(self.work_dir, initiative_id)),
        })

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
            if initiative_id in self.configs or initiative_exists(self.work_dir, initiative_id):
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
                repo = default_repository(self.work_dir, initiative_id)
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
            extra = []
            if action == "human-check":
                revision = text_field(data, "revision", 64, True)
                if not detail["actions"]["human_check"] or revision != detail["revision"]:
                    raise Conflict("確認後に状態が更新されています。最新の結果を確認してください")
                result = text_field(data, "result", 16, True)
                if result not in ("ACCEPT", "FEEDBACK"):
                    raise ValueError("承認または差し戻しを選んでください")
                extra = [result, "--expected-revision", revision]
                if result == "FEEDBACK":
                    extra += ["--feedback", text_field(data, "feedback", required=True)]
            elif action == "run":
                if not detail["actions"]["run"]:
                    raise Conflict("この状態では実行できません。最新の状態を確認してください")
                if not initiative_exists(self.work_dir, initiative_id):
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
                    pipeline = read_pipeline(self.work_dir, initiative_id, include_evidence=False)
                    if pipeline["actions"]["run"]:
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
                pipeline = read_pipeline(self.work_dir, initiative_id, include_evidence=False)
                title = "承認待ち" if pipeline["actions"]["human_check"] else "案件が完了しました"
                phase = pipeline["state"]["current_phase"] or "完了"
                self.notifications.notify(title, f"{initiative_id} / {phase}", initiative_id)
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
    def _job_log(path, limit=100000):
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
            try:
                result = read_pipeline(self.work_dir, initiative_id, include_evidence)
            except FileNotFoundError:
                config = self.configs.get(initiative_id)
                if not config:
                    raise FileNotFoundError("案件が見つかりません")
                result = {"initiative": {"id": initiative_id, "goal": config.get("request", "初期化中")},
                          "state": {"state": "PLANNING", "current_phase": None, "current_run": 0},
                          "revision": None, "phases": [], "usage": {},
                          "actions": available_actions("PLANNING")}
                if include_evidence:
                    result.update(evidence=[], checkpoint="")
            job = self._latest(initiative_id)
            if not include_evidence:
                return {**result, "job": job}
            result.update(job=job, config=self._config(initiative_id),
                          job_log=self._job_log(
                              self.directory / "job-logs" / f"{job['id']}.log") if job else "")
            return result

    def list_initiatives(self):
        with self.lock:
            ids = set(self.configs)
            ids.update(list_initiative_ids(self.work_dir))
            result = []
            for initiative_id in sorted(ids):
                try:
                    detail = self.detail(initiative_id, include_evidence=False)
                    result.append({k: detail[k] for k in
                                   ("initiative", "state", "actions", "job", "phases")})
                except (OSError, ValueError, TypeError) as error:
                    result.append({"initiative": {"id": initiative_id, "goal": "状態の読み取りエラー"},
                                   "state": {"state": "FAILED"},
                                   "actions": {"run": False, "human_check": False, "completed": False},
                                   "job": None, "phases": [], "error": str(error)})
            return result
