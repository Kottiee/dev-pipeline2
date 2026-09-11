from __future__ import annotations

import json
import shlex
from pathlib import Path

from .base import AgentError, AgentResult, extract_json
from .command import CommandAgent


def _extract_codex_answer(output: str) -> str:
    messages: list[str] = []
    has_jsonl = False
    for line in output.splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        if line_str.startswith("{") and line_str.endswith("}"):
            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and "type" in event:
                has_jsonl = True
                if event.get("type") == "item.completed":
                    item = event.get("item")
                    if isinstance(item, dict) and item.get("type") == "agent_message":
                        text = item.get("text")
                        if isinstance(text, str):
                            messages.append(text)
    if has_jsonl and messages:
        for msg in reversed(messages):
            try:
                extract_json(msg)
                return msg
            except AgentError:
                continue
        try:
            combined = "\n".join(messages)
            extract_json(combined)
            return combined
        except AgentError:
            return messages[-1]
    return output


class CodexAgent(CommandAgent):
    def __init__(self, command: str | None = None, log_dir: Path | None = None):
        super().__init__(command or "codex exec --sandbox workspace-write --json -", log_dir, name="codex")
        self.allocation_usage_format = "codex"
        self.allocation_command = self.command

    def execute(self, prompt: str, repository: Path) -> AgentResult:
        result = self.runner.run(shlex.split(self.command), prompt, repository)
        answer = _extract_codex_answer(result.stdout)
        try:
            data = extract_json(answer)
        except AgentError as error:
            raise AgentError(str(error), result.log_path) from error
        return AgentResult(data, result.stdout, result.stderr)

