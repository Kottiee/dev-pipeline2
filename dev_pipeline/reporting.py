"""Stable read API for CLI and user interfaces.

Only this module knows how pipeline results are laid out on disk. Consumers receive
plain dictionaries and action capabilities instead of inspecting stored files.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
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


def _collect_phase_runs(store: IssueStore | WorkStore, phase_id: str) -> list[dict[str, Any]]:
    runs_dir = store.path / "runs"
    evidence_dir = store.path / "evidence"
    feedback_dir = store.path / "feedback"

    run_ids: set[int] = set()

    if runs_dir.is_dir():
        for path in runs_dir.glob(f"{phase_id}-run-*.md"):
            try:
                num = int(path.stem.rsplit("-", 1)[1])
                run_ids.add(num)
            except (ValueError, IndexError):
                pass

    if evidence_dir.is_dir():
        for path in evidence_dir.glob(f"{phase_id}-run-*-*.json"):
            try:
                parts = path.stem.split("-run-")
                if len(parts) == 2:
                    num_str = parts[1].split("-")[0]
                    run_ids.add(int(num_str))
            except (ValueError, IndexError):
                pass

    if feedback_dir.is_dir():
        for path in feedback_dir.glob(f"{phase_id}-run-*.json"):
            try:
                num = int(path.stem.rsplit("-", 1)[1])
                run_ids.add(num)
            except (ValueError, IndexError):
                pass

    if not run_ids:
        unnum_verif = evidence_dir / f"{phase_id}-verification.json"
        if unnum_verif.exists():
            try:
                data = json.loads(unnum_verif.read_text(encoding="utf-8"))
                run_num = data.get("run_id")
                if isinstance(run_num, int) and run_num > 0:
                    run_ids.add(run_num)
                else:
                    run_ids.add(1)
            except Exception:
                run_ids.add(1)

    feedbacks_by_run: dict[int, dict[str, Any]] = {}
    if feedback_dir.is_dir():
        feedback_files = sorted(feedback_dir.glob(f"{phase_id}-*.json"))
        for fb_path in feedback_files:
            try:
                fb_data = json.loads(fb_path.read_text(encoding="utf-8"))
                if isinstance(fb_data, dict):
                    r_id = fb_data.get("run_id")
                    if isinstance(r_id, int):
                        feedbacks_by_run[r_id] = fb_data
            except Exception:
                pass
        if not feedbacks_by_run and feedback_files and len(run_ids) == 1:
            try:
                fb_data = json.loads(feedback_files[-1].read_text(encoding="utf-8"))
                if isinstance(fb_data, dict):
                    feedbacks_by_run[next(iter(run_ids))] = fb_data
            except Exception:
                pass

    runs = []
    for run_id in sorted(run_ids, reverse=True):
        checkpoint_path = runs_dir / f"{phase_id}-run-{run_id:03d}.md"
        checkpoint_text = _read_tail(checkpoint_path) if checkpoint_path.exists() else ""

        # Verification
        v_path = evidence_dir / f"{phase_id}-run-{run_id:03d}-verification.json"
        if not v_path.exists():
            candidate = evidence_dir / f"{phase_id}-verification.json"
            if candidate.exists():
                try:
                    c_data = json.loads(candidate.read_text(encoding="utf-8"))
                    if c_data.get("run_id") == run_id or len(run_ids) == 1:
                        v_path = candidate
                except Exception:
                    pass
        v_data = None
        if v_path.exists():
            try:
                v_data = json.loads(v_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        v_log_path = evidence_dir / f"{phase_id}-run-{run_id:03d}-verification.log"
        if not v_log_path.exists() and (evidence_dir / f"{phase_id}-verification.log").exists():
            if v_path == evidence_dir / f"{phase_id}-verification.json":
                v_log_path = evidence_dir / f"{phase_id}-verification.log"
        v_log = _read_tail(v_log_path) if v_log_path.exists() else ""

        # Review
        r_path = evidence_dir / f"{phase_id}-run-{run_id:03d}-adversarial-review.json"
        if not r_path.exists():
            candidate = evidence_dir / f"{phase_id}-adversarial-review.json"
            if candidate.exists():
                try:
                    c_data = json.loads(candidate.read_text(encoding="utf-8"))
                    if c_data.get("run_id") == run_id or len(run_ids) == 1:
                        r_path = candidate
                except Exception:
                    pass
        r_data = None
        if r_path.exists():
            try:
                r_data = json.loads(r_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Review Summary
        s_path = evidence_dir / f"{phase_id}-run-{run_id:03d}-review-summary.json"
        if not s_path.exists():
            candidate = evidence_dir / f"{phase_id}-review-summary.json"
            if candidate.exists():
                try:
                    c_data = json.loads(candidate.read_text(encoding="utf-8"))
                    if c_data.get("run_id") == run_id or len(run_ids) == 1:
                        s_path = candidate
                except Exception:
                    pass
        s_data = None
        if s_path.exists():
            try:
                s_data = json.loads(s_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        fb_data = feedbacks_by_run.get(run_id)

        agent_log_path = evidence_dir / f"{phase_id}-run-{run_id:03d}-agent.log"
        agent_log = _read_tail(agent_log_path) if agent_log_path.exists() else ""

        created_at = ""
        if fb_data and fb_data.get("created_at"):
            created_at = fb_data["created_at"]
        elif s_data and s_data.get("created_at"):
            created_at = s_data["created_at"]
        elif r_data and r_data.get("created_at"):
            created_at = r_data["created_at"]
        elif v_data and v_data.get("created_at"):
            created_at = v_data["created_at"]
        elif checkpoint_path.exists():
            try:
                created_at = datetime.fromtimestamp(checkpoint_path.stat().st_mtime, timezone.utc).isoformat()
            except Exception:
                pass

        stage = "run"
        if fb_data and fb_data.get("result") == "FEEDBACK":
            outcome = "HUMAN_FEEDBACK"
            outcome_label = "差し戻し"
            fb_text = fb_data.get("feedback") or ""
            reason = f"人の判断：{fb_text}".strip()
            next_action = f"修正Run {run_id + 1}を開始"
            stage = "human_check"
        elif fb_data and fb_data.get("result") == "ACCEPT":
            outcome = "ACCEPTED"
            outcome_label = "承認済み"
            reason = "テスト・レビューともに合格"
            next_action = "次へ進む"
            stage = "human_check"
        elif r_data and r_data.get("result") == "PASS":
            outcome = "AWAITING_HUMAN_CHECK"
            outcome_label = "承認待ち"
            reason = "テスト・レビューともに合格"
            next_action = "現在：あなたの承認待ち"
            stage = "review"
        elif r_data and r_data.get("result") == "FINDINGS":
            outcome = "REVIEW_FINDINGS"
            outcome_label = "レビュー指摘"
            reason_text = ""
            if s_data and s_data.get("status") == "SUCCESS":
                items = s_data.get("items") or []
                if items and isinstance(items[0], dict) and items[0].get("problem"):
                    reason_text = items[0]["problem"]
                elif s_data.get("decision_reason"):
                    reason_text = s_data["decision_reason"]
            if not reason_text:
                findings = r_data.get("findings") or []
                if findings and isinstance(findings[0], dict) and findings[0].get("description"):
                    reason_text = findings[0]["description"]
                elif findings:
                    reason_text = str(findings[0])
            reason = reason_text or "レビューで指摘事項が検出されました"
            next_action = f"指摘を反映してRun {run_id + 1}を開始"
            stage = "review"
        elif v_data and v_data.get("overall") == "FAIL":
            outcome = "VERIFICATION_FAILED"
            outcome_label = "テスト失敗"
            failed_checks = [c.get("name") for c in (v_data.get("checks") or []) if c.get("status") != "PASS"]
            if failed_checks:
                reason = f"テスト失敗: {', '.join(str(f) for f in failed_checks)}"
            else:
                reason = "機械検証に失敗しました"
            next_action = f"不具合を修正してRun {run_id + 1}を開始"
            stage = "verification"
        elif v_data and v_data.get("overall") == "PASS":
            outcome = "VERIFICATION_PASSED"
            outcome_label = "検証合格"
            reason = "機械検証に合格しました"
            next_action = "レビューを実行"
            stage = "verification"
        else:
            outcome = "CONTINUE"
            outcome_label = "継続"
            reason = "実装途中"
            next_action = f"実装を継続してRun {run_id + 1}を開始"
            stage = "run"

        runs.append({
            "phase_id": phase_id,
            "run_id": run_id,
            "created_at": created_at,
            "outcome": outcome,
            "outcome_label": outcome_label,
            "reason": reason,
            "next_action": next_action,
            "stage": stage,
            "details": {
                "checkpoint": checkpoint_text,
                "verification": v_data,
                "verification_log": v_log,
                "review": r_data,
                "review_summary": s_data,
                "feedback": fb_data,
                "agent_log": agent_log,
            },
        })

    return runs


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

    result.update(evidence=[], checkpoint="", history=[])
    for phase in phases:
        phase_runs = _collect_phase_runs(store, phase["id"])
        evidence = {"phase_id": phase["id"], "verification": None, "review": None,
                    "review_summary": None, "verification_log": "", "runs": phase_runs}
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
        result["history"].extend(phase_runs)

    if state.current_phase:
        checkpoints = sorted(
            (store.path / "runs").glob(f"{state.current_phase}-run-*.md"),
            key=lambda path: int(path.stem.rsplit("-", 1)[1]),
        )
        if checkpoints:
            result["checkpoint"] = _read_tail(checkpoints[-1])

    try:
        result["changelog"] = store.read_json("evidence/changelog.json")
    except FileNotFoundError:
        result["changelog"] = None

    return result


def read_changelog(repository: Path) -> str:
    path = repository / "CHANGELOG.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


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

    result.update(evidence=[], checkpoint="", history=[])
    for phase in phases:
        phase_runs = _collect_phase_runs(store, phase["id"])
        evidence = {"phase_id": phase["id"], "verification": None, "review": None,
                    "review_summary": None, "verification_log": "", "runs": phase_runs}
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
        result["history"].extend(phase_runs)

    if state.current_phase:
        checkpoints = sorted(
            (store.path / "runs").glob(f"{state.current_phase}-run-*.md"),
            key=lambda path: int(path.stem.rsplit("-", 1)[1]),
        )
        if checkpoints:
            result["checkpoint"] = _read_tail(checkpoints[-1])

    try:
        result["changelog"] = store.read_json("evidence/changelog.json")
    except FileNotFoundError:
        result["changelog"] = None

    return result
