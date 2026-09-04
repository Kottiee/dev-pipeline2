from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .base import AgentError


@dataclass
class ProcessResult:
    stdout: str
    stderr: str
    log_path: Path | None
    returncode: int = 0


class ProcessRunner:
    """Shared CLI execution and logs; no agent protocol or retry policy."""

    def __init__(self, name: str, log_dir: Path | None = None):
        self.name = name
        self.log_dir = log_dir

    def run(self, command: list[str], prompt: str, repository: Path,
            invocation: str | None = None, attempt: int = 1,
            check_returncode: bool = True) -> ProcessResult:
        repository = repository.resolve()
        env = os.environ.copy()
        env["PWD"] = str(repository)
        completed = subprocess.run(
            command, input=prompt, text=True, cwd=repository, env=env,
            capture_output=True, check=False,
        )
        log_path = None
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / f"{invocation or uuid.uuid4().hex}-{attempt}.json"
            log_path.write_text(json.dumps({
                "command": command, "repository": str(repository), "prompt": prompt,
                "returncode": completed.returncode, "stdout": completed.stdout,
                "stderr": completed.stderr,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if check_returncode and completed.returncode:
            detail = completed.stderr.strip()
            detail_msg = f": {detail}" if detail else ""
            raise AgentError(f"{self.name} exited with {completed.returncode}{detail_msg}", log_path)
        return ProcessResult(completed.stdout, completed.stderr, log_path, completed.returncode)

