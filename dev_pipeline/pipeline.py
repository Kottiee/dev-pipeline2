from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .agents.base import Agent
from .models import Initiative, Phase, PipelineState, RunOutcome, now_iso
from .store import WorkStore


class TransitionError(RuntimeError):
    pass


class Pipeline:
    def __init__(self, store: WorkStore, repository: Path, agent: Agent):
        self.store = store
        self.repository = repository.resolve()
        self.agent = agent

    @staticmethod
    def initialize(
        store: WorkStore, goal: str, requirements: list[str], constraints: list[str],
        phase_specs: list[dict[str, Any]],
    ) -> None:
        phases = [Phase(id=f"phase-{index:02d}", sequence=index, **spec)
                  for index, spec in enumerate(phase_specs, 1)]
        initiative = Initiative(store.initiative_id, goal, requirements, constraints,
                                [phase.id for phase in phases])
        store.create(initiative, phases)

    def run(self, checks: list[tuple[str, str]]) -> str:
        state = self.store.load_state()
        if state.state not in {"PHASE_RUNNING", "PHASE_FIXING"} or not state.current_phase:
            raise TransitionError(f"cannot run agent while state is {state.state}")
        state.current_run += 1
        self.store.save_state(state)
        initiative = self.store.load_initiative()
        phase = self.store.load_phase(state.current_phase)
        prompt = self._run_prompt(initiative, phase, state, checks)
        result = self.agent.execute(prompt, self.repository)
        outcome = RunOutcome.from_dict(result.data)
        run_id = f"run-{state.current_run:03d}"
        self.store.write_text(f"runs/{phase.id}-{run_id}.md", self._checkpoint(phase, outcome))
        self.store.write_text(f"evidence/{phase.id}-{run_id}-agent.log", result.stdout + result.stderr)
        if outcome.result == "CONTINUE":
            return "CONTINUE"
        if not checks:
            raise TransitionError("at least one verification check is required before review")
        if not self.verify(checks):
            return "VERIFICATION_FAILED"
        return "READY" if self.review() else "REVIEW_FINDINGS"

    def run_until_ready(self, checks: list[tuple[str, str]]) -> str:
        if not checks:
            raise TransitionError("at least one verification check is required before review")
        while True:
            state = self.store.load_state()
            if state.state == "AWAITING_HUMAN_CHECK":
                return "READY"
            if state.state == "PHASE_VERIFYING":
                self.verify(checks)
            elif state.state == "PHASE_REVIEWING":
                self.review()
            elif self.run(checks) == "READY":
                return "READY"

    def verify(self, checks: list[tuple[str, str]]) -> bool:
        state = self.store.load_state()
        if not state.current_phase:
            raise TransitionError("there is no current phase")
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
        result = self.agent.execute(self._review_prompt(initiative, phase), self.repository)
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
        return f"""You are the implementation agent for an autonomous development pipeline.
Work directly in repository: {self.repository}
Run commands with this repository as their working directory. Keep temporary verification
files inside this repository and clean up your own temporary files afterwards.
Initiative goal: {initiative.goal}
Requirements: {initiative.requirements}
Constraints: {initiative.constraints}
Phase: {phase.goal}
Scope: {phase.scope}
Acceptance criteria: {phase.acceptance_criteria}
Latest human feedback: {feedback}
Previously accepted phases: {accepted}
Current run: {state.current_run}
Verification commands (run in the repository): {checks}
Current progress and latest evidence (may refer to an earlier run):
{self._phase_context(phase)}

Inspect the current repository, implement and test as much as possible in this run. Do not ask
the human about machine-checkable issues. At the very end, output exactly one JSON object (and
nothing after it) with keys: result (CONTINUE or GOAL_REACHED), completed (string array),
remaining (string array), problems (string array), recommended_next_action (string).
Choose GOAL_REACHED only when every acceptance criterion is implemented and locally checked.
"""

    def _review_prompt(self, initiative: Initiative, phase: Phase) -> str:
        return f"""Act as an adversarial reviewer, independent from the implementer. Inspect the
repository at {self.repository} and find reasons to reject this phase. Do not modify source files.
Run verification commands from this repository. If temporary test inputs are needed, create
them inside this repository and clean up only those temporary inputs afterwards.
Initiative goal: {initiative.goal}
Requirements: {initiative.requirements}
Constraints: {initiative.constraints}
Phase goal: {phase.goal}
Scope: {phase.scope}
Acceptance criteria: {phase.acceptance_criteria}
Current progress and verification evidence:
{self._phase_context(phase)}
Output exactly one JSON object and nothing after it. Schema:
{{"result":"PASS"|"FINDINGS","findings":[{{"severity":"high|medium|low","description":"...","recommendation":"..."}}]}}
PASS is allowed only when there are no findings.
Report concrete acceptance failures or regressions, not optional features outside this phase.
Use valid JSON string escaping for quotes, backslashes, and newlines in descriptions.
"""

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
