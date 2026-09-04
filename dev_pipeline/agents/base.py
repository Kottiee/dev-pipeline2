from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class AgentError(RuntimeError):
    def __init__(self, message: str, log_path: Path | None = None):
        self.log_path = log_path
        suffix = f" (agent log: {log_path})" if log_path else ""
        super().__init__(message + suffix)


@dataclass
class AgentResult:
    data: dict[str, Any]
    stdout: str
    stderr: str


class Agent(Protocol):
    """The only interface required by planning and pipeline execution."""

    def execute(self, prompt: str, repository: Path) -> AgentResult: ...


def extract_json(output: str) -> dict[str, Any]:
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
