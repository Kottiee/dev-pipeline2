from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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

    def execute(self, prompt, repository):
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


class AgentParsingTests(unittest.TestCase):
    def test_extracts_final_json_after_noisy_output(self):
        self.assertEqual({"result": "PASS"}, CommandAgent._extract_json("thinking\n{\"result\":\"PASS\"}\n"))


if __name__ == "__main__":
    unittest.main()

