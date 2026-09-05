"""Stable read API for CLI and user interfaces.

Only this module knows how pipeline results are laid out on disk. Consumers receive
plain dictionaries and action capabilities instead of inspecting stored files.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .store import WorkStore


def default_repository(work_dir: Path, initiative_id: str) -> Path:
    return WorkStore(work_dir, initiative_id).path / "repo"


def initiative_exists(work_dir: Path, initiative_id: str) -> bool:
    return (WorkStore(work_dir, initiative_id).path / "initiative.json").is_file()


def list_initiative_ids(work_dir: Path) -> list[str]:
    root = work_dir.resolve()
    if not root.is_dir():
        return []
    return sorted(
        path.name for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
        and (path / "initiative.json").is_file()
    )


def _read_tail(path: Path, limit: int = 100_000) -> str:
    try:
        with path.open("rb") as stream:
            size = path.stat().st_size
            if size > limit:
                stream.seek(-limit, 2)
            content = stream.read(limit).decode("utf-8", errors="replace")
        return ("［末尾100KBを表示］\n" if size > limit else "") + content
    except FileNotFoundError:
        return ""


def available_actions(state: str) -> dict[str, bool]:
    """Describe valid user actions without exposing transition rules to a UI."""
    return {
        "run": state not in {"AWAITING_HUMAN_CHECK", "COMPLETED"},
        "human_check": state == "AWAITING_HUMAN_CHECK",
        "completed": state == "COMPLETED",
    }


def read_pipeline(work_dir: Path, initiative_id: str,
                  include_evidence: bool = True) -> dict[str, Any]:
    """Return the public read model for one initiative."""
    store = WorkStore(work_dir, initiative_id)
    state = store.load_state()
    initiative = store.load_initiative()
    phases = [store.load_phase(phase_id).as_dict() for phase_id in initiative.phase_order]
    result: dict[str, Any] = {
        "initiative": initiative.as_dict(),
        "state": state.as_dict(),
        "revision": state.revision(),
        "phases": phases,
        "usage": store.load_token_usage(),
        "actions": available_actions(state.state),
    }
    if not include_evidence:
        return result

    result.update(evidence=[], checkpoint="")
    for phase in phases:
        evidence = {"phase_id": phase["id"], "verification": None, "review": None,
                    "verification_log": ""}
        for key, suffix in (("verification", "verification"),
                            ("review", "adversarial-review")):
            try:
                evidence[key] = store.read_json(f"evidence/{phase['id']}-{suffix}.json")
            except FileNotFoundError:
                pass
        evidence["verification_log"] = _read_tail(
            store.path / "evidence" / f"{phase['id']}-verification.log")
        result["evidence"].append(evidence)

    if state.current_phase:
        checkpoints = sorted(
            (store.path / "runs").glob(f"{state.current_phase}-run-*.md"),
            key=lambda path: int(path.stem.rsplit("-", 1)[1]),
        )
        if checkpoints:
            result["checkpoint"] = _read_tail(checkpoints[-1])
    return result
