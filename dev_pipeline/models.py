from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


PIPELINE_STATES = {
    "PLANNING", "PHASE_RUNNING", "PHASE_VERIFYING", "PHASE_REVIEWING",
    "AWAITING_HUMAN_CHECK", "PHASE_FIXING", "COMPLETED", "FAILED",
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
class Initiative:
    id: str
    goal: str
    requirements: list[str]
    constraints: list[str]
    phase_order: list[str]
    checks: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Initiative":
        data = dict(value)
        data["checks"] = _parse_checks(data.get("checks"))
        initiative = cls(**data)
        if not initiative.goal or not initiative.phase_order:
            raise ValueError("initiative goal and at least one phase are required")
        return initiative

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineState:
    initiative_id: str
    state: str
    current_phase: str | None
    current_run: int = 0
    last_updated_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PipelineState":
        state = cls(**value)
        if state.state not in PIPELINE_STATES:
            raise ValueError(f"invalid pipeline state: {state.state}")
        return state

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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
