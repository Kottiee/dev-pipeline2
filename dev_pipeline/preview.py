"""Lifecycle management for repository-declared Web previews."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .models import now_iso


CONFIG_NAME = ".dev-pipeline-preview.json"
ACTIVE_PREVIEW = {"starting", "running", "stopping"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PreviewConflict(ValueError):
    pass


def _tail(path: Path, limit: int = 100_000) -> str:
    try:
        with path.open("rb") as stream:
            size = path.stat().st_size
            if size > limit:
                stream.seek(-limit, 2)
            content = stream.read(limit).decode("utf-8", errors="replace")
        return ("［末尾100KBを表示］\n" if size > limit else "") + content
    except FileNotFoundError:
        return ""


def read_preview_config(repository: Path) -> dict | None:
    """Read and validate the one supported, repository-local declaration."""
    path = repository / CONFIG_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{CONFIG_NAME} が正しいJSONではありません") from error
    if not isinstance(value, dict):
        raise ValueError(f"{CONFIG_NAME} にはJSONオブジェクトを指定してください")
    if value.get("type") != "web":
        raise ValueError("プレビューの type は web のみ対応しています")
    command = value.get("command")
    if (not isinstance(command, list) or not command or len(command) > 64
            or any(not isinstance(part, str) or not part or len(part) > 4096 for part in command)):
        raise ValueError("プレビューの command は空でない文字列の配列で指定してください")
    health_path = value.get("health_path", "/")
    parsed_health = urlsplit(health_path) if isinstance(health_path, str) else None
    if (not parsed_health or not health_path.startswith("/") or health_path.startswith("//")
            or parsed_health.scheme or parsed_health.netloc or parsed_health.fragment
            or len(health_path) > 2048):
        raise ValueError("プレビューの health_path は / で始まるローカルパスにしてください")
    port = value.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535):
        raise ValueError("プレビューの port は1024から65535の整数にしてください")
    port_env = value.get("port_env", "PORT")
    if not isinstance(port_env, str) or not _ENV_NAME.fullmatch(port_env):
        raise ValueError("プレビューの port_env は正しい環境変数名にしてください")
    timeout = value.get("startup_timeout", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        raise ValueError("プレビューの startup_timeout は1から120秒にしてください")
    return {
        "type": "web",
        "command": command,
        "health_path": health_path,
        "port": port,
        "port_env": port_env,
        "startup_timeout": float(timeout),
    }


class PreviewManager:
    def __init__(self, directory: Path):
        self.directory = directory
        self.logs = directory / "preview-logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.runs: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}
        self.ports: set[int] = set()
        self.closing = False

    def _log_path(self, key: str) -> Path:
        name = hashlib.sha256(key.encode()).hexdigest()[:24]
        return self.logs / f"{name}.log"

    def _choose_port(self, requested: int | None) -> int:
        if requested in self.ports:
            raise PreviewConflict("指定されたプレビューポートは別の案件が使用中です")
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", requested or 0))
            except OSError as error:
                raise PreviewConflict("指定されたプレビューポートは使用できません") from error
            port = sock.getsockname()[1]
        if port in self.ports:
            raise PreviewConflict("プレビューポートを確保できませんでした")
        self.ports.add(port)
        return port

    def status(self, key: str, repository: Path) -> dict:
        repository = repository.resolve()
        with self.lock:
            run = self.runs.get(key)
            try:
                config = read_preview_config(repository)
                config_error = None
            except ValueError as error:
                config = None
                config_error = str(error)
            if run and run["status"] in ACTIVE_PREVIEW:
                result = {name: run.get(name) for name in (
                    "status", "type", "url", "port", "error", "started_at", "ended_at")}
                result.update(configured=True, config_file=CONFIG_NAME,
                              log=_tail(self._log_path(key)))
                return result
            if config_error:
                return {"configured": True, "status": "failed", "type": None, "url": None,
                        "port": None, "error": config_error, "config_file": CONFIG_NAME, "log": ""}
            if not config:
                return {"configured": False, "status": "unsupported", "type": None, "url": None,
                        "port": None, "error": None, "config_file": CONFIG_NAME, "log": ""}
            if run:
                result = {name: run.get(name) for name in (
                    "status", "type", "url", "port", "error", "started_at", "ended_at")}
                result.update(configured=True, config_file=CONFIG_NAME,
                              log=_tail(self._log_path(key)))
                return result
            return {"configured": True, "status": "stopped", "type": config["type"], "url": None,
                    "port": None, "error": None, "config_file": CONFIG_NAME,
                    "log": _tail(self._log_path(key))}

    def start(self, key: str, repository: Path) -> dict:
        repository = repository.resolve()
        with self.lock:
            if self.closing:
                raise PreviewConflict("サービスを停止しています")
            previous = self.runs.get(key)
            if previous and previous["status"] in ACTIVE_PREVIEW:
                raise PreviewConflict("この案件のプレビューは既に起動しています")
            config = read_preview_config(repository)
            if not config:
                raise ValueError(f"{CONFIG_NAME} がありません")
            port = self._choose_port(config["port"])
            token = secrets.token_urlsafe(24)
            command = [part.replace("{port}", str(port)) for part in config["command"]]
            env = os.environ.copy()
            env[config["port_env"]] = str(port)
            env["DEV_PIPELINE_PREVIEW_PORT"] = str(port)
            env["DEV_PIPELINE_PREVIEW_BASE_PATH"] = f"/preview/{token}/"
            log_path = self._log_path(key)
            run = {
                "key": key, "repository": str(repository), "status": "starting", "type": "web",
                "url": f"/preview/{token}/", "token": token, "port": port, "error": None,
                "started_at": now_iso(), "ended_at": None, "stop_requested": False,
                "config": config, "process": None, "thread": None,
            }
            self.runs[key] = run
            self.tokens[token] = key
            try:
                with log_path.open("w", encoding="utf-8") as output:
                    process = subprocess.Popen(command, cwd=repository, env=env, stdout=output,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                run["process"] = process
            except (OSError, ValueError) as error:
                self.ports.discard(port)
                self.tokens.pop(token, None)
                run.update(status="failed", error=f"起動できませんでした: {error}", ended_at=now_iso())
                return self.status(key, repository)
            thread = threading.Thread(target=self._monitor, args=(key,), daemon=True,
                                      name=f"preview-{hashlib.sha256(key.encode()).hexdigest()[:8]}")
            run["thread"] = thread
            thread.start()
            return self.status(key, repository)

    @staticmethod
    def _signal(process: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    def _healthy(port: int, path: str) -> bool:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
            response = connection.getresponse()
            response.read(1024)
            return 200 <= response.status < 400
        except OSError:
            return False
        finally:
            connection.close()

    def _monitor(self, key: str) -> None:
        with self.lock:
            run = self.runs[key]
            process = run["process"]
            deadline = time.monotonic() + run["config"]["startup_timeout"]
        while process.poll() is None:
            with self.lock:
                stopping = run["stop_requested"]
            if stopping:
                self._signal(process, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._signal(process, signal.SIGKILL)
                break
            if self._healthy(run["port"], run["config"]["health_path"]):
                with self.lock:
                    if not run["stop_requested"]:
                        run["status"] = "running"
                break
            if time.monotonic() >= deadline:
                with self.lock:
                    run["error"] = "ヘルスチェックが時間内に成功しませんでした"
                    run["stop_requested"] = True
                    run["status"] = "stopping"
                continue
            time.sleep(0.15)
        while process.poll() is None:
            with self.lock:
                stopping = run["stop_requested"]
            if stopping:
                self._signal(process, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._signal(process, signal.SIGKILL)
            else:
                time.sleep(0.2)
        with self.lock:
            stopped = run["stop_requested"] and not run["error"]
            run["status"] = "stopped" if stopped else "failed"
            if not stopped and not run["error"]:
                run["error"] = f"プレビュープロセスが終了しました（終了コード {process.returncode}）"
            run["ended_at"] = now_iso()
            self.ports.discard(run["port"])
            self.tokens.pop(run["token"], None)

    def stop(self, key: str, wait: bool = False) -> dict:
        with self.lock:
            run = self.runs.get(key)
            if not run or run["status"] not in ACTIVE_PREVIEW:
                raise PreviewConflict("現在起動中のプレビューはありません")
            run["stop_requested"] = True
            run["status"] = "stopping"
            self._signal(run["process"], signal.SIGTERM)
            thread = run["thread"]
            repository = Path(run["repository"])
        if wait:
            thread.join(timeout=7)
        return self.status(key, repository)

    def restart(self, key: str, repository: Path) -> dict:
        with self.lock:
            run = self.runs.get(key)
            active = bool(run and run["status"] in ACTIVE_PREVIEW)
        if active:
            self.stop(key, wait=True)
        return self.start(key, repository)

    def target(self, token: str) -> tuple[int, str] | None:
        with self.lock:
            key = self.tokens.get(token)
            run = self.runs.get(key) if key else None
            if not run or run["status"] not in {"starting", "running"}:
                return None
            return run["port"], run["status"]

    def close(self) -> None:
        with self.lock:
            self.closing = True
            keys = [key for key, run in self.runs.items() if run["status"] in ACTIVE_PREVIEW]
        for key in keys:
            try:
                self.stop(key)
            except PreviewConflict:
                pass
        with self.lock:
            threads = [run["thread"] for run in self.runs.values() if run.get("thread")]
        for thread in threads:
            thread.join(timeout=7)
