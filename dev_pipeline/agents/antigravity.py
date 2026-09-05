from __future__ import annotations

import json
import shlex
from pathlib import Path

from .base import AgentError, AgentResult, extract_json
from .process import ProcessRunner


class AntigravityAgent:
    """Antigravity CLI (agy) headless streaming adapter."""

    def __init__(self, command: str | None = None, log_dir: Path | None = None,
                 default_timeout: str | None = "40m"):
        self.command = command or "agy"
        self.runner = ProcessRunner("antigravity", log_dir)
        self.token_usage_format = "antigravity"
        self.allocation_usage_format = "antigravity"
        self.allocation_command = self.command
        self.default_timeout = default_timeout

    def execute(self, prompt: str, repository: Path) -> AgentResult:
        repository = repository.resolve()
        args = shlex.split(self.command) + [
            "--input-format", "stream-json",
            "--output-format", "stream-json",
        ]
        if self.default_timeout and not any(
            arg == "--print-timeout" or arg.startswith("--print-timeout=")
            for arg in args
        ):
            args.extend(["--print-timeout", self.default_timeout])
        message = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
        result = self.runner.run(args, message, repository, check_returncode=False)
        try:
            payload = self._agy_result(result.stdout)
            if payload.get("status") != "SUCCESS":
                error = payload.get("error") or "no error message"
                raise AgentError(f"Antigravity ended with status {payload.get('status')}: {error}")
            if result.returncode != 0:
                detail = result.stderr.strip()
                detail_msg = f": {detail}" if detail else ""
                raise AgentError(f"antigravity exited with {result.returncode}{detail_msg}")
            answer = payload.get("response", "")
            if not answer:
                raise AgentError("Antigravity ended without a response")
            data = extract_json(answer)
        except AgentError as error:
            if result.returncode != 0 and (result.stderr.strip() or "ended without a result event" in str(error) or "invalid Antigravity JSON event" in str(error)):
                detail = result.stderr.strip()
                detail_msg = f": {detail}" if detail else ""
                raise AgentError(f"antigravity exited with {result.returncode}{detail_msg}", result.log_path) from error
            raise AgentError(str(error), result.log_path) from error
        return AgentResult(data, result.stdout, result.stderr)

    @staticmethod
    def _agy_result(output: str) -> dict[str, object]:
        result = None
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise AgentError(f"invalid Antigravity JSON event: {error}") from error
            if not isinstance(event, dict) or event.get("event") != "result":
                continue
            payload = event.get("result")
            if not isinstance(payload, dict):
                raise AgentError("Antigravity result event must carry an object")
            result = payload
        if result is None:
            raise AgentError("Antigravity ended without a result event")
        return result
