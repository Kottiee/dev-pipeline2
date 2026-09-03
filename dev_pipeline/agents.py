from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AgentError(RuntimeError):
    pass


DEFAULT_COMMANDS = {
    "codex": "codex exec --full-auto -",
    "opencode": "opencode run",
}


@dataclass
class AgentResult:
    data: dict[str, Any]
    stdout: str
    stderr: str


class CommandAgent:
    """Agent adapter using a JSON object on the final non-empty stdout line."""

    def __init__(self, name: str, command: str | None = None):
        if name not in DEFAULT_COMMANDS and command is None:
            raise ValueError(f"unknown agent {name!r}; provide --agent-command")
        self.name = name
        self.command = command or DEFAULT_COMMANDS[name]

    def execute(self, prompt: str, repository: Path) -> AgentResult:
        completed = subprocess.run(
            shlex.split(self.command), input=prompt, text=True, cwd=repository,
            capture_output=True, check=False,
        )
        if completed.returncode:
            raise AgentError(
                f"{self.name} exited with {completed.returncode}: {completed.stderr.strip()}"
            )
        data = self._extract_json(completed.stdout)
        return AgentResult(data, completed.stdout, completed.stderr)

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
