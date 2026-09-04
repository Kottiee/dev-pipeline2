from __future__ import annotations

import shlex
from pathlib import Path

from .base import AgentError, AgentResult, extract_json
from .process import ProcessRunner


class CommandAgent:
    """Generic stdin prompt / trailing JSON stdout adapter."""

    def __init__(self, command: str | None = None, log_dir: Path | None = None,
                 name: str = "command"):
        if not command:
            raise ValueError(f"agent {name!r} requires --agent-command")
        self.command = command
        self.runner = ProcessRunner(name, log_dir)

    def execute(self, prompt: str, repository: Path) -> AgentResult:
        result = self.runner.run(shlex.split(self.command), prompt, repository)
        try:
            data = extract_json(result.stdout)
        except AgentError as error:
            raise AgentError(str(error), result.log_path) from error
        return AgentResult(data, result.stdout, result.stderr)
