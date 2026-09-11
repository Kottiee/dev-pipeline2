from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .agents.base import Agent, AgentError
from .allocation import collect_agent_allocation
from .models import Issue, Phase, PipelineState, RunOutcome, now_iso
from .prompts import render_prompt
from .store import IssueStore, WorkStore
from .usage import DEFAULT_TOKEN_USAGE_FORMAT, extract_token_usage


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
        store: IssueStore, goal: str, requirements: list[str], constraints: list[str],
        phase_specs: list[dict[str, Any]],
        checks: list[tuple[str, str]] | None = None,
    ) -> None:
        phases = [Phase(id=f"phase-{index:02d}", sequence=index, **spec)
                  for index, spec in enumerate(phase_specs, 1)]
        issue_id = getattr(store, "issue_id", getattr(store, "initiative_id", ""))
        issue = Issue(issue_id, goal, requirements, constraints,
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

    def run_until_ready(self, checks: list[tuple[str, str]], verify: bool = True) -> str:
        resolved = self._resolve_checks(checks)
        while True:
            state = self.store.load_state()
            if state.state == "AWAITING_HUMAN_CHECK":
                return "READY"
            if state.state == "PHASE_VERIFYING":
                self.verify(resolved)
            elif state.state == "PHASE_REVIEWING":
                self.review()
            elif self.run(resolved, verify=verify) == "READY":
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
            self.store.write_json(f"evidence/{phase_id}-verification.json", {
                "phase_id": phase_id, "run_id": state.current_run,
                "overall": "FAIL", "checks": results, "created_at": now_iso(),
            })
            self.store.write_text(f"evidence/{phase_id}-verification.log", message)
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
        self.store.write_json(f"evidence/{phase_id}-verification.json", {
            "phase_id": phase_id, "run_id": state.current_run,
            "overall": "PASS" if overall else "FAIL", "checks": results,
            "created_at": now_iso(),
        })
        self.store.write_text(f"evidence/{phase_id}-verification.log", "\n".join(logs))
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
        self.store.write_json(f"evidence/{phase.id}-adversarial-review.json", {
            "phase_id": phase.id, "run_id": state.current_run, "result": decision, "findings": findings,
            "created_at": now_iso(),
        })
        self.store.write_text(f"evidence/{phase.id}-review-agent.log", result.stdout + result.stderr)
        if decision == "PASS":
            state.state = "AWAITING_HUMAN_CHECK"
            phase.status = "awaiting_human_check"
        else:
            state.state = "PHASE_RUNNING"
        self.store.save_phase(phase)
        self.store.save_state(state)
        return decision == "PASS"

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
        self.store.write_json(f"feedback/{phase.id}-{now_iso().replace(':', '')}.json", {
            "phase_id": phase.id, "result": result,
            "feedback": feedback if result == "FEEDBACK" else None, "created_at": now_iso(),
        })
        if result == "FEEDBACK":
            state.state = "PHASE_FIXING"
            phase.status = "running"
            self.store.save_phase(phase)
            self.store.save_state(state)
            return "PHASE_FIXING"
        phase.status = "accepted"
        self.store.save_phase(phase)
        issue = self.store.load_issue()
        index = issue.phase_order.index(phase.id)
        if index + 1 == len(issue.phase_order):
            state.state, state.current_phase = "RESOLVED", None
        else:
            next_phase = self.store.load_phase(issue.phase_order[index + 1])
            next_phase.status = "running"
            self.store.save_phase(next_phase)
            state.state, state.current_phase, state.current_run = "PHASE_RUNNING", next_phase.id, 0
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
                    entry = f"- Issue {other_id}: {iss.goal}"
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
            goal=issue.goal,
            requirements=str(issue.requirements),
            constraints=str(issue.constraints),
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
            goal=issue.goal,
            requirements=str(issue.requirements),
            constraints=str(issue.constraints),
            resolved_issues=self._resolved_issues_context(),
            phase_goal=phase.goal,
            scope=str(phase.scope),
            acceptance_criteria=str(phase.acceptance_criteria),
            context=self._phase_context(phase),
            manifest_file=TEST_MANIFEST_FILE,
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
