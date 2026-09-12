from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from datetime import datetime, timezone
from typing import Any


PIPELINE_STATES = {
    "PLANNING", "PHASE_RUNNING", "PHASE_VERIFYING", "PHASE_REVIEWING",
    "AWAITING_HUMAN_CHECK", "PHASE_FIXING", "RESOLVED", "COMPLETED", "FAILED",
}
PHASE_STATUSES = {"pending", "running", "awaiting_human_check", "accepted"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_checks(value: Any) -> list[tuple[str, str]]:
    checks = []
    for entry in value or []:
        if isinstance(entry, (list, tuple)) and len(entry) == 2 \
                and all(isinstance(item, str) for item in entry):
            checks.append((entry[0], entry[1]))
    return checks


@dataclass
class Phase:
    id: str
    sequence: int
    goal: str
    scope: list[str]
    acceptance_criteria: list[str]
    status: str = "pending"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Phase":
        phase = cls(**value)
        if phase.status not in PHASE_STATUSES:
            raise ValueError(f"invalid phase status: {phase.status}")
        if not phase.goal or not phase.acceptance_criteria:
            raise ValueError("phase goal and acceptance criteria are required")
        return phase

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Project:
    id: str
    repo: str | None = None
    agent: str = "codex"
    agent_command: str | None = None
    checks: list[tuple[str, str]] = field(default_factory=list)
    token_usage_format: str | None = None
    created_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Project":
        data = dict(value)
        data["checks"] = _parse_checks(data.get("checks"))
        return cls(**data)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Issue:
    id: str
    description: str
    phase_order: list[str]
    checks: list[tuple[str, str]] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)

    @property
    def goal(self) -> str:
        """Backward-compatible alias used by display helpers."""
        return self.description

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Issue":
        data = dict(value)
        data["checks"] = _parse_checks(data.get("checks"))
        # Backward compatibility: merge legacy goal/requirements/constraints
        if "description" not in data and "goal" in data:
            parts = [data.pop("goal")]
            for key in ("requirements", "constraints"):
                items = data.pop(key, None)
                if items:
                    parts.append("\n".join(items) if isinstance(items, list) else str(items))
            data["description"] = "\n".join(parts)
        else:
            data.pop("goal", None)
            data.pop("requirements", None)
            data.pop("constraints", None)
        if "created_at" not in data:
            data["created_at"] = now_iso()
        issue = cls(**data)
        if not issue.description or not issue.phase_order:
            raise ValueError("issue description and at least one phase are required")
        return issue

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


Initiative = Issue


@dataclass
class PipelineState:
    issue_id: str
    state: str
    current_phase: str | None
    current_run: int = 0
    last_updated_at: str = field(default_factory=now_iso)

    @property
    def initiative_id(self) -> str:
        return self.issue_id

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PipelineState":
        data = dict(value)
        if "issue_id" not in data and "initiative_id" in data:
            data["issue_id"] = data.pop("initiative_id")
        else:
            data.pop("initiative_id", None)
        state = cls(**data)
        if state.state not in PIPELINE_STATES:
            raise ValueError(f"invalid pipeline state: {state.state}")
        return state

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def revision(self) -> str:
        """Identify exactly the state a human inspected before making a decision."""
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()


@dataclass
class RunOutcome:
    result: str
    completed: list[str]
    remaining: list[str]
    problems: list[str]
    recommended_next_action: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunOutcome":
        outcome = cls(**value)
        if outcome.result not in {"CONTINUE", "GOAL_REACHED"}:
            raise ValueError("run result must be CONTINUE or GOAL_REACHED")
        return outcome
