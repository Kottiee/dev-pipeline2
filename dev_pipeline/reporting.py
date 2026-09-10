"""Stable read API for CLI and user interfaces.

Only this module knows how pipeline results are laid out on disk. Consumers receive
plain dictionaries and action capabilities instead of inspecting stored files.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import Issue, Project
from .store import IssueStore, ProjectStore, WorkStore


def default_repository(work_dir: Path, project_id: str) -> Path:
    return ProjectStore(work_dir, project_id).repository_path()


def project_exists(work_dir: Path, project_id: str) -> bool:
    return ProjectStore(work_dir, project_id).exists()


def list_project_ids(work_dir: Path) -> list[str]:
    root = work_dir.resolve()
    if not root.is_dir():
        return []
    return sorted(
        path.name for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
        and (path / "project.json").is_file()
    )


def issue_exists(work_dir: Path, project_id: str, issue_id: str) -> bool:
    p_store = ProjectStore(work_dir, project_id)
    return p_store.issue_store(issue_id).exists()


def list_issue_ids(work_dir: Path, project_id: str) -> list[str]:
    return ProjectStore(work_dir, project_id).list_issue_ids()


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
        "run": state not in {"AWAITING_HUMAN_CHECK", "RESOLVED", "COMPLETED"},
        "human_check": state == "AWAITING_HUMAN_CHECK",
        "completed": state in {"RESOLVED", "COMPLETED"},
    }


def read_issue(work_dir: Path, project_id: str, issue_id: str,
               include_evidence: bool = True) -> dict[str, Any]:
    """Return the public read model for one issue in a project."""
    p_store = ProjectStore(work_dir, project_id)
    resolved_id = p_store.resolve_issue_id(issue_id)
    store = p_store.issue_store(resolved_id)
    state = store.load_state()
    issue = store.load_issue()
    phases = [store.load_phase(phase_id).as_dict() for phase_id in issue.phase_order]
    result: dict[str, Any] = {
        "project_id": project_id,
        "issue": issue.as_dict(),
        "state": state.as_dict(),
        "revision": state.revision(),
        "phases": phases,
        "usage": store.load_token_usage(),
        "allocation_usage": store.load_allocation_usage(),
        "actions": available_actions(state.state),
    }
    if not include_evidence:
        return result

    result.update(evidence=[], checkpoint="")
    for phase in phases:
        evidence = {"phase_id": phase["id"], "verification": None, "review": None,
                    "review_summary": None, "verification_log": ""}
        for key, suffix in (("verification", "verification"),
                            ("review", "adversarial-review"),
                            ("review_summary", "review-summary")):
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


def read_project(work_dir: Path, project_id: str) -> dict[str, Any]:
    """Return project summary with all its issues."""
    p_store = ProjectStore(work_dir, project_id)
    project = p_store.load_project() if p_store.exists() else Project(id=project_id)
    issue_ids = p_store.list_issue_ids()
    issues = []
    for i_id in issue_ids:
        try:
            issues.append(read_issue(work_dir, project_id, i_id, include_evidence=False))
        except Exception:
            pass
    return {
        "project": project.as_dict(),
        "issues": issues,
    }


def initiative_exists(work_dir: Path, initiative_id: str) -> bool:
    if project_exists(work_dir, initiative_id):
        return True
    return (work_dir / initiative_id / "initiative.json").is_file() or (work_dir / initiative_id / "issue.json").is_file()


def list_initiative_ids(work_dir: Path) -> list[str]:
    projects = list_project_ids(work_dir)
    if projects:
        return projects
    root = work_dir.resolve()
    if not root.is_dir():
        return []
    return sorted(
        path.name for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
        and ((path / "initiative.json").is_file() or (path / "issue.json").is_file())
    )


def read_pipeline(work_dir: Path, target_id: str,
                  include_evidence: bool = True) -> dict[str, Any]:
    """Backward-compatible read_pipeline that works for projects and direct stores."""
    p_store = ProjectStore(work_dir, target_id)
    if p_store.exists():
        issue_ids = p_store.list_issue_ids()
        if issue_ids:
            chosen_issue = issue_ids[-1]
            for i_id in issue_ids:
                try:
                    istore = p_store.issue_store(i_id)
                    st = istore.load_state()
                    if st.state not in {"RESOLVED", "COMPLETED"}:
                        chosen_issue = i_id
                        break
                except Exception:
                    pass
            return read_issue(work_dir, target_id, chosen_issue, include_evidence=include_evidence)
    store = WorkStore(work_dir, target_id)
    state = store.load_state()
    issue = store.load_issue()
    phases = [store.load_phase(phase_id).as_dict() for phase_id in issue.phase_order]
    result: dict[str, Any] = {
        "issue": issue.as_dict(),
        "state": state.as_dict(),
        "revision": state.revision(),
        "phases": phases,
        "usage": store.load_token_usage(),
        "allocation_usage": store.load_allocation_usage(),
        "actions": available_actions(state.state),
    }
    if not include_evidence:
        return result

    result.update(evidence=[], checkpoint="")
    for phase in phases:
        evidence = {"phase_id": phase["id"], "verification": None, "review": None,
                    "review_summary": None, "verification_log": ""}
        for key, suffix in (("verification", "verification"),
                            ("review", "adversarial-review"),
                            ("review_summary", "review-summary")):
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
