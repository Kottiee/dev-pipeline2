from __future__ import annotations

import json
import os
import re
import tempfile
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import Initiative, Phase, PipelineState, now_iso
from .usage import summarize_token_usage


class WorkStore:
    def __init__(self, root: Path, initiative_id: str):
        if initiative_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", initiative_id):
            raise ValueError("initiative id may contain only letters, numbers, dot, underscore, and dash")
        self.root = root.resolve()
        self.initiative_id = initiative_id
        self.path = self.root / initiative_id

    @contextmanager
    def execution_lock(self):
        """Serialize CLI mutations, including long agent calls, across processes."""
        self.path.mkdir(parents=True, exist_ok=True)
        with (self.path / ".execution.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("この案件は別のプロセスで実行中です") from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def create(self, initiative: Initiative, phases: list[Phase]) -> None:
        # A repository may already have been prepared beneath this directory.
        # Refuse to overwrite pipeline metadata, including a partial initialization.
        managed_paths = ("initiative.json", "state.json", "usage.json", "phases", "runs", "feedback", "evidence")
        if any((self.path / name).exists() for name in managed_paths):
            raise FileExistsError(f"initiative already exists: {self.initiative_id}")
        for directory in ("phases", "runs", "feedback", "evidence"):
            (self.path / directory).mkdir(parents=True, exist_ok=True)
        self.write_json("initiative.json", initiative.as_dict())
        for phase in phases:
            self.save_phase(phase)
        phases[0].status = "running"
        self.save_phase(phases[0])
        self.save_state(PipelineState(initiative.id, "PHASE_RUNNING", phases[0].id))

    def _safe_path(self, relative: str) -> Path:
        path = (self.path / relative).resolve()
        if self.path != path and self.path not in path.parents:
            raise ValueError("path escapes initiative directory")
        return path

    def write_json(self, relative: str, value: Any) -> None:
        destination = self._safe_path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def read_json(self, relative: str) -> Any:
        with self._safe_path(relative).open(encoding="utf-8") as stream:
            return json.load(stream)

    def save_state(self, state: PipelineState) -> None:
        state.last_updated_at = now_iso()
        self.write_json("state.json", state.as_dict())

    def load_state(self) -> PipelineState:
        return PipelineState.from_dict(self.read_json("state.json"))

    def load_initiative(self) -> Initiative:
        return Initiative.from_dict(self.read_json("initiative.json"))

    def save_phase(self, phase: Phase) -> None:
        self.write_json(f"phases/{phase.id}.json", phase.as_dict())

    def load_phase(self, phase_id: str) -> Phase:
        return Phase.from_dict(self.read_json(f"phases/{phase_id}.json"))

    def write_text(self, relative: str, text: str) -> None:
        destination = self._safe_path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")

    def _usage_events(self) -> list[dict[str, Any]]:
        path = self.path / "usage.json"
        if not path.is_file():
            return []
        try:
            value = self.read_json("usage.json")
        except (OSError, ValueError):
            return []
        events = value.get("events") if isinstance(value, dict) else None
        if not isinstance(events, list):
            return []
        return [event for event in events if isinstance(event, dict)]

    def record_usage_event(self, event: dict[str, Any]) -> None:
        events = self._usage_events()
        events.append(event)
        summary = summarize_token_usage(item.get("usage") for item in events)
        self.write_json("usage.json", {"events": events, "token_usage": summary})

    def load_token_usage(self) -> dict[str, int]:
        return summarize_token_usage(item.get("usage") for item in self._usage_events())
