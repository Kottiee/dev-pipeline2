from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dev_pipeline.agents import AgentResult
from dev_pipeline.models import Project
from dev_pipeline.pipeline import Pipeline
from dev_pipeline.reporting import _collect_phase_runs, read_issue, read_pipeline
from dev_pipeline.store import ProjectStore, WorkStore


class FakeHistoryAgent:
    def __init__(self, run_replies, review_replies=None, summary_replies=None):
        self.run_replies = iter(run_replies)
        self.review_replies = iter(review_replies) if review_replies is not None else None
        self.summary_replies = iter(summary_replies) if summary_replies is not None else None
        self.prompts = []

    def execute(self, prompt, repository):
        self.prompts.append(prompt)
        if prompt.startswith("Create a concise Japanese summary"):
            if self.summary_replies is not None:
                data = next(self.summary_replies)
            else:
                data = {
                    "recommendation": "APPROVE",
                    "decision_reason": "テスト・レビューともに問題ありません",
                    "affected_scope": "全機能",
                    "required_action": "承認してください",
                    "items": [],
                }
        elif "Act as an independent code reviewer" in prompt:
            if self.review_replies is not None:
                data = next(self.review_replies)
            else:
                data = {"result": "PASS", "findings": []}
        else:
            data = next(self.run_replies)
        return AgentResult(data, json.dumps(data, ensure_ascii=False), "")


class ExecutionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work_dir = self.root / "work"
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_run_evidence_persistence_with_run_numbers(self):
        store = WorkStore(self.work_dir, "ISS-HIST-1")
        phases = [
            {"goal": "duplicate removal", "scope": ["core"], "acceptance_criteria": ["deduped"]},
        ]
        Pipeline.initialize(store, "重複除外を実装", phases)

        # Run 1: review findings
        agent = FakeHistoryAgent(
            run_replies=[
                {"result": "GOAL_REACHED", "completed": ["core"], "remaining": [],
                 "problems": [], "recommended_next_action": "review"},
            ],
            review_replies=[
                {"result": "FINDINGS", "findings": [
                    {"id": 1, "severity": "medium", "description": "同じ請求番号でも取引先が違う行を削除する可能性"}
                ]},
            ],
            summary_replies=[
                {
                    "recommendation": "REQUEST_CHANGES",
                    "decision_reason": "誤削除の懸念があります",
                    "affected_scope": "請求データ",
                    "required_action": "取引先ごとの判定を追加してください",
                    "items": [{
                        "finding_ids": [1],
                        "problem": "同じ請求番号でも取引先が違う行を削除する可能性",
                        "business_impact": "別顧客の請求データが消えるリスク",
                        "affected_scope": "請求データ",
                        "required_action": "取引先ごとの判定を追加してください",
                    }],
                },
            ],
        )
        pipeline = Pipeline(store, self.repo, agent)

        # Register check that passes
        checks = [("test", "true")]
        res1 = pipeline.run(checks)
        self.assertEqual(res1, "REVIEW_FINDINGS")

        state = store.load_state()
        self.assertEqual(state.current_run, 1)

        # Verify run-numbered evidence files exist
        evidence_dir = store.path / "evidence"
        self.assertTrue((evidence_dir / "phase-01-run-001-verification.json").exists())
        self.assertTrue((evidence_dir / "phase-01-run-001-verification.log").exists())
        self.assertTrue((evidence_dir / "phase-01-run-001-adversarial-review.json").exists())
        self.assertTrue((evidence_dir / "phase-01-run-001-review-agent.log").exists())
        self.assertTrue((evidence_dir / "phase-01-run-001-review-summary.json").exists())
        self.assertTrue((evidence_dir / "phase-01-run-001-review-summary-agent.log").exists())

        # Also backward-compatible unnumbered files exist
        self.assertTrue((evidence_dir / "phase-01-verification.json").exists())
        self.assertTrue((evidence_dir / "phase-01-adversarial-review.json").exists())

        # Check collected phase runs
        runs = _collect_phase_runs(store, "phase-01")
        self.assertEqual(len(runs), 1)
        r1 = runs[0]
        self.assertEqual(r1["run_id"], 1)
        self.assertEqual(r1["outcome"], "REVIEW_FINDINGS")
        self.assertEqual(r1["outcome_label"], "レビュー指摘")
        self.assertEqual(r1["reason"], "同じ請求番号でも取引先が違う行を削除する可能性")
        self.assertEqual(r1["next_action"], "指摘を反映してRun 2を開始")

    def test_multi_run_history_records_past_failures_and_human_feedback(self):
        p_store = ProjectStore(self.work_dir, "PROJ-HIST")
        p_store.create(Project(id="PROJ-HIST", repo=str(self.repo), agent="codex"))
        issue_store = p_store.issue_store("0001")
        phases = [
            {"goal": "duplicate removal", "scope": ["core"], "acceptance_criteria": ["deduped"]},
        ]
        Pipeline.initialize(issue_store, "重複除外を実装", phases)

        # Run 1: Test failed
        agent1 = FakeHistoryAgent(
            run_replies=[
                {"result": "GOAL_REACHED", "completed": ["core"], "remaining": [],
                 "problems": [], "recommended_next_action": "verify"},
            ]
        )
        pipeline1 = Pipeline(issue_store, self.repo, agent1)
        failing_checks = [("unit-test", "false")]
        res1 = pipeline1.run(failing_checks)
        self.assertEqual(res1, "VERIFICATION_FAILED")

        # Run 2: Review pass, but Human Feedback
        agent2 = FakeHistoryAgent(
            run_replies=[
                {"result": "GOAL_REACHED", "completed": ["core"], "remaining": [],
                 "problems": [], "recommended_next_action": "review"},
            ],
            review_replies=[{"result": "PASS", "findings": []}],
            summary_replies=[{
                "recommendation": "APPROVE",
                "decision_reason": "テスト・レビュー合格",
                "affected_scope": "全体",
                "required_action": "承認してください",
                "items": [],
            }],
        )
        pipeline2 = Pipeline(issue_store, self.repo, agent2)
        passing_checks = [("unit-test", "true")]
        res2 = pipeline2.run(passing_checks)
        self.assertEqual(res2, "READY")

        # Human feedback on Run 2
        pipeline2.human_check("FEEDBACK", feedback="空の請求番号も確認してほしい")
        self.assertEqual(issue_store.load_state().state, "PHASE_FIXING")

        # Run 3: Success and Human Accept
        agent3 = FakeHistoryAgent(
            run_replies=[
                {"result": "GOAL_REACHED", "completed": ["core"], "remaining": [],
                 "problems": [], "recommended_next_action": "review"},
            ],
            review_replies=[{"result": "PASS", "findings": []}],
            summary_replies=[{
                "recommendation": "APPROVE",
                "decision_reason": "テスト・レビュー合格",
                "affected_scope": "全体",
                "required_action": "承認してください",
                "items": [],
            }],
        )
        pipeline3 = Pipeline(issue_store, self.repo, agent3)
        res3 = pipeline3.run(passing_checks)
        self.assertEqual(res3, "READY")

        # Inspect runs history BEFORE human acceptance of Run 3
        detail_before = read_issue(self.work_dir, "PROJ-HIST", "0001")
        phase1_ev = detail_before["evidence"][0]
        runs_before = phase1_ev["runs"]

        self.assertEqual(len(runs_before), 3)
        # Ordered newest first: Run 3, Run 2, Run 1
        r3 = runs_before[0]
        r2 = runs_before[1]
        r1 = runs_before[2]

        self.assertEqual(r3["run_id"], 3)
        self.assertEqual(r3["outcome"], "AWAITING_HUMAN_CHECK")
        self.assertEqual(r3["outcome_label"], "承認待ち")
        self.assertEqual(r3["reason"], "テスト・レビューともに合格")
        self.assertEqual(r3["next_action"], "現在：あなたの承認待ち")

        self.assertEqual(r2["run_id"], 2)
        self.assertEqual(r2["outcome"], "HUMAN_FEEDBACK")
        self.assertEqual(r2["outcome_label"], "差し戻し")
        self.assertEqual(r2["reason"], "人の判断：空の請求番号も確認してほしい")
        self.assertEqual(r2["next_action"], "修正Run 3を開始")

        self.assertEqual(r1["run_id"], 1)
        self.assertEqual(r1["outcome"], "VERIFICATION_FAILED")
        self.assertEqual(r1["outcome_label"], "テスト失敗")
        self.assertIn("unit-test", r1["reason"])
        self.assertEqual(r1["next_action"], "不具合を修正してRun 2を開始")

        # Accept Run 3
        pipeline3.human_check("ACCEPT")
        self.assertEqual(issue_store.load_state().state, "RESOLVED")

        # Inspect runs history AFTER acceptance
        detail_after = read_issue(self.work_dir, "PROJ-HIST", "0001")
        runs_after = detail_after["evidence"][0]["runs"]
        r3_accepted = runs_after[0]
        self.assertEqual(r3_accepted["outcome"], "ACCEPTED")
        self.assertEqual(r3_accepted["outcome_label"], "承認済み")

        # History is also available on top-level
        self.assertIn("history", detail_after)
        self.assertEqual(len(detail_after["history"]), 3)

    def test_legacy_workspace_compatibility(self):
        store = WorkStore(self.work_dir, "LEGACY-1")
        phases = [{"goal": "legacy feat", "scope": ["legacy"], "acceptance_criteria": ["ok"]}]
        Pipeline.initialize(store, "レガシー案件", phases)

        # Manually create legacy unnumbered files without run numbers
        evidence_dir = store.path / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "phase-01-verification.json").write_text(
            json.dumps({"phase_id": "phase-01", "run_id": 1, "overall": "PASS", "checks": []}),
            encoding="utf-8",
        )
        (evidence_dir / "phase-01-adversarial-review.json").write_text(
            json.dumps({"phase_id": "phase-01", "run_id": 1, "result": "PASS", "findings": []}),
            encoding="utf-8",
        )

        detail = read_pipeline(self.work_dir, "LEGACY-1")
        runs = detail["evidence"][0]["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], 1)
        self.assertEqual(runs[0]["outcome"], "AWAITING_HUMAN_CHECK")


if __name__ == "__main__":
    unittest.main()
