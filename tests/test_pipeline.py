from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess

from dev_pipeline.agents import AgentResult, CommandAgent
from dev_pipeline.pipeline import Pipeline, TransitionError
from dev_pipeline.store import WorkStore


PHASES = [
    {"goal": "usable feature", "scope": ["implementation"], "acceptance_criteria": ["tests pass"]},
    {"goal": "polish", "scope": ["documentation"], "acceptance_criteria": ["documented"]},
]


class FakeAgent:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.prompts = []

    def execute(self, prompt, repository):
        self.prompts.append(prompt)
        data = next(self.replies)
        return AgentResult(data, json.dumps(data), "")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorkStore(self.root / "work", "INIT-001")
        Pipeline.initialize(self.store, "ship", ["works"], [], PHASES)

    def tearDown(self):
        self.temp.cleanup()

    def test_initial_state_starts_phase_without_approval(self):
        state = self.store.load_state()
        self.assertEqual("PHASE_RUNNING", state.state)
        self.assertEqual("running", self.store.load_phase("phase-01").status)
        self.assertEqual("pending", self.store.load_phase("phase-02").status)

    def test_continue_writes_checkpoint(self):
        agent = FakeAgent([{"result": "CONTINUE", "completed": ["core"], "remaining": ["UI"],
                            "problems": [], "recommended_next_action": "build UI"}])
        result = Pipeline(self.store, self.root, agent).run([])
        self.assertEqual("CONTINUE", result)
        checkpoint = (self.store.path / "runs/phase-01-run-001.md").read_text()
        self.assertIn("- core", checkpoint)
        self.assertIn("build UI", checkpoint)

    def test_complete_verify_review_and_accept(self):
        replies = [
            {"result": "GOAL_REACHED", "completed": ["all"], "remaining": [], "problems": [],
             "recommended_next_action": "verify"},
            {"result": "PASS", "findings": []},
        ]
        pipeline = Pipeline(self.store, self.root, FakeAgent(replies))
        self.assertEqual("READY", pipeline.run([("python", "python -c 'print(1)'")]))
        self.assertEqual("AWAITING_HUMAN_CHECK", self.store.load_state().state)
        self.assertEqual("PHASE_RUNNING", pipeline.human_check("ACCEPT"))
        self.assertEqual("phase-02", self.store.load_state().current_phase)

    def test_failed_verification_returns_to_run(self):
        reply = {"result": "GOAL_REACHED", "completed": [], "remaining": [], "problems": [],
                 "recommended_next_action": "verify"}
        pipeline = Pipeline(self.store, self.root, FakeAgent([reply]))
        self.assertEqual("VERIFICATION_FAILED", pipeline.run([("false", "python -c 'raise SystemExit(1)'")]))
        self.assertEqual("PHASE_RUNNING", self.store.load_state().state)

    def test_findings_return_to_execution(self):
        agent = FakeAgent([{"result": "FINDINGS", "findings": [{"severity": "high",
                           "description": "broken", "recommendation": "fix"}]}])
        pipeline = Pipeline(self.store, self.root, agent)
        self.store.load_state()
        pipeline.verify([("ok", "python -c 'print(1)'")])
        self.assertFalse(pipeline.review())
        self.assertEqual("PHASE_RUNNING", self.store.load_state().state)

    def test_feedback_starts_fix_run(self):
        state = self.store.load_state()
        state.state = "AWAITING_HUMAN_CHECK"
        self.store.save_state(state)
        pipeline = Pipeline(self.store, self.root, FakeAgent([]))
        self.assertEqual("PHASE_FIXING", pipeline.human_check("FEEDBACK", "keep search filters"))
        self.assertTrue(list((self.store.path / "feedback").glob("*.json")))

    def test_cannot_accept_running_phase(self):
        with self.assertRaises(TransitionError):
            Pipeline(self.store, self.root, FakeAgent([])).human_check("ACCEPT")

    def test_until_ready_continues_and_passes_findings_to_fix_run(self):
        outcome = {"result": "GOAL_REACHED", "completed": ["implementation"],
                   "remaining": [], "problems": [], "recommended_next_action": "verify"}
        finding = {"severity": "high", "description": "missing behavior",
                   "recommendation": "repair behavior"}
        agent = FakeAgent([
            dict(outcome, result="CONTINUE", remaining=["finish feature"]),
            outcome, {"result": "FINDINGS", "findings": [finding]},
            outcome, {"result": "PASS", "findings": []},
        ])
        pipeline = Pipeline(self.store, self.root, agent)
        self.assertEqual("READY", pipeline.run_until_ready([("test", "python -c 'print(1)'")]))
        self.assertIn("finish feature", agent.prompts[1])
        self.assertIn("missing behavior", agent.prompts[3])
        self.assertIn("repair behavior", agent.prompts[3])
        self.assertIn("verification.log", agent.prompts[3])
        self.assertEqual(3, self.store.load_state().current_run)
        self.assertEqual("AWAITING_HUMAN_CHECK", self.store.load_state().state)
        self.assertEqual("pending", self.store.load_phase("phase-02").status)

    def test_until_ready_repairs_verification_failure(self):
        outcome = {"result": "GOAL_REACHED", "completed": [], "remaining": [],
                   "problems": [], "recommended_next_action": "verify"}
        agent = FakeAgent([outcome, outcome, {"result": "PASS", "findings": []}])
        pipeline = Pipeline(self.store, self.root, agent)
        with patch("dev_pipeline.pipeline.subprocess.run", side_effect=[
            CompletedProcess("check", 1, "", "regression detail"),
            CompletedProcess("check", 0, "fixed", ""),
        ]):
            self.assertEqual("READY", pipeline.run_until_ready([("test", "project verify")]))
        self.assertIn("project verify", agent.prompts[0])
        self.assertIn("regression detail", agent.prompts[1])
        self.assertIn('"overall": "PASS"', agent.prompts[2])

    def test_until_ready_resumes_review_without_implementation(self):
        pipeline = Pipeline(self.store, self.root, FakeAgent([{"result": "PASS", "findings": []}]))
        pipeline.verify([("test", "python -c 'print(1)'")])
        self.assertEqual("READY", pipeline.run_until_ready([("test", "unused")]))
        self.assertEqual(0, self.store.load_state().current_run)
        self.assertEqual("READY", pipeline.run_until_ready([("test", "unused")]))

    def test_until_ready_requires_checks_before_agent_execution(self):
        pipeline = Pipeline(self.store, self.root, FakeAgent([]))
        with self.assertRaises(TransitionError):
            pipeline.run_until_ready([])
        self.assertEqual(0, self.store.load_state().current_run)

    def test_accept_last_phase_completes_initiative(self):
        state = self.store.load_state()
        state.state = "AWAITING_HUMAN_CHECK"
        state.current_phase = "phase-02"
        self.store.save_state(state)
        pipeline = Pipeline(self.store, self.root, FakeAgent([]))
        self.assertEqual("COMPLETED", pipeline.human_check("ACCEPT"))
        self.assertIsNone(self.store.load_state().current_phase)
        self.assertEqual("accepted", self.store.load_phase("phase-02").status)

    def test_fix_prompt_includes_human_feedback_and_accepted_phases(self):
        state = self.store.load_state()
        state.state = "AWAITING_HUMAN_CHECK"
        self.store.save_state(state)
        agent = FakeAgent([{"result": "CONTINUE", "completed": [], "remaining": [],
                            "problems": [], "recommended_next_action": "continue"}])
        pipeline = Pipeline(self.store, self.root, agent)
        pipeline.human_check("ACCEPT")
        state = self.store.load_state()
        state.state = "AWAITING_HUMAN_CHECK"
        self.store.save_state(state)
        pipeline.human_check("FEEDBACK", "preserve existing behavior")
        pipeline.run([])
        self.assertIn("preserve existing behavior", agent.prompts[0])
        self.assertIn("usable feature", agent.prompts[0])
        self.assertIn("'status': 'accepted'", agent.prompts[0])


class AgentParsingTests(unittest.TestCase):
    def test_codex_command_uses_workspace_write(self):
        agent = CommandAgent("codex")
        with patch("dev_pipeline.agents.subprocess.run",
                   return_value=CompletedProcess("codex", 0, '{"result":"PASS"}', "")) as run:
            agent.execute("prompt", Path("."))
        self.assertEqual(["codex", "exec", "--sandbox", "workspace-write", "-"],
                         run.call_args.args[0])

    def test_extracts_final_json_after_noisy_output(self):
        self.assertEqual({"result": "PASS"}, CommandAgent._extract_json("thinking\n{\"result\":\"PASS\"}\n"))


if __name__ == "__main__":
    unittest.main()
