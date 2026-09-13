from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess

from dev_pipeline.agents import AgentResult, CodexAgent
from dev_pipeline.agents.base import AgentError, extract_json
from dev_pipeline.cli import main
from dev_pipeline.pipeline import Pipeline, TransitionError
from dev_pipeline.store import WorkStore


PHASES = [
    {"goal": "usable feature", "scope": ["implementation"], "acceptance_criteria": ["tests pass"]},
    {"goal": "polish", "scope": ["documentation"], "acceptance_criteria": ["documented"]},
]


class FakeAgent:
    def __init__(self, replies, summary_replies=None):
        self.replies = iter(replies)
        self.summary_replies = iter(summary_replies) if summary_replies is not None else None
        self.prompts = []

    def execute(self, prompt, repository):
        self.prompts.append(prompt)
        if prompt.startswith("Create a concise Japanese summary"):
            if self.summary_replies is not None:
                data = next(self.summary_replies)
                if isinstance(data, Exception):
                    raise data
            else:
                findings = list(range(1, prompt.count('"id":') + 1))
                data = {
                    "recommendation": "REQUEST_CHANGES" if findings else "APPROVE",
                    "decision_reason": "修正要否を判断できます",
                    "affected_scope": "対象フェーズの操作とデータ",
                    "required_action": "指摘を確認して判断してください",
                    "items": [{
                        "finding_ids": findings,
                        "problem": "期待した結果になりません",
                        "business_impact": "業務を完了できない可能性があります",
                        "affected_scope": "対象の操作とデータ",
                        "required_action": "修正を依頼してください",
                    }] if findings else [],
                }
        else:
            data = next(self.replies)
        return AgentResult(data, json.dumps(data), "")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorkStore(self.root / "work", "INIT-001")
        Pipeline.initialize(self.store, "ship", PHASES)

    def tearDown(self):
        self.temp.cleanup()

    def test_initial_state_starts_phase_without_approval(self):
        state = self.store.load_state()
        self.assertEqual("PHASE_RUNNING", state.state)
        self.assertEqual("running", self.store.load_phase("phase-01").status)
        self.assertEqual("pending", self.store.load_phase("phase-02").status)

    def test_init_with_prepared_repository(self):
        store = WorkStore(self.root / "work", "NEW-001")
        repo = store.path / "repo"
        repo.mkdir(parents=True)
        source = repo / "existing.txt"
        source.write_text("keep this", encoding="utf-8")
        plan = {"description": "ship", "phases": PHASES}
        with patch("dev_pipeline.agents.codex.CodexAgent.execute",
                   return_value=AgentResult(plan, json.dumps(plan), "")) as execute, \
             patch("dev_pipeline.cli.collect_agent_allocation", return_value=None):
            with patch("builtins.print"):
                result = main(["--work-dir", str(store.root), "--repo", str(repo),
                               "--agent", "codex", "init", "--plan-only", "--id", store.issue_id,
                               "--request", "ship a usable feature"])
        self.assertEqual(0, result)
        self.assertEqual(repo, execute.call_args.args[1])
        self.assertEqual("PHASE_RUNNING", store.load_state().state)
        self.assertEqual("keep this", source.read_text(encoding="utf-8"))

    def test_initialize_does_not_overwrite_existing_issue(self):
        before = self.store.read_json("issue.json")
        with self.assertRaises(FileExistsError):
            Pipeline.initialize(self.store, "replacement", PHASES)
        self.assertEqual(before, self.store.read_json("issue.json"))

    def test_initialize_does_not_overwrite_partial_metadata(self):
        store = WorkStore(self.root / "work", "PARTIAL-001")
        (store.path / "phases").mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            Pipeline.initialize(store, "ship", PHASES)
        self.assertFalse((store.path / "issue.json").exists())

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
        original = self.store.read_json("evidence/phase-01-adversarial-review.json")
        summary = self.store.read_json("evidence/phase-01-review-summary.json")
        self.assertEqual("broken", original["findings"][0]["description"])
        self.assertEqual("SUCCESS", summary["status"])
        self.assertEqual("REQUEST_CHANGES", summary["recommendation"])
        self.assertEqual([1], summary["items"][0]["finding_ids"])
        self.assertIn('"id": 1', agent.prompts[1])
        self.assertIn("Issue description: ship", agent.prompts[1])

    def test_summary_failure_does_not_block_passed_review_or_approval(self):
        agent = FakeAgent(
            [{"result": "PASS", "findings": []}],
            summary_replies=[AgentError("summary unavailable")],
        )
        pipeline = Pipeline(self.store, self.root, agent)
        pipeline.verify([("ok", "python -c 'print(1)'")])
        self.assertTrue(pipeline.review())
        self.assertEqual("AWAITING_HUMAN_CHECK", self.store.load_state().state)
        self.assertEqual("PASS", self.store.read_json(
            "evidence/phase-01-adversarial-review.json")["result"])
        summary = self.store.read_json("evidence/phase-01-review-summary.json")
        self.assertEqual("FAILED", summary["status"])
        self.assertEqual("要約を生成できませんでした", summary["error"])
        self.assertEqual("PHASE_RUNNING", pipeline.human_check("ACCEPT"))

    def test_summary_with_incomplete_finding_mapping_is_rejected_without_stopping_review(self):
        findings = [
            {"severity": "high", "description": "first", "recommendation": "fix first"},
            {"severity": "low", "description": "second", "recommendation": "fix second"},
        ]
        invalid_summary = {
            "recommendation": "REQUEST_CHANGES",
            "decision_reason": "修正が必要です",
            "affected_scope": "利用操作",
            "required_action": "修正を依頼してください",
            "items": [{"finding_ids": [1], "problem": "問題", "business_impact": "影響",
                       "affected_scope": "範囲", "required_action": "対応"}],
        }
        pipeline = Pipeline(
            self.store, self.root,
            FakeAgent([{"result": "FINDINGS", "findings": findings}], [invalid_summary]),
        )
        pipeline.verify([("ok", "python -c 'print(1)'")])
        self.assertFalse(pipeline.review())
        self.assertEqual("PHASE_RUNNING", self.store.load_state().state)
        self.assertEqual("FAILED", self.store.read_json(
            "evidence/phase-01-review-summary.json")["status"])

    def test_pass_with_findings_is_not_accepted(self):
        agent = FakeAgent([{"result": "PASS", "findings": [{"description": "broken"}]}])
        pipeline = Pipeline(self.store, self.root, agent)
        pipeline.verify([("ok", "python -c 'print(1)'")])
        with self.assertRaisesRegex(ValueError, "PASS review must not contain findings"):
            pipeline.review()
        self.assertEqual("PHASE_REVIEWING", self.store.load_state().state)

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
        run_prompts = [prompt for prompt in agent.prompts
                       if prompt.startswith("You are the implementation agent")]
        self.assertIn("finish feature", run_prompts[1])
        self.assertIn("missing behavior", run_prompts[2])
        self.assertIn("repair behavior", run_prompts[2])
        self.assertIn("verification.log", run_prompts[2])
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

    def test_until_ready_uses_manifest_command_when_none_provided(self):
        (self.root / "devpipeline-tests.json").write_text(
            '{"command": "node --test"}', encoding="utf-8")
        agent = FakeAgent([
            {"result": "GOAL_REACHED", "completed": [], "remaining": [],
             "problems": [], "recommended_next_action": "verify"},
            {"result": "PASS", "findings": []},
        ])
        pipeline = Pipeline(self.store, self.root, agent)
        with patch("dev_pipeline.pipeline.subprocess.run",
                   return_value=CompletedProcess("check", 0, "ok", "")) as run:
            self.assertEqual("READY", pipeline.run_until_ready([]))
        self.assertEqual("node --test", run.call_args.args[0])
        self.assertEqual("AWAITING_HUMAN_CHECK", self.store.load_state().state)

    def test_accept_last_phase_completes_issue(self):
        state = self.store.load_state()
        state.state = "AWAITING_HUMAN_CHECK"
        state.current_phase = "phase-02"
        self.store.save_state(state)
        pipeline = Pipeline(self.store, self.root, FakeAgent([]))
        self.assertEqual("RESOLVED", pipeline.human_check("ACCEPT"))
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

    def test_stored_checks_are_used_when_run_omits_checks(self):
        store = WorkStore(self.root / "work", "STORED-001")
        Pipeline.initialize(store, "ship", PHASES,
                            checks=[("test", "python -c 'print(1)'")])
        agent = FakeAgent([
            {"result": "GOAL_REACHED", "completed": ["all"], "remaining": [],
             "problems": [], "recommended_next_action": "verify"},
            {"result": "PASS", "findings": []},
        ])
        result = Pipeline(store, self.root, agent).run([])
        self.assertEqual("READY", result)
        self.assertEqual("AWAITING_HUMAN_CHECK", store.load_state().state)

    def test_run_uses_manifest_command_when_none_configured(self):
        (self.root / "devpipeline-tests.json").write_text(
            '{"command": "python -m unittest discover -s tests"}', encoding="utf-8")
        agent = FakeAgent([
            {"result": "GOAL_REACHED", "completed": ["all"], "remaining": [],
             "problems": [], "recommended_next_action": "verify"},
            {"result": "PASS", "findings": []},
        ])
        pipeline = Pipeline(self.store, self.root, agent)
        with patch("dev_pipeline.pipeline.subprocess.run",
                   return_value=CompletedProcess("check", 0, "ok", "")) as run:
            self.assertEqual("READY", pipeline.run([]))
        self.assertEqual("python -m unittest discover -s tests", run.call_args.args[0])
        self.assertEqual("AWAITING_HUMAN_CHECK", self.store.load_state().state)

    def test_run_explicit_checks_do_not_persist(self):
        outcome = {"result": "CONTINUE", "completed": ["core"], "remaining": ["UI"],
                   "problems": [], "recommended_next_action": "build UI"}
        agent = FakeAgent([outcome])
        pipeline = Pipeline(self.store, self.root, agent)
        pipeline.run([("lint", "flake8")])
        self.assertEqual([], self.store.load_issue().checks)

    def test_run_no_verify_skips_verification_and_goes_to_review(self):
        agent = FakeAgent([
            {"result": "GOAL_REACHED", "completed": ["all"], "remaining": [],
             "problems": [], "recommended_next_action": "review"},
            {"result": "PASS", "findings": []},
        ])
        pipeline = Pipeline(self.store, self.root, agent)
        with patch("dev_pipeline.pipeline.subprocess.run") as run:
            self.assertEqual("READY", pipeline.run([], verify=False))
        run.assert_not_called()
        self.assertFalse((self.store.path / "evidence/phase-01-verification.json").exists())
        self.assertEqual("AWAITING_HUMAN_CHECK", self.store.load_state().state)

    def test_verify_uses_stored_checks_when_given_empty(self):
        store = WorkStore(self.root / "work", "VERIFY-001")
        Pipeline.initialize(store, "ship", PHASES,
                            checks=[("test", "python -c 'print(1)'")])
        pipeline = Pipeline(store, self.root, FakeAgent([]))
        self.assertTrue(pipeline.verify([]))
        evidence = store.read_json("evidence/phase-01-verification.json")
        self.assertEqual("PASS", evidence["overall"])

    def test_verify_fails_when_default_manifest_is_missing(self):
        pipeline = Pipeline(self.store, self.root, FakeAgent([]))
        self.assertFalse(pipeline.verify([]))
        evidence = self.store.read_json("evidence/phase-01-verification.json")
        self.assertEqual("FAIL", evidence["overall"])
        self.assertEqual("manifest", evidence["checks"][0]["name"])
        self.assertEqual("PHASE_RUNNING", self.store.load_state().state)

    def test_review_with_passing_test_command_is_disproved_and_passes(self):
        finding = {
            "severity": "high",
            "description": "alleged defect",
            "recommendation": "fix it",
            "test_command": "python -c 'exit(0)'",
        }
        agent = FakeAgent([{"result": "FINDINGS", "findings": [finding]}])
        pipeline = Pipeline(self.store, self.root, agent)
        pipeline.verify([("ok", "python -c 'print(1)'")])
        self.assertTrue(pipeline.review())
        self.assertEqual("AWAITING_HUMAN_CHECK", self.store.load_state().state)
        evidence = self.store.read_json("evidence/phase-01-adversarial-review.json")
        self.assertEqual("PASS", evidence["result"])
        self.assertEqual([], evidence["findings"])

    def test_review_with_failing_test_command_is_verified_and_findings_kept(self):
        finding = {
            "severity": "high",
            "description": "real defect",
            "recommendation": "fix it",
            "test_command": "python -c 'exit(1)'",
        }
        agent = FakeAgent([{"result": "FINDINGS", "findings": [finding]}])
        pipeline = Pipeline(self.store, self.root, agent)
        pipeline.verify([("ok", "python -c 'print(1)'")])
        self.assertFalse(pipeline.review())
        self.assertEqual("PHASE_RUNNING", self.store.load_state().state)
        evidence = self.store.read_json("evidence/phase-01-adversarial-review.json")
        self.assertEqual("FINDINGS", evidence["result"])
        self.assertTrue(evidence["findings"][0]["test_verified"])

    def test_run_until_ready_auto_passes_after_max_review_retries(self):
        outcome = {"result": "GOAL_REACHED", "completed": ["all"], "remaining": [],
                   "problems": [], "recommended_next_action": "verify"}
        finding = {"severity": "medium", "description": "unfalsifiable nitpick",
                   "recommendation": "refactor"}
        agent = FakeAgent([
            outcome, {"result": "FINDINGS", "findings": [finding]},
            outcome, {"result": "FINDINGS", "findings": [finding]},
        ])
        pipeline = Pipeline(self.store, self.root, agent)
        self.assertEqual("READY", pipeline.run_until_ready([("ok", "python -c 'print(1)'")], max_review_retries=2))
        self.assertEqual("AWAITING_HUMAN_CHECK", self.store.load_state().state)
        evidence = self.store.read_json("evidence/phase-01-adversarial-review.json")
        self.assertTrue(evidence.get("auto_passed"))
        self.assertIn("maximum review retries (2)", evidence.get("warning", ""))

    def test_run_until_ready_does_not_auto_pass_when_verification_fails(self):
        outcome = {"result": "GOAL_REACHED", "completed": [], "remaining": [],
                   "problems": [], "recommended_next_action": "verify"}
        agent = FakeAgent([outcome])
        pipeline = Pipeline(self.store, self.root, agent)
        self.assertEqual("VERIFICATION_FAILED", pipeline.run([("fail", "python -c 'exit(1)'")]))
        state = self.store.load_state()
        state.review_retries = 5
        self.store.save_state(state)
        self.assertFalse(pipeline._is_verification_passed("phase-01"))


class AgentParsingTests(unittest.TestCase):
    def test_codex_command_uses_workspace_write(self):
        agent = CodexAgent()
        with patch("dev_pipeline.agents.process.subprocess.run",
                   return_value=CompletedProcess("codex", 0, '{"result":"PASS"}', "")) as run:
            agent.execute("prompt", Path("."))
        self.assertEqual(["codex", "exec", "--sandbox", "workspace-write", "--json", "-"],
                         run.call_args.args[0])

    def test_extracts_final_json_after_noisy_output(self):
        self.assertEqual({"result": "PASS"}, extract_json("thinking\n{\"result\":\"PASS\"}\n"))


if __name__ == "__main__":
    unittest.main()
