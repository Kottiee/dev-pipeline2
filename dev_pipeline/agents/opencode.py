from __future__ import annotations

import json
import shlex
import uuid
from pathlib import Path

from .base import AgentError, AgentResult, extract_json
from .process import ProcessRunner


class PermissionInterrupted(AgentError):
    def __init__(self, message: str, session: str | None):
        super().__init__(message)
        self.session = session


class OpenCodeAgent:
    """OpenCode JSON events and bounded same-session protocol recovery."""

    def __init__(self, command: str | None = None, log_dir: Path | None = None):
        self.command = command or "opencode run"
        self.runner = ProcessRunner("opencode", log_dir)

    def execute(self, prompt: str, repository: Path) -> AgentResult:
        repository = repository.resolve()
        command = shlex.split(self.command) + ["--format", "json", "--dir", str(repository)]
        invocation = uuid.uuid4().hex
        stdout, stderr = [], []
        session = None
        for attempt in range(3):
            args = command + (["--session", session] if session else [])
            result = self.runner.run(args, prompt, repository, invocation, attempt + 1)
            stdout.append(result.stdout)
            stderr.append(result.stderr)
            try:
                try:
                    answer, session = self._opencode_answer(result.stdout)
                except PermissionInterrupted as error:
                    if not error.session or attempt == 2:
                        raise
                    session = error.session
                    prompt = (
                        "The last operation was denied. Respect that denial: do not retry "
                        "the denied operation, change permissions, or access the denied path "
                        "through another tool. Continue the original task using only allowed "
                        f"operations within {repository}. Run commands from that directory. "
                        "Use in-memory checks or new temporary inputs inside that repository "
                        "instead of external scratch paths. Preserve the original output "
                        "contract and return valid JSON. If the task genuinely requires the "
                        "denied operation, report the blocker rather than claim success.\n"
                        f"Interruption: {error}"
                    )
                    continue
                try:
                    data = extract_json(answer)
                except AgentError:
                    if not session or attempt == 2:
                        raise
                    prompt = (
                        "Your final answer was not valid JSON. Re-emit the SAME result and ALL "
                        "findings/fields as one valid JSON object. Only correct JSON syntax and "
                        "escaping; do not change the decision, omit findings, or redo the work. "
                        "Do not call tools. Escape quotes, backslashes, and newlines inside strings. "
                        "No Markdown fences or trailing commentary. Previous answer:\n" + answer
                    )
                    continue
                return AgentResult(data, "\n".join(stdout), "\n".join(stderr))
            except AgentError as error:
                raise AgentError(str(error), result.log_path) from error
        raise AssertionError("unreachable")

    @staticmethod
    def _opencode_answer(output: str) -> tuple[str, str | None]:
        session = None
        texts = []
        message_id = None
        finish = None
        tool_error = None
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise AgentError(f"invalid OpenCode JSON event: {error}") from error
            if not isinstance(event, dict):
                raise AgentError("OpenCode event must be an object")
            session = event.get("sessionID", session)
            part = event.get("part", {})
            kind = event.get("type")
            if kind == "error":
                raise AgentError(f"OpenCode error: {event.get('error')}")
            if kind == "step_start":
                finish = None
                texts = []
                message_id = part.get("messageID")
            elif kind == "text":
                if part.get("messageID") != message_id:
                    texts = []
                    message_id = part.get("messageID")
                texts.append(part.get("text", ""))
            elif kind == "tool_use" and part.get("state", {}).get("status") == "error":
                tool_error = part["state"].get("error")
            elif kind == "step_finish":
                finish = part.get("reason")
        if finish != "stop" or not texts:
            if tool_error and "rejected permission" in tool_error.lower():
                raise PermissionInterrupted(f"OpenCode permission interruption: {tool_error}", session)
            raise AgentError("OpenCode ended without a completed final answer"
                             + (f": {tool_error}" if tool_error else f" (finish={finish})"))
        return "\n".join(texts), session
