from __future__ import annotations

import json
import os
import re
import tempfile
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .allocation import summarize_allocation_usage
from .models import Issue, Phase, PipelineState, Project, now_iso
from .usage import summarize_token_usage


class JsonStore:
    """Small atomic JSON/text store rooted at one directory."""

    def __init__(self, path: Path):
        self.path = path.resolve()

    def _safe_path(self, relative: str) -> Path:
        path = (self.path / relative).resolve()
        if self.path != path and self.path not in path.parents:
            raise ValueError("path escapes directory")
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

    def write_text(self, relative: str, text: str) -> None:
        destination = self._safe_path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")


class ProjectStore(JsonStore):
    def __init__(self, root: Path, project_id: str):
        if project_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project_id):
            raise ValueError("project id may contain only letters, numbers, dot, underscore, and dash")
        self.root = root.resolve()
        self.project_id = project_id
        super().__init__(self.root / project_id)
        self.issues_dir = self.path / "issues"

    def exists(self) -> bool:
        return (self.path / "project.json").is_file()

    def create(self, project: Project) -> None:
        if self.exists():
            raise FileExistsError(f"project already exists: {self.project_id}")
        self.path.mkdir(parents=True, exist_ok=True)
        self.issues_dir.mkdir(parents=True, exist_ok=True)
        self.save_project(project)

    def save_project(self, project: Project) -> None:
        self.write_json("project.json", project.as_dict())

    def load_project(self) -> Project:
        return Project.from_dict(self.read_json("project.json"))

    def repository_path(self) -> Path:
        if self.exists():
            try:
                project = self.load_project()
                if project.repo:
                    repo_path = Path(project.repo)
                    if not repo_path.is_absolute():
                        repo_path = (self.path / repo_path).resolve()
                    return repo_path
            except Exception:
                pass
        return self.path / "repo"

    def list_issue_ids(self) -> list[str]:
        if not self.issues_dir.is_dir():
            return []
        return sorted(
            path.name for path in self.issues_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".")
            and ((path / "issue.json").is_file() or (path / "initiative.json").is_file())
        )

    def resolve_issue_id(self, issue_id: str | None) -> str:
        """Resolve full issue directory name, supporting prefix match (e.g. '0001' -> '0001-csv-import').
        If issue_id is None or empty, resolves to the latest issue."""
        if not issue_id:
            existing = self.list_issue_ids()
            return existing[-1] if existing else ""
        if (self.issues_dir / issue_id).is_dir():
            return issue_id
        matches = [
            name for name in self.list_issue_ids()
            if name == issue_id or name.startswith(f"{issue_id}-")
        ]
        if len(matches) == 1:
            return matches[0]
        return issue_id

    def issue_store(self, issue_id: str) -> IssueStore:
        resolved_id = self.resolve_issue_id(issue_id)
        return IssueStore(self, resolved_id)

    def next_issue_id(self, slug: str = "") -> str:
        existing = self.list_issue_ids()
        numbers = []
        for name in existing:
            # Match first sequence of digits, or prefix
            m = re.search(r"\d+", name)
            if m:
                numbers.append(int(m.group()))
        next_num = max(numbers, default=0) + 1
        num_str = f"{next_num:04d}"
        clean_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", slug).strip("-")
        return f"{num_str}-{clean_slug}" if clean_slug else num_str

    @contextmanager
    def execution_lock(self):
        """Serialize CLI mutations across processes for this project."""
        self.path.mkdir(parents=True, exist_ok=True)
        with (self.path / ".execution.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("このプロジェクトは別のプロセスで実行中です") from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


class IssueStore(JsonStore):
    def __init__(self, project_store: ProjectStore, issue_id: str, custom_path: Path | None = None):
        if issue_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", issue_id):
            raise ValueError("issue id may contain only letters, numbers, dot, underscore, and dash")
        self.project_store = project_store
        self.issue_id = issue_id
        super().__init__(custom_path if custom_path is not None else project_store.issues_dir / issue_id)

    @property
    def root(self) -> Path:
        return self.project_store.root

    @property
    def initiative_id(self) -> str:
        return self.issue_id

    def exists(self) -> bool:
        return (self.path / "issue.json").is_file() or (self.path / "initiative.json").is_file()

    def create(self, issue: Issue, phases: list[Phase]) -> None:
        managed_paths = ("issue.json", "initiative.json", "state.json", "usage.json", "phases", "runs", "feedback", "evidence")
        if any((self.path / name).exists() for name in managed_paths):
            raise FileExistsError(f"issue already exists: {self.issue_id}")
        for directory in ("phases", "runs", "feedback", "evidence"):
            (self.path / directory).mkdir(parents=True, exist_ok=True)
        self.write_json("issue.json", issue.as_dict())
        for phase in phases:
            self.save_phase(phase)
        phases[0].status = "running"
        self.save_phase(phases[0])
        self.save_state(PipelineState(issue.id, "PHASE_RUNNING", phases[0].id))

    @contextmanager
    def execution_lock(self):
        """Serialize CLI mutations across processes."""
        if self.path == self.project_store.path:
            with self.project_store.execution_lock():
                yield
            return
        with self.project_store.execution_lock():
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

    def save_state(self, state: PipelineState) -> None:
        state.last_updated_at = now_iso()
        self.write_json("state.json", state.as_dict())

    def load_state(self) -> PipelineState:
        return PipelineState.from_dict(self.read_json("state.json"))

    def save_issue(self, issue: Issue) -> None:
        self.write_json("issue.json", issue.as_dict())

    def load_issue(self) -> Issue:
        try:
            return Issue.from_dict(self.read_json("issue.json"))
        except FileNotFoundError:
            return Issue.from_dict(self.read_json("initiative.json"))

    def load_initiative(self) -> Issue:
        return self.load_issue()

    def save_phase(self, phase: Phase) -> None:
        self.write_json(f"phases/{phase.id}.json", phase.as_dict())

    def load_phase(self, phase_id: str) -> Phase:
        return Phase.from_dict(self.read_json(f"phases/{phase_id}.json"))

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
        allocation = summarize_allocation_usage(events)
        self.write_json("usage.json", {
            "events": events,
            "token_usage": summary,
            "allocation_usage": allocation,
        })

    def load_token_usage(self) -> dict[str, int]:
        return summarize_token_usage(item.get("usage") for item in self._usage_events())

    def load_allocation_usage(self) -> dict[str, Any]:
        return summarize_allocation_usage(self._usage_events())


class WorkStore(IssueStore):
    """Backward-compatible store for tests or legacy callers expecting root / id."""
    def __init__(self, root: Path, initiative_id: str):
        project_store = ProjectStore(root, initiative_id)
        target_path = root.resolve() / initiative_id
        if (target_path / "project.json").is_file() and (target_path / "issues").is_dir():
            issue_ids = project_store.list_issue_ids()
            chosen_issue = issue_ids[0] if issue_ids else "0001"
            super().__init__(project_store, chosen_issue)
        else:
            super().__init__(project_store, initiative_id, custom_path=target_path)
