from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AgentError(RuntimeError):
    pass


class PermissionInterrupted(AgentError):
    def __init__(self, message: str, session: str | None):
        super().__init__(message)
        self.session = session


DEFAULT_COMMANDS = {
    "codex": "codex exec --sandbox workspace-write -",
    "opencode": "opencode run --format json",
}


@dataclass
class AgentResult:
    data: dict[str, Any]
    stdout: str
    stderr: str


class CommandAgent:
    """Normalize CLI final answers to the pipeline JSON contract."""

    def __init__(self, name: str, command: str | None = None, log_dir: Path | None = None):
        if name not in DEFAULT_COMMANDS and command is None:
            raise ValueError(f"unknown agent {name!r}; provide --agent-command")
        self.name = name
        self.command = command or DEFAULT_COMMANDS[name]
        self.log_dir = log_dir
        self.opencode_events = name == "opencode" and command is None

    def execute(self, prompt: str, repository: Path) -> AgentResult:
        repository = repository.resolve()
        command = shlex.split(self.command)
        env = os.environ.copy()
        env["PWD"] = str(repository)
        if self.opencode_events:
            command.extend(["--dir", str(repository)])
        invocation = uuid.uuid4().hex
        stdout, stderr = [], []
        session = None
        # Resume the same context for protocol recovery, never restart the work.
        for attempt in range(3):
            args = command + (["--session", session] if session else [])
            completed = subprocess.run(
                args, input=prompt, text=True, cwd=repository, env=env,
                capture_output=True, check=False,
            )
            stdout.append(completed.stdout)
            stderr.append(completed.stderr)
            log_path = None
            if self.log_dir is not None:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                log_path = self.log_dir / f"{invocation}-{attempt + 1}.json"
                log_path.write_text(json.dumps({
                    "command": args, "repository": str(repository), "prompt": prompt,
                    "returncode": completed.returncode, "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                if completed.returncode:
                    raise AgentError(f"{self.name} exited with {completed.returncode}: "
                                     f"{completed.stderr.strip()}")
                answer = completed.stdout
                if self.opencode_events:
                    try:
                        answer, session = self._opencode_answer(answer)
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
                    data = self._extract_json(answer)
                except AgentError:
                    if not self.opencode_events or not session or attempt == 2:
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
                suffix = f" (agent log: {log_path})" if log_path else ""
                raise AgentError(f"{error}{suffix}") from error
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

    @staticmethod
    def _extract_json(output: str) -> dict[str, Any]:
        decoder = json.JSONDecoder()
        candidates = [index for index, char in enumerate(output) if char == "{"]
        for index in reversed(candidates):
            try:
                candidate = output[index:]
                value, end = decoder.raw_decode(candidate)
                if isinstance(value, dict) and not candidate[end:].strip():
                    return value
            except json.JSONDecodeError:
                continue
        raise AgentError("agent output did not end with a valid JSON object")
