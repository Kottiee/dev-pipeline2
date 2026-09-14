from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agents.base import Agent, AgentError, AgentResult
from .allocation import collect_agent_allocation
from .models import Issue, Phase, PipelineState, RunOutcome, now_iso
from .prompts import render_prompt
from .store import IssueStore, WorkStore
from .usage import DEFAULT_TOKEN_USAGE_FORMAT, extract_token_usage


def format_changelog_entry(issue_id: str, title: str, date: str, summary: str, changes: list[str]) -> str:
    bullet_items = "\n".join(f"- {item}" for item in changes)
    id_part = f"[{issue_id}] " if issue_id else ""
    return f"""## {id_part}{title} - {date}

{summary}

### 主な変更点
{bullet_items}"""


def update_changelog_markdown(original: str, entry: str, issue_id: str) -> str:
    original = original.strip()
    entry = entry.strip()
    if not original:
        return f"# Changelog\n\nAll notable changes to this project will be documented in this file.\n\n{entry}\n"

    if issue_id:
        pattern = rf"(?ms)^## \[{re.escape(issue_id)}\].*?(?=(?:^## |\Z))"
        if re.search(pattern, original):
            replaced = re.sub(pattern, f"{entry}\n\n", original).rstrip()
            return f"{replaced}\n"

    first_section = re.search(r"(?m)^## ", original)
    if first_section:
        pos = first_section.start()
        prefix = original[:pos].rstrip()
        suffix = original[pos:].lstrip()
        return f"{prefix}\n\n{entry}\n\n{suffix}\n"

    return f"{original}\n\n{entry}\n"


class TransitionError(RuntimeError):
    pass



# When neither --check nor registered (init --check) checks are available, verification
# runs the command the implementation agent declares in this file at the repository root.
TEST_MANIFEST_FILE = "devpipeline-tests.json"


class Pipeline:
    def __init__(self, store: IssueStore, repository: Path, agent: Agent):
        self.store = store
        self.repository = repository.resolve()
        self.agent = agent

    @staticmethod
    def initialize(
        store: IssueStore, description: str,
        phase_specs: list[dict[str, Any]],
        checks: list[tuple[str, str]] | None = None,
    ) -> None:
        phases = [Phase(id=f"phase-{index:02d}", sequence=index, **spec)
                  for index, spec in enumerate(phase_specs, 1)]
        issue_id = getattr(store, "issue_id", getattr(store, "initiative_id", ""))
        issue = Issue(issue_id, description,
                      [phase.id for phase in phases],
                      checks=[tuple(check) for check in (checks or [])])
        store.create(issue, phases)

    def _manifest_command(self) -> str | None:
        path = self.repository / TEST_MANIFEST_FILE
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        command = value.get("command") if isinstance(value, dict) else None
        return command.strip() if isinstance(command, str) and command.strip() else None

    def _resolve_checks(self, checks: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """--check overrides registered checks; both fall back to project checks and test manifest."""
        if checks:
            return [tuple(check) for check in checks]
        stored = self.store.load_issue().checks
        if stored:
            return [tuple(check) for check in stored]
        project_store = getattr(self.store, "project_store", None)
        if project_store and project_store.exists():
            try:
                project = project_store.load_project()
                if project.checks:
                    return [tuple(check) for check in project.checks]
            except Exception:
                pass
        command = self._manifest_command()
        return [("test", command)] if command else []

    def run(self, checks: list[tuple[str, str]], verify: bool = True) -> str:
        state = self.store.load_state()
        if state.state not in {"PHASE_RUNNING", "PHASE_FIXING"} or not state.current_phase:
            raise TransitionError(f"cannot run agent while state is {state.state}")
        prompt_checks = self._resolve_checks(checks) if verify else []
        state.current_run += 1
        self.store.save_state(state)
        issue = self.store.load_issue()
        phase = self.store.load_phase(state.current_phase)
        prompt = self._run_prompt(issue, phase, state, prompt_checks)
        try:
            result = self.agent.execute(prompt, self.repository)
        except AgentError as error:
            self._record_failure_usage("run", phase.id, state.current_run, error)
            raise
        self._record_usage("run", phase.id, state.current_run, self._combined_output(result))
        outcome = RunOutcome.from_dict(result.data)
        run_id = f"run-{state.current_run:03d}"
        self.store.write_text(f"runs/{phase.id}-{run_id}.md", self._checkpoint(phase, outcome))
        self.store.write_text(f"evidence/{phase.id}-{run_id}-agent.log", result.stdout + result.stderr)
        if outcome.result == "CONTINUE":
            return "CONTINUE"
        if not verify:
            state.state = "PHASE_REVIEWING"
            self.store.save_state(state)
            return "READY" if self.review() else "REVIEW_FINDINGS"
        checks = self._resolve_checks(checks)
        if not self.verify(checks):
            return "VERIFICATION_FAILED"
        return "READY" if self.review() else "REVIEW_FINDINGS"

    def _is_verification_passed(self, phase_id: str) -> bool:
        path = self.store.path / "evidence" / f"{phase_id}-verification.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("overall") == "PASS"
        except (OSError, ValueError):
            return False

    def _auto_pass_after_retries(self, state: PipelineState, max_review_retries: int) -> None:
        if not state.current_phase:
            return
        phase = self.store.load_phase(state.current_phase)
        state.state = "AWAITING_HUMAN_CHECK"
        phase.status = "awaiting_human_check"
        self.store.save_phase(phase)
        self.store.save_state(state)

        review_path = f"evidence/{phase.id}-adversarial-review.json"
        if (self.store.path / review_path).exists():
            try:
                rev_data = self.store.read_json(review_path)
                rev_data["auto_passed"] = True
                rev_data["warning"] = (
                    f"Phase automatically approved after reaching maximum review retries ({max_review_retries}) "
                    "with passing automated tests."
                )
                self.store.write_json(review_path, rev_data)
                run_rev_path = f"evidence/{phase.id}-run-{state.current_run:03d}-adversarial-review.json"
                if (self.store.path / run_rev_path).exists():
                    self.store.write_json(run_rev_path, rev_data)
            except Exception:
                pass

        summary_path = f"evidence/{phase.id}-review-summary.json"
        if (self.store.path / summary_path).exists():
            try:
                summary_data = self.store.read_json(summary_path)
                summary_data["auto_passed"] = True
                summary_data["warning"] = (
                    f"Phase auto-approved after {max_review_retries} review retries with passing automated tests."
                )
                self.store.write_json(summary_path, summary_data)
                run_sum_path = f"evidence/{phase.id}-run-{state.current_run:03d}-review-summary.json"
                if (self.store.path / run_sum_path).exists():
                    self.store.write_json(run_sum_path, summary_data)
            except Exception:
                pass

    def run_until_ready(self, checks: list[tuple[str, str]], verify: bool = True,
                        max_review_retries: int = 2) -> str:
        resolved = self._resolve_checks(checks)
        while True:
            state = self.store.load_state()
            if state.state == "AWAITING_HUMAN_CHECK":
                return "READY"
            if state.state == "PHASE_VERIFYING":
                self.verify(resolved)
            elif state.state == "PHASE_REVIEWING":
                if self.review():
                    return "READY"
                state = self.store.load_state()
                if (state.review_retries >= max_review_retries and
                        state.current_phase and
                        self._is_verification_passed(state.current_phase)):
                    self._auto_pass_after_retries(state, max_review_retries)
                    return "READY"
            else:
                outcome = self.run(resolved, verify=verify)
                if outcome == "READY":
                    return "READY"
                if outcome == "REVIEW_FINDINGS":
                    state = self.store.load_state()
                    if (state.review_retries >= max_review_retries and
                            state.current_phase and
                            self._is_verification_passed(state.current_phase)):
                        self._auto_pass_after_retries(state, max_review_retries)
                        return "READY"

    def verify(self, checks: list[tuple[str, str]]) -> bool:
        state = self.store.load_state()
        if not state.current_phase:
            raise TransitionError("there is no current phase")
        checks = self._resolve_checks(checks)
        state.state = "PHASE_VERIFYING"
        self.store.save_state(state)
        phase_id = state.current_phase
        results = []
        overall = True
        logs = []
        if not checks:
            message = (
                f"{TEST_MANIFEST_FILE} is missing or has no \"command\": the implementation "
                "agent must create automated tests and that manifest so the default "
                "verification can run them. Alternatively pass --check or register checks "
                "with init --check."
            )
            results.append({"name": "manifest", "status": "FAIL",
                            "command": f"read {TEST_MANIFEST_FILE}"})
            logs.append(message)
            overall = False
            verif_data = {
                "phase_id": phase_id, "run_id": state.current_run,
                "overall": "FAIL", "checks": results, "created_at": now_iso(),
            }
            self.store.write_json(f"evidence/{phase_id}-verification.json", verif_data)
            self.store.write_json(f"evidence/{phase_id}-run-{state.current_run:03d}-verification.json", verif_data)
            self.store.write_text(f"evidence/{phase_id}-verification.log", message)
            self.store.write_text(f"evidence/{phase_id}-run-{state.current_run:03d}-verification.log", message)
            state.state = "PHASE_RUNNING"
            self.store.save_state(state)
            return False
        for name, command in checks:
            completed = subprocess.run(command, shell=True, cwd=self.repository, text=True,
                                       capture_output=True, check=False)
            status = "PASS" if completed.returncode == 0 else "FAIL"
            overall &= completed.returncode == 0
            results.append({"name": name, "status": status, "command": command})
            logs.append(f"$ {command}\n{completed.stdout}{completed.stderr}")
        verif_data = {
            "phase_id": phase_id, "run_id": state.current_run,
            "overall": "PASS" if overall else "FAIL", "checks": results,
            "created_at": now_iso(),
        }
        verif_log = "\n".join(logs)
        self.store.write_json(f"evidence/{phase_id}-verification.json", verif_data)
        self.store.write_json(f"evidence/{phase_id}-run-{state.current_run:03d}-verification.json", verif_data)
        self.store.write_text(f"evidence/{phase_id}-verification.log", verif_log)
        self.store.write_text(f"evidence/{phase_id}-run-{state.current_run:03d}-verification.log", verif_log)
        state.state = "PHASE_REVIEWING" if overall else "PHASE_RUNNING"
        self.store.save_state(state)
        return overall

    def review(self) -> bool:
        state = self.store.load_state()
        if state.state != "PHASE_REVIEWING" or not state.current_phase:
            raise TransitionError("review requires successful verification")
        phase = self.store.load_phase(state.current_phase)
        issue = self.store.load_issue()
        try:
            result = self.agent.execute(self._review_prompt(issue, phase), self.repository)
        except AgentError as error:
            self._record_failure_usage("review", phase.id, state.current_run, error)
            raise
        self._record_usage("review", phase.id, state.current_run, self._combined_output(result))
        decision = result.data.get("result")
        findings = result.data.get("findings", [])
        if decision not in {"PASS", "FINDINGS"} or not isinstance(findings, list):
            raise ValueError("review result must contain result=PASS|FINDINGS and findings array")
        if decision == "FINDINGS" and not findings:
            raise ValueError("FINDINGS review must include at least one finding")
        if decision == "PASS" and findings:
            raise ValueError("PASS review must not contain findings")
        # Falsification by test: verify test_command for findings if provided
        if decision == "FINDINGS":
            tested_count = 0
            reproduced_count = 0
            for finding in findings:
                if isinstance(finding, dict) and "test_command" in finding:
                    cmd = finding["test_command"]
                    if isinstance(cmd, str) and cmd.strip():
                        tested_count += 1
                        completed = subprocess.run(
                            cmd, shell=True, cwd=self.repository, text=True,
                            capture_output=True, check=False,
                        )
                        # A reproduction test MUST fail (exit code != 0) to prove a problem exists
                        if completed.returncode != 0:
                            finding["test_verified"] = True
                            finding["test_output"] = completed.stdout + completed.stderr
                            reproduced_count += 1
                        else:
                            finding["test_verified"] = False
                            finding["test_output"] = completed.stdout + completed.stderr

            # If all findings provided test_commands and none of them reproduced (all passed):
            if tested_count > 0 and reproduced_count == 0:
                decision = "PASS"
                findings = []

        review_data = {
            "phase_id": phase.id, "run_id": state.current_run, "result": decision, "findings": findings,
            "created_at": now_iso(),
        }
        review_log = result.stdout + result.stderr
        self.store.write_json(f"evidence/{phase.id}-adversarial-review.json", review_data)
        self.store.write_json(f"evidence/{phase.id}-run-{state.current_run:03d}-adversarial-review.json", review_data)
        self.store.write_text(f"evidence/{phase.id}-review-agent.log", review_log)
        self.store.write_text(f"evidence/{phase.id}-run-{state.current_run:03d}-review-agent.log", review_log)
        if decision == "PASS":
            state.state = "AWAITING_HUMAN_CHECK"
            phase.status = "awaiting_human_check"
            state.review_retries = 0
        else:
            state.state = "PHASE_RUNNING"
            state.review_retries += 1
        self.store.save_phase(phase)
        self.store.save_state(state)
        self._generate_review_summary(issue, phase, state.current_run, decision, findings)
        return decision == "PASS"

    def _generate_review_summary(
        self, issue: Issue, phase: Phase, run_number: int,
        decision: str, findings: list[Any],
    ) -> None:
        """Create optional business-facing evidence without affecting review state."""
        path = f"evidence/{phase.id}-review-summary.json"
        run_path = f"evidence/{phase.id}-run-{run_number:03d}-review-summary.json"
        log_path = f"evidence/{phase.id}-review-summary-agent.log"
        run_log_path = f"evidence/{phase.id}-run-{run_number:03d}-review-summary-agent.log"
        try:
            pending_payload = {
                "phase_id": phase.id,
                "run_id": run_number,
                "status": "PENDING",
                "created_at": now_iso(),
            }
            self.store.write_json(path, pending_payload)
            self.store.write_json(run_path, pending_payload)
            result = self.agent.execute(
                self._review_summary_prompt(issue, phase, decision, findings),
                self.repository,
            )
            self._record_usage("review_summary", phase.id, run_number, result.stdout)
            summary = self._validate_review_summary(result.data, decision, len(findings))
            success_payload = {
                "phase_id": phase.id,
                "run_id": run_number,
                "status": "SUCCESS",
                **summary,
                "created_at": now_iso(),
            }
            self.store.write_json(path, success_payload)
            self.store.write_json(run_path, success_payload)
            summary_log = result.stdout + result.stderr
            self.store.write_text(log_path, summary_log)
            self.store.write_text(run_log_path, summary_log)
        except Exception as error:
            if isinstance(error, AgentError):
                self._record_failure_usage("review_summary", phase.id, run_number, error)
            failed_payload = {
                "phase_id": phase.id,
                "run_id": run_number,
                "status": "FAILED",
                "error": "要約を生成できませんでした",
                "created_at": now_iso(),
            }
            self.store.write_json(path, failed_payload)
            self.store.write_json(run_path, failed_payload)

    @staticmethod
    def _validate_review_summary(
        value: Any, decision: str, finding_count: int,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("review summary must be an object")
        expected_recommendation = "APPROVE" if decision == "PASS" else "REQUEST_CHANGES"
        if value.get("recommendation") != expected_recommendation:
            raise ValueError("review summary recommendation does not match review")
        for key in ("decision_reason", "affected_scope", "required_action"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise ValueError(f"review summary must include {key}")
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("review summary must include items")
        if decision == "PASS" and items:
            raise ValueError("approved review summary must not include finding items")
        covered: set[int] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("review summary items must be objects")
            finding_ids = item.get("finding_ids")
            if not isinstance(finding_ids, list) or not finding_ids:
                raise ValueError("review summary item must reference findings")
            if any(not isinstance(finding_id, int)
                   or isinstance(finding_id, bool)
                   or finding_id < 1 or finding_id > finding_count
                   for finding_id in finding_ids):
                raise ValueError("review summary item references an unknown finding")
            covered.update(finding_ids)
            for key in ("problem", "business_impact", "affected_scope", "required_action"):
                if not isinstance(item.get(key), str) or not item[key].strip():
                    raise ValueError(f"review summary item must include {key}")
        if decision == "FINDINGS" and covered != set(range(1, finding_count + 1)):
            raise ValueError("review summary must cover every finding")
        return {
            "recommendation": expected_recommendation,
            "decision_reason": value["decision_reason"].strip(),
            "affected_scope": value["affected_scope"].strip(),
            "required_action": value["required_action"].strip(),
            "items": items,
        }

    def human_check(self, result: str, feedback: str | None = None,
                    expected_revision: str | None = None) -> str:
        state = self.store.load_state()
        if expected_revision is not None and state.revision() != expected_revision:
            raise TransitionError("確認後に状態が更新されています。最新の結果を確認してください")
        if state.state != "AWAITING_HUMAN_CHECK" or not state.current_phase:
            raise TransitionError("human check is only allowed for a ready phase")
        if result not in {"ACCEPT", "FEEDBACK"}:
            raise ValueError("result must be ACCEPT or FEEDBACK")
        if result == "FEEDBACK" and not feedback:
            raise ValueError("feedback text is required for FEEDBACK")
        phase = self.store.load_phase(state.current_phase)
        feedback_payload = {
            "phase_id": phase.id, "run_id": state.current_run, "result": result,
            "feedback": feedback if result == "FEEDBACK" else None, "created_at": now_iso(),
        }
        self.store.write_json(f"feedback/{phase.id}-{now_iso().replace(':', '')}.json", feedback_payload)
        self.store.write_json(f"feedback/{phase.id}-run-{state.current_run:03d}.json", feedback_payload)
        if result == "FEEDBACK":
            state.state = "PHASE_FIXING"
            state.review_retries = 0
            phase.status = "running"
            self.store.save_phase(phase)
            self.store.save_state(state)
            return "PHASE_FIXING"
        phase.status = "accepted"
        self.store.save_phase(phase)
        issue = self.store.load_issue()
        index = issue.phase_order.index(phase.id)
        if index + 1 == len(issue.phase_order):
            state.state, state.current_phase, state.review_retries = "RESOLVED", None, 0
            self.store.save_state(state)
            try:
                self._update_changelog(issue)
            except Exception:
                pass
        else:
            next_phase = self.store.load_phase(issue.phase_order[index + 1])
            next_phase.status = "running"
            self.store.save_phase(next_phase)
            state.state, state.current_phase, state.current_run, state.review_retries = "PHASE_RUNNING", next_phase.id, 0, 0
            self.store.save_state(state)
        return state.state

    def _phase_context(self, phase: Phase) -> str:
        checkpoints = sorted((self.store.path / "runs").glob(f"{phase.id}-run-*.md"),
                             key=lambda path: int(path.stem.rsplit("-", 1)[1]))
        paths = checkpoints[-1:]
        paths.extend(self.store.path / "evidence" / f"{phase.id}-{suffix}" for suffix in (
            "verification.json", "verification.log", "adversarial-review.json"))
        return "\n\n".join(f"{path.name}:\n{path.read_text(encoding='utf-8')}"
                            for path in paths if path.exists()) or "none"

    def _resolved_issues_context(self) -> str:
        project_store = getattr(self.store, "project_store", None)
        if not project_store:
            return "none"
        resolved = []
        current_id = getattr(self.store, "issue_id", "")
        for other_id in project_store.list_issue_ids():
            if other_id == current_id:
                continue
            try:
                istore = project_store.issue_store(other_id)
                st = istore.load_state()
                if st.state in {"RESOLVED", "COMPLETED"}:
                    iss = istore.load_issue()
                    phases_summary = []
                    for pid in iss.phase_order:
                        try:
                            p = istore.load_phase(pid)
                            phases_summary.append(f"  * Phase {p.id}: {p.goal} (status: {p.status})")
                        except Exception:
                            pass
                    entry = f"- Issue {other_id}: {iss.description}"
                    if phases_summary:
                        entry += "\n" + "\n".join(phases_summary)
                    resolved.append(entry)
            except Exception:
                continue
        return "\n".join(resolved) if resolved else "none"

    def _run_prompt(self, issue: Issue, phase: Phase, state: PipelineState,
                    checks: list[tuple[str, str]]) -> str:
        feedback_files = sorted((self.store.path / "feedback").glob(f"{phase.id}-*.json"))
        feedback = feedback_files[-1].read_text(encoding="utf-8") if feedback_files else "none"
        accepted = [self.store.load_phase(phase_id).as_dict()
                    for phase_id in issue.phase_order[:phase.sequence - 1]]
        return render_prompt(
            "run",
            repository=str(self.repository),
            description=issue.description,
            resolved_issues=self._resolved_issues_context(),
            phase_goal=phase.goal,
            scope=str(phase.scope),
            acceptance_criteria=str(phase.acceptance_criteria),
            feedback=feedback,
            accepted_phases=str(accepted),
            current_run=str(state.current_run),
            checks=str(checks),
            context=self._phase_context(phase),
            manifest_file=TEST_MANIFEST_FILE,
        )

    def _review_prompt(self, issue: Issue, phase: Phase) -> str:
        return render_prompt(
            "review",
            repository=str(self.repository),
            description=issue.description,
            resolved_issues=self._resolved_issues_context(),
            phase_goal=phase.goal,
            scope=str(phase.scope),
            acceptance_criteria=str(phase.acceptance_criteria),
            context=self._phase_context(phase),
            manifest_file=TEST_MANIFEST_FILE,
        )

    def _review_summary_prompt(
        self, issue: Issue, phase: Phase, decision: str, findings: list[Any],
    ) -> str:
        numbered_findings = [
            {"id": index, **finding} if isinstance(finding, dict)
            else {"id": index, "description": str(finding)}
            for index, finding in enumerate(findings, 1)
        ]
        return render_prompt(
            "review-summary",
            description=issue.description,
            phase_goal=phase.goal,
            acceptance_criteria=json.dumps(phase.acceptance_criteria, ensure_ascii=False),
            review_result=decision,
            findings=json.dumps(numbered_findings, ensure_ascii=False),
        )

    def _token_usage_format(self) -> str:
        return getattr(self.agent, "token_usage_format", DEFAULT_TOKEN_USAGE_FORMAT)

    def _record_usage(self, stage: str, phase_id: str, run_number: int,
                      output: str) -> None:
        usage = extract_token_usage(output, self._token_usage_format())
        allocation = collect_agent_allocation(self.agent)
        if usage is None and allocation is None:
            return
        event = {
            "stage": stage, "phase": phase_id, "run": run_number,
            "recorded_at": now_iso(),
        }
        if usage is not None:
            event["usage"] = usage
        if allocation is not None:
            event["allocation_usage"] = allocation
        self.store.record_usage_event(event)

    @staticmethod
    def _combined_output(result: AgentResult) -> str:
        if result.stderr.strip():
            return f"{result.stdout}\n{result.stderr}"
        return result.stdout

    def _record_failure_usage(self, stage: str, phase_id: str, run_number: int,
                              error: AgentError) -> None:
        outputs = []
        if error.log_path is None:
            paths = []
        else:
            prefix = error.log_path.name.rsplit("-", 1)[0]
            paths = sorted(
                error.log_path.parent.glob(f"{prefix}-*.json"),
                key=lambda path: int(path.name.rsplit("-", 1)[1].rsplit(".", 1)[0]),
            )
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            stdout = payload.get("stdout") if isinstance(payload, dict) else None
            if isinstance(stdout, str) and stdout:
                outputs.append(stdout)
            stderr = payload.get("stderr") if isinstance(payload, dict) else None
            if isinstance(stderr, str) and stderr:
                outputs.append(stderr)
        self._record_usage(stage, phase_id, run_number, "\n".join(outputs))

    @staticmethod
    def _checkpoint(phase: Phase, outcome: RunOutcome) -> str:
        bullets = lambda values: "\n".join(f"- {value}" for value in values) or "- None"
        return f"""# Run Checkpoint
## Phase
{phase.id}
## Goal
{phase.goal}
## Completed
{bullets(outcome.completed)}
## Remaining
{bullets(outcome.remaining)}
## Current Problems
{bullets(outcome.problems)}
## Verification
- Phase verification: NOT RUN
## Recommended Next Action
{outcome.recommended_next_action}
"""

    def _changelog_prompt(self, issue: Issue, date_str: str) -> str:
        phases = [self.store.load_phase(pid) for pid in issue.phase_order]
        phases_context_lines = []
        for phase in phases:
            phases_context_lines.append(f"Phase {phase.id}: {phase.goal}")
            if phase.acceptance_criteria:
                phases_context_lines.append(f"  受入条件: {phase.acceptance_criteria}")
            checkpoints = sorted((self.store.path / "runs").glob(f"{phase.id}-run-*.md"),
                                 key=lambda p: int(p.stem.rsplit("-", 1)[1]))
            if checkpoints:
                try:
                    phases_context_lines.append(f"  チェックポイント:\n{checkpoints[-1].read_text(encoding='utf-8')}")
                except Exception:
                    pass
        phases_context = "\n\n".join(phases_context_lines) or "none"
        return render_prompt(
            "changelog",
            issue_id=issue.id or "ISSUE",
            description=issue.description,
            phases_context=phases_context,
            date=date_str,
        )

    @staticmethod
    def _validate_changelog(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("changelog data must be an object")
        title = data.get("title")
        summary = data.get("summary")
        changes = data.get("changes")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("changelog title must be a non-empty string")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("changelog summary must be a non-empty string")
        if not isinstance(changes, list) or not changes:
            raise ValueError("changelog changes must be a non-empty array")
        clean_changes = [str(c).strip() for c in changes if str(c).strip()]
        if not clean_changes:
            raise ValueError("changelog changes must have at least one valid item")
        return {
            "title": title.strip(),
            "summary": summary.strip(),
            "changes": clean_changes,
        }

    def _update_changelog(self, issue: Issue) -> dict[str, Any]:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        title = issue.description
        summary = f"{issue.description} の実装が完了しました。"
        phases = [self.store.load_phase(pid) for pid in issue.phase_order]
        changes = [f"{p.id}: {p.goal}" for p in phases]
        status = "SUCCESS"
        agent_log = ""

        try:
            prompt = self._changelog_prompt(issue, date_str)
            result = self.agent.execute(prompt, self.repository)
            agent_log = result.stdout + (f"\n{result.stderr}" if result.stderr.strip() else "")
            self._record_usage("changelog", "", 0, agent_log)
            validated = self._validate_changelog(result.data)
            title = validated["title"]
            summary = validated["summary"]
            changes = validated["changes"]
        except Exception as error:
            status = "FALLBACK"
            if isinstance(error, AgentError):
                self._record_failure_usage("changelog", "", 0, error)
            agent_log = f"Agent execution failed: {error}\nUsing fallback changelog."

        entry_markdown = format_changelog_entry(issue.id, title, date_str, summary, changes)
        changelog_path = self.repository / "CHANGELOG.md"

        try:
            original_content = changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else ""
            new_content = update_changelog_markdown(original_content, entry_markdown, issue.id)
            changelog_path.write_text(new_content, encoding="utf-8")
        except Exception as file_error:
            agent_log += f"\nFailed to write CHANGELOG.md: {file_error}"

        payload = {
            "issue_id": issue.id,
            "title": title,
            "date": date_str,
            "summary": summary,
            "changes": changes,
            "markdown": entry_markdown,
            "status": status,
            "created_at": now_iso(),
        }
        try:
            self.store.write_json("evidence/changelog.json", payload)
            self.store.write_text("evidence/changelog.md", entry_markdown)
            if agent_log:
                self.store.write_text("evidence/changelog-agent.log", agent_log)
        except Exception:
            pass

        return payload

    def generate_changelog(self) -> dict[str, Any]:
        issue = self.store.load_issue()
        return self._update_changelog(issue)

