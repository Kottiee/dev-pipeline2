"""Agent selection lives here; pipeline code depends only on the Agent contract."""

from pathlib import Path
from typing import Callable

from .base import Agent, AgentError, AgentResult
from .antigravity import AntigravityAgent
from .codex import CodexAgent
from .command import CommandAgent
from .opencode import OpenCodeAgent


# Factories accept (command override, log directory). No dynamic plugin loading.
AGENT_FACTORIES: dict[str, Callable[[str | None, Path | None], Agent]] = {
    "codex": CodexAgent,
    "opencode": OpenCodeAgent,
    "antigravity": AntigravityAgent,
    "command": CommandAgent,
}


def create_agent(name: str, command: str | None = None, log_dir: Path | None = None) -> Agent:
    factory = AGENT_FACTORIES.get(name)
    if factory is not None:
        return factory(command, log_dir)
    if command:
        return CommandAgent(command, log_dir, name=name)
    raise ValueError(f"unknown agent {name!r}; register an adapter or provide --agent-command")
