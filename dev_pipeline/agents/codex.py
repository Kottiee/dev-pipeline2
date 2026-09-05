from pathlib import Path

from .command import CommandAgent


class CodexAgent(CommandAgent):
    def __init__(self, command: str | None = None, log_dir: Path | None = None):
        super().__init__(command or "codex exec --sandbox workspace-write -", log_dir, name="codex")
        self.allocation_usage_format = "codex"
        self.allocation_command = self.command
