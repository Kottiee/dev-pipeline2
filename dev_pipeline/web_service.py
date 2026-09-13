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
    issue_exists,
    list_initiative_ids,
    list_issue_ids,
    list_project_ids,
    project_exists,
    read_changelog,
    read_issue,
    read_pipeline,
    read_project,
)
from .store import JsonStore, ProjectStore
from .models import Project

AGENTS = ("codex", "opencode", "antigravity")
ACTIVE = {"queued", "running", "stopping"}


class Conflict(ValueError):
    pass


def text_field(data, name, maximum=100000, required=False):
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
        for job_id, job in list(self.jobs.items()):
            if job.get("status") in ACTIVE:
                pid = job.get("pid")
                if pid and self._is_pid_alive(pid):
                    self.cancels[job_id] = threading.Event()
                    if job.get("status") == "stopping":
                        self.cancels[job_id].set()
                    thread = threading.Thread(
                        target=self._monitor_detached,
                        args=(job_id, pid),
                        daemon=True,
                        name=f"detached-pipeline-{job.get('initiative_id')}"
                    )
                    self.threads[job_id] = thread
                    thread.start()
                else:
                    self._finalize_terminated_job(job)
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
            threads = list(self.threads.values())
        for thread in threads:
            thread.join(timeout=1.0)
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
               "repository": config["repository"], "error": None, "returncode": None,
               "pid": None}
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
            if job_id in self.jobs:
                self.jobs[job_id]["pid"] = process.pid
                self._save_jobs()
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
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                pass

    @staticmethod
    def _signal_pid(pid: int, sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    @staticmethod
    def _is_pid_alive(pid: int | None) -> bool:
        if not pid or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

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
                project_id = job.get("project_id")
                issue_id = job.get("issue_id")
                if project_id and issue_id:
                    if action == "init":
                        command = base + ["issue", "create", project_id, "--id", issue_id, "--request", config["request"]]
                    elif action == "human-check":
                        command = base + ["issue", "human-check", project_id, issue_id] + extra
                    else:
                        command = base + ["issue", "run", project_id, issue_id, "--until-ready"]
                elif action == "init":
                    if config.get("create_repository"):
                        repo = Path(config["repository"])
                        repo.mkdir(parents=True, exist_ok=True)
                        if not (repo / ".git").exists():
                            code = self._process(job_id, ["git", "init", str(repo)], output)
                            if code:
                                raise ValueError("新規リポジトリのGit初期化に失敗しました")
                    command = base + ["init", "--id", initiative_id, "--request", config["request"]]
                elif action == "human-check":
                    command = base + ["human-check", initiative_id] + extra
                else:
                    command = base + ["run", initiative_id, "--until-ready"]
                code = self._process(job_id, command, output)
                if code == 0 and action == "human-check" and not self.cancels[job_id].is_set():
                    if project_id and issue_id:
                        p_info = read_issue(self.work_dir, project_id, issue_id, include_evidence=False)
                        if p_info["actions"]["run"]:
                            code = self._process(job_id, base + ["issue", "run", project_id, issue_id, "--until-ready"], output)
                    else:
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
        job_id = job["id"]
        if job_id not in self.cancels:
            self.cancels[job_id] = threading.Event()
        self.cancels[job_id].set()
        job["status"] = "stopping"
        process = self.processes.get(job_id)
        if process:
            self._signal(process, signal.SIGTERM)
        elif job.get("pid"):
            self._signal_pid(job["pid"], signal.SIGTERM)
        self._save_jobs()
        return job.copy()

    def _monitor_detached(self, job_id, pid):
        stopping_at = None
        while self._is_pid_alive(pid):
            if self.cancels.get(job_id) and self.cancels[job_id].is_set():
                if stopping_at is None:
                    self._signal_pid(pid, signal.SIGTERM)
                    stopping_at = time.monotonic()
                elif time.monotonic() - stopping_at > 3:
                    self._signal_pid(pid, signal.SIGKILL)
            time.sleep(0.5)
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job.get("status") in ACTIVE:
                self._finalize_terminated_job(job)
                self._save_jobs()
            self.cancels.pop(job_id, None)
            self.threads.pop(job_id, None)

    def _finalize_terminated_job(self, job):
        job_id = job["id"]
        initiative_id = job.get("initiative_id", "")
        pid = job.get("pid")
        if not pid and job.get("status") == "queued":
            job.update(status="interrupted", ended_at=now_iso(),
                       error="サービスの中断を検出しました。状態を確認して再開してください。")
            return

        stopped = (job_id in self.cancels and self.cancels[job_id].is_set()) or job.get("status") == "stopping"
        if stopped:
            job.update(status="stopped", ended_at=now_iso(), returncode=-signal.SIGTERM, error=None)
            self.notifications.notify("実行を停止しました", f"{initiative_id} は保存済み状態から再開できます。", initiative_id)
            return

        log_file = self.directory / "job-logs" / f"{job_id}.log"
        log_text = ""
        if log_file.exists():
            try:
                log_text = log_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        lines = [line.strip() for line in log_text.strip().splitlines() if line.strip()]
        last_line = lines[-1] if lines else ""

        project_id = job.get("project_id")
        issue_id = job.get("issue_id")
        succeeded = (last_line == "READY")
        if not succeeded and project_id and issue_id:
            try:
                p_info = read_issue(self.work_dir, project_id, issue_id, include_evidence=False)
                state = p_info.get("state", {}).get("state")
                if state in ("AWAITING_HUMAN_CHECK", "RESOLVED", "COMPLETED"):
                    succeeded = True
            except Exception:
                pass
        elif not succeeded and initiative_id:
            try:
                pipeline = read_pipeline(self.work_dir, initiative_id, include_evidence=False)
                state = pipeline.get("state", {}).get("state")
                if state in ("AWAITING_HUMAN_CHECK", "RESOLVED", "COMPLETED"):
                    succeeded = True
            except Exception:
                pass

        if succeeded:
            job.update(status="succeeded", ended_at=now_iso(), returncode=0, error=None)
            try:
                if project_id and issue_id:
                    detail = read_issue(self.work_dir, project_id, issue_id, include_evidence=False)
                else:
                    detail = read_pipeline(self.work_dir, initiative_id, include_evidence=False)
                title = "承認待ち" if detail["actions"]["human_check"] else "案件が完了しました"
                phase = detail["state"]["current_phase"] or "完了"
                self.notifications.notify(title, f"{initiative_id} / {phase}", initiative_id)
            except Exception:
                pass
        else:
            job.update(status="failed", ended_at=now_iso(), returncode=1,
                       error="実行に失敗しました。実行ログを確認してください。")
            self.notifications.notify("実行エラー", f"{initiative_id} の実行が停止しました。", initiative_id)

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
                result = {"initiative": {"id": initiative_id, "description": config.get("request", "初期化中")},
                          "state": {"state": "PLANNING", "current_phase": None, "current_run": 0},
                          "revision": None, "phases": [], "usage": {},
                          "allocation_usage": {}, "actions": available_actions("PLANNING")}
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
                    result.append({"initiative": {"id": initiative_id, "description": "状態の読み取りエラー"},
                                   "state": {"state": "FAILED"},
                                   "actions": {"run": False, "human_check": False, "completed": False},
                                   "job": None, "phases": [], "error": str(error)})
            return result

    def list_projects(self):
        with self.lock:
            ids = set(list_project_ids(self.work_dir))
            for cfg_id, cfg in self.configs.items():
                if cfg.get("is_project") or ProjectStore(self.work_dir, cfg_id).exists():
                    ids.add(cfg_id)
            projects = []
            for pid in sorted(ids):
                p_store = ProjectStore(self.work_dir, pid)
                if p_store.exists():
                    try:
                        proj = p_store.load_project()
                        repo = str(p_store.repository_path())
                        agent = proj.agent
                    except Exception:
                        repo = str(p_store.repository_path())
                        agent = self.default_agent
                else:
                    repo = str(p_store.repository_path())
                    agent = self.default_agent
                issue_ids = p_store.list_issue_ids()
                active_count = 0
                waiting_count = 0
                for i_id in issue_ids:
                    job = self._latest(f"{pid}/{i_id}") or self._latest(i_id)
                    if job and job["status"] in ACTIVE:
                        active_count += 1
                    try:
                        det = read_issue(self.work_dir, pid, i_id, include_evidence=False)
                        if det["actions"]["human_check"]:
                            waiting_count += 1
                    except Exception:
                        pass
                projects.append({
                    "id": pid,
                    "repository": repo,
                    "agent": agent,
                    "issues_count": len(issue_ids),
                    "active_issues_count": active_count,
                    "waiting_issues_count": waiting_count,
                })
            return projects

    def create_project(self, data):
        with self.lock:
            project_id = text_field(data, "id", 128, required=True)
            p_store = ProjectStore(self.work_dir, project_id)
            if p_store.exists():
                raise Conflict("このプロジェクトIDは既に使われています")
            agent = text_field(data, "agent", 30) or self.default_agent
            if agent not in AGENTS:
                raise ValueError("対応していないエージェントです")
            repository = text_field(data, "repository", 4096)
            if repository:
                repo = Path(repository).expanduser().resolve()
                if not repo.is_dir():
                    raise ValueError("指定されたリポジトリのディレクトリが存在しません")
                repo_path = str(repo)
            else:
                default_repo = p_store.path / "repo"
                default_repo.mkdir(parents=True, exist_ok=True)
                if not (default_repo / ".git").exists():
                    subprocess.run(["git", "init", str(default_repo)], check=False)
                repo_path = str(default_repo)
            project = Project(id=project_id, repo=repo_path, agent=agent)
            p_store.create(project)
            self.configs[project_id] = {"agent": agent, "repository": repo_path, "is_project": True}
            self.store.write_json("settings.json", self.configs)
            return project.as_dict()

    def project_detail(self, project_id):
        with self.lock:
            p_store = ProjectStore(self.work_dir, project_id)
            if not p_store.exists():
                raise FileNotFoundError("プロジェクトが見つかりません")
            proj = p_store.load_project()
            issues = self.list_issues(project_id)
            return {
                "project": proj.as_dict(),
                "repository": str(p_store.repository_path()),
                "issues": issues,
            }

    def project_changelog(self, project_id):
        with self.lock:
            p_store = ProjectStore(self.work_dir, project_id)
            if not p_store.exists():
                raise FileNotFoundError("プロジェクトが見つかりません")
            return read_changelog(p_store.repository_path())

    def list_issues(self, project_id):
        with self.lock:
            p_store = ProjectStore(self.work_dir, project_id)
            issue_ids = p_store.list_issue_ids()
            results = []
            for i_id in issue_ids:
                try:
                    detail = self.issue_detail(project_id, i_id, include_evidence=False)
                    results.append({k: detail[k] for k in
                                    ("issue", "state", "actions", "job", "phases", "project_id")})
                except Exception as error:
                    results.append({
                        "issue": {"id": i_id, "description": "状態の読み取りエラー"},
                        "state": {"state": "FAILED"},
                        "actions": {"run": False, "human_check": False, "completed": False},
                        "job": None, "phases": [], "project_id": project_id, "error": str(error)
                    })
            return results

    def issue_detail(self, project_id, issue_id, include_evidence=True):
        with self.lock:
            p_store = ProjectStore(self.work_dir, project_id)
            resolved_id = p_store.resolve_issue_id(issue_id)
            try:
                result = read_issue(self.work_dir, project_id, resolved_id, include_evidence)
            except FileNotFoundError:
                key = f"{project_id}/{issue_id}"
                config = self.configs.get(key, {})
                result = {
                    "project_id": project_id,
                    "issue": {"id": issue_id, "description": config.get("request", "初期化中")},
                    "state": {"state": "PLANNING", "current_phase": None, "current_run": 0},
                    "revision": None, "phases": [], "usage": {},
                    "allocation_usage": {}, "actions": available_actions("PLANNING")
                }
                if include_evidence:
                    result.update(evidence=[], checkpoint="")
            job = self._latest(f"{project_id}/{resolved_id}") or self._latest(resolved_id)
            if not include_evidence:
                return {**result, "job": job}
            p_config = self._config(project_id)
            result.update(job=job, config=p_config,
                          job_log=self._job_log(self.directory / "job-logs" / f"{job['id']}.log") if job else "")
            return result

    def create_issue(self, project_id, data):
        with self.lock:
            request_id = text_field(data, "request_id", 100, True)
            p_store = ProjectStore(self.work_dir, project_id)
            if not p_store.exists():
                raise FileNotFoundError("プロジェクトが見つかりません")
            issue_id = text_field(data, "id", 128) or p_store.next_issue_id(str(data.get("request") or "")[:50])
            full_key = f"{project_id}/{issue_id}"
            for job in self.jobs.values():
                if job.get("project_id") == project_id and job.get("issue_id") == issue_id and job["request_id"] == request_id:
                    return job.copy()
            p_config = self._config(project_id)
            repo = Path(p_config["repository"])
            config = {
                "request": text_field(data, "request", required=True),
                "agent": p_config["agent"],
                "repository": str(repo),
                "project_id": project_id,
                "issue_id": issue_id,
            }
            self._idle(full_key, str(repo))
            return self._enqueue_issue(project_id, issue_id, "init", request_id, config, [])

    def act_issue(self, project_id, issue_id, action, data):
        with self.lock:
            request_id = text_field(data, "request_id", 100, True)
            full_key = f"{project_id}/{issue_id}"
            for job in self.jobs.values():
                if job.get("project_id") == project_id and job.get("issue_id") == issue_id and job["request_id"] == request_id:
                    return job.copy()
            detail = self.issue_detail(project_id, issue_id)
            p_config = self._config(project_id)
            self._idle(full_key, p_config["repository"])
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
            elif action == "stop":
                return self.stop_issue(project_id, issue_id)
            else:
                raise ValueError("不明な操作です")
            config = {
                "agent": p_config["agent"],
                "repository": p_config["repository"],
                "project_id": project_id,
                "issue_id": issue_id,
            }
            return self._enqueue_issue(project_id, issue_id, action, request_id, config, extra)

    def configure_issue(self, project_id, issue_id, data):
        with self.lock:
            self.issue_detail(project_id, issue_id)
            full_key = f"{project_id}/{issue_id}"
            self._idle(full_key)
            agent = text_field(data, "agent", 30, True)
            if agent not in AGENTS:
                raise ValueError("対応していないエージェントです")
            repository = text_field(data, "repository", 4096, True)
            path = Path(repository).expanduser().resolve()
            if not path.is_dir():
                raise ValueError("リポジトリのディレクトリが存在しません")
            p_store = ProjectStore(self.work_dir, project_id)
            if p_store.exists():
                proj = p_store.load_project()
                proj.agent = agent
                p_store.save_project(proj)
            config = self._config(project_id).copy()
            config.update(agent=agent, repository=str(path))
            self.configs[project_id] = config
            self.store.write_json("settings.json", self.configs)
            return config

    def stop_issue(self, project_id, issue_id):
        with self.lock:
            job = self._latest(f"{project_id}/{issue_id}")
            if not job:
                raise Conflict("現在実行中のジョブはありません")
            return self._stop(self.jobs[job["id"]])

    def _enqueue_issue(self, project_id, issue_id, action, request_id, config, extra):
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "initiative_id": f"{project_id}/{issue_id}",
               "project_id": project_id, "issue_id": issue_id, "action": action,
               "request_id": request_id, "status": "queued", "created_at": now_iso(),
               "repository": config["repository"], "error": None, "returncode": None,
               "pid": None}
        self.jobs[job_id] = job
        self.cancels[job_id] = threading.Event()
        self._save_jobs()
        thread = threading.Thread(target=self._execute, args=(job_id, config.copy(), extra),
                                  daemon=True, name=f"pipeline-{project_id}-{issue_id}")
        self.threads[job_id] = thread
        thread.start()
        return job.copy()
