from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .agents.base import Agent, AgentError
from .models import Initiative, Phase, PipelineState, RunOutcome, now_iso
from .prompts import render_prompt
from .store import WorkStore
from .usage import DEFAULT_TOKEN_USAGE_FORMAT, extract_token_usage


class TransitionError(RuntimeError):
    pass


# Used when neither --check nor registered (init --check) checks are available.
DEFAULT_CHECKS = [("test", "python3 -m unittest discover -s tests -v")]


class Pipeline:
    def __init__(self, store: WorkStore, repository: Path, agent: Agent):
        self.store = store
        self.repository = repository.resolve()
        self.agent = agent

    @staticmethod
    def initialize(
        store: WorkStore, goal: str, requirements: list[str], constraints: list[str],
        phase_specs: list[dict[str, Any]],
        checks: list[tuple[str, str]] | None = None,
    ) -> None:
        phases = [Phase(id=f"phase-{index:02d}", sequence=index, **spec)
                  for index, spec in enumerate(phase_specs, 1)]
        initiative = Initiative(store.initiative_id, goal, requirements, constraints,
                                [phase.id for phase in phases],
                                checks=[tuple(check) for check in (checks or [])])
        store.create(initiative, phases)

    def _resolve_checks(self, checks: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """--check overrides registered checks; both default to the built-in test run."""
        if checks:
            return [tuple(check) for check in checks]
        stored = self.store.load_initiative().checks
        if stored:
            return [tuple(check) for check in stored]
        return [tuple(check) for check in DEFAULT_CHECKS]

    def run(self, checks: list[tuple[str, str]], verify: bool = True) -> str:
        state = self.store.load_state()
        if state.state not in {"PHASE_RUNNING", "PHASE_FIXING"} or not state.current_phase:
            raise TransitionError(f"cannot run agent while state is {state.state}")
        resolved = self._resolve_checks(checks)
        state.current_run += 1
        self.store.save_state(state)
        initiative = self.store.load_initiative()
        phase = self.store.load_phase(state.current_phase)
        prompt = self._run_prompt(initiative, phase, state, resolved if verify else [])
        try:
            result = self.agent.execute(prompt, self.repository)
        except AgentError as error:
            self._record_failure_usage("run", phase.id, state.current_run, error)
            raise
        self._record_usage("run", phase.id, state.current_run, result.stdout)
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
        if not self.verify(resolved):
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
        results = []
        overall = True
        logs = []
        for name, command in checks:
            completed = subprocess.run(command, shell=True, cwd=self.repository, text=True,
                                       capture_output=True, check=False)
            status = "PASS" if completed.returncode == 0 else "FAIL"
            overall &= completed.returncode == 0
            results.append({"name": name, "status": status, "command": command})
            logs.append(f"$ {command}\n{completed.stdout}{completed.stderr}")
        phase_id = state.current_phase
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
        initiative = self.store.load_initiative()
        try:
            result = self.agent.execute(self._review_prompt(initiative, phase), self.repository)
        except AgentError as error:
            self._record_failure_usage("review", phase.id, state.current_run, error)
            raise
        self._record_usage("review", phase.id, state.current_run, result.stdout)
        decision = result.data.get("result")
        findings = result.data.get("findings", [])
        if decision not in {"PASS", "FINDINGS"} or not isinstance(findings, list):
            raise ValueError("review result must contain result=PASS|FINDINGS and findings array")
        if decision == "FINDINGS" and not findings:
            raise ValueError("FINDINGS review must include at least one finding")
        if decision == "PASS" and findings:
            raise ValueError("PASS review must not contain findings")
        self.store.write_json(f"evidence/{phase.id}-adversarial-review.json", {
            "phase_id": phase.id, "result": decision, "findings": findings,
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

    def human_check(self, result: str, feedback: str | None = None) -> str:
        state = self.store.load_state()
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
        initiative = self.store.load_initiative()
        index = initiative.phase_order.index(phase.id)
        if index + 1 == len(initiative.phase_order):
            state.state, state.current_phase = "COMPLETED", None
        else:
            next_phase = self.store.load_phase(initiative.phase_order[index + 1])
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

    def _run_prompt(self, initiative: Initiative, phase: Phase, state: PipelineState,
                    checks: list[tuple[str, str]]) -> str:
        feedback_files = sorted((self.store.path / "feedback").glob(f"{phase.id}-*.json"))
        feedback = feedback_files[-1].read_text(encoding="utf-8") if feedback_files else "none"
        accepted = [self.store.load_phase(phase_id).as_dict()
                    for phase_id in initiative.phase_order[:phase.sequence - 1]]
        return render_prompt(
            "run",
            repository=str(self.repository),
            goal=initiative.goal,
            requirements=str(initiative.requirements),
            constraints=str(initiative.constraints),
            phase_goal=phase.goal,
            scope=str(phase.scope),
            acceptance_criteria=str(phase.acceptance_criteria),
            feedback=feedback,
            accepted_phases=str(accepted),
            current_run=str(state.current_run),
            checks=str(checks),
            context=self._phase_context(phase),
        )

    def _review_prompt(self, initiative: Initiative, phase: Phase) -> str:
        return render_prompt(
            "review",
            repository=str(self.repository),
            goal=initiative.goal,
            requirements=str(initiative.requirements),
            constraints=str(initiative.constraints),
            phase_goal=phase.goal,
            scope=str(phase.scope),
            acceptance_criteria=str(phase.acceptance_criteria),
            context=self._phase_context(phase),
        )

    def _token_usage_format(self) -> str:
        return getattr(self.agent, "token_usage_format", DEFAULT_TOKEN_USAGE_FORMAT)

    def _record_usage(self, stage: str, phase_id: str, run_number: int,
                      output: str) -> None:
        usage = extract_token_usage(output, self._token_usage_format())
        if usage is None:
            return
        self.store.record_usage_event({
            "stage": stage, "phase": phase_id, "run": run_number,
            "usage": usage, "recorded_at": now_iso(),
        })

    def _record_failure_usage(self, stage: str, phase_id: str, run_number: int,
                              error: AgentError) -> None:
        if error.log_path is None:
            return
        prefix = error.log_path.name.rsplit("-", 1)[0]
        outputs = []
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
        if outputs:
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
