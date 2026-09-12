from __future__ import annotations

import json
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path

from dev_pipeline.agents import AgentResult
from dev_pipeline.cli import main
from dev_pipeline.models import Project, Issue, Phase
from dev_pipeline.pipeline import Pipeline
from dev_pipeline.reporting import read_project, list_project_ids, list_issue_ids, read_issue
from dev_pipeline.store import ProjectStore, IssueStore
from dev_pipeline.web_service import WebService


class FakeAgent:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.prompts = []

    def execute(self, prompt, repository):
        self.prompts.append(prompt)
        data = next(self.replies)
        return AgentResult(data, json.dumps(data), "")


class ProjectIssueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work_dir = self.root / "work"

    def tearDown(self):
        self.temp.cleanup()

    def test_project_store_and_issue_store_lifecycle(self):
        project_store = ProjectStore(self.work_dir, "proj-1")
        project = Project(
            id="proj-1",
            repo=str(self.root / "repo"),
            agent="antigravity",
            checks=[("unit", "pytest")],
        )
        project_store.create(project)
        self.assertEqual(project.id, "proj-1")
        self.assertTrue(project_store.exists())
        self.assertEqual(project_store.load_project().agent, "antigravity")
        self.assertEqual(project_store.load_project().checks, [("unit", "pytest")])

        issue_store_1 = project_store.issue_store("ISS-001")
        self.assertFalse(issue_store_1.exists())
        issue_1 = Issue("ISS-001", "Add feature A", ["phase-01"])
        phase_1 = Phase("phase-01", 1, "Goal 1", ["all"], ["done"])
        issue_store_1.create(issue_1, [phase_1])
        self.assertEqual(issue_1.id, "ISS-001")
        self.assertTrue(issue_store_1.exists())

        # List issues in project
        self.assertEqual(project_store.list_issue_ids(), ["ISS-001"])
        self.assertEqual(project_store.resolve_issue_id(None), "ISS-001")
        self.assertEqual(project_store.next_issue_id(), "0002")

        # Second issue
        issue_store_2 = project_store.issue_store("ISS-002")
        issue_2 = Issue("ISS-002", "Add feature B", ["phase-01"])
        phase_2 = Phase("phase-01", 1, "Goal 2", ["all"], ["done"])
        issue_store_2.create(issue_2, [phase_2])
        self.assertEqual(project_store.list_issue_ids(), ["ISS-001", "ISS-002"])
        # Resolve None returns the latest
        self.assertEqual(project_store.resolve_issue_id(None), "ISS-002")

    def test_cli_project_and_issue_commands(self):
        # 1. Project init
        ret = main(["--work-dir", str(self.work_dir), "project", "init", "my-project", "--agent", "antigravity"])
        self.assertEqual(ret, 0)
        proj_store = ProjectStore(self.work_dir, "my-project")
        self.assertTrue(proj_store.exists())

        # 2. Project list & show
        self.assertEqual(main(["--work-dir", str(self.work_dir), "project", "list"]), 0)
        self.assertEqual(main(["--work-dir", str(self.work_dir), "project", "show", "my-project"]), 0)

        # 3. Issue create with explicit phases file
        phase_file = self.root / "phases.json"
        phase_file.write_text(json.dumps([
            {"goal": "impl 1", "scope": ["core"], "acceptance_criteria": ["pass"]}
        ]), encoding="utf-8")
        ret = main([
            "--work-dir", str(self.work_dir),
            "issue", "create",
            "my-project",
            "--id", "ISS-1",
            "--request", "Build auth",
            "--phase-file", str(phase_file),
            "--plan-only",
        ])
        self.assertEqual(ret, 0)

        iss_store = proj_store.issue_store("ISS-1")
        self.assertTrue(iss_store.exists())
        self.assertEqual(iss_store.load_state().state, "PHASE_RUNNING")

        # 4. Issue list & show & status
        self.assertEqual(main(["--work-dir", str(self.work_dir), "issue", "list", "my-project"]), 0)
        self.assertEqual(main(["--work-dir", str(self.work_dir), "issue", "show", "my-project", "ISS-1"]), 0)
        self.assertEqual(main(["--work-dir", str(self.work_dir), "issue", "status", "my-project", "ISS-1"]), 0)

    def test_resolved_issues_context_injection(self):
        project_store = ProjectStore(self.work_dir, "multi-issue-proj")
        repo = self.root / "repo"
        repo.mkdir()
        project = Project(id="multi-issue-proj", repo=str(repo))
        project_store.create(project)

        # Issue 1: complete to RESOLVED
        issue_store_1 = project_store.issue_store("ISS-001")
        phases_1 = [{"goal": "Step 1", "scope": ["all"], "acceptance_criteria": ["done"]}]
        Pipeline.initialize(issue_store_1, "Goal 1", phases_1)

        # Run pipeline 1 until acceptance
        agent_1 = FakeAgent([
            {"result": "GOAL_REACHED", "completed": ["all"], "remaining": [], "problems": [], "recommended_next_action": "verify"},
            {"result": "PASS", "findings": []},
        ])
        pipeline_1 = Pipeline(issue_store_1, repo, agent_1)
        pipeline_1.run_until_ready([("noop", sys.executable + " -c 0")])
        pipeline_1.human_check("ACCEPT")

        self.assertEqual(issue_store_1.load_state().state, "RESOLVED")

        # Issue 2: initialize and check prompt context
        issue_store_2 = project_store.issue_store("ISS-002")
        phases_2 = [{"goal": "Step 2", "scope": ["all"], "acceptance_criteria": ["done"]}]
        Pipeline.initialize(issue_store_2, "Goal 2", phases_2)

        # Pipeline.run for Issue 2 will build run prompt including resolved issues
        agent_2 = FakeAgent([
            {"result": "GOAL_REACHED", "completed": ["all"], "remaining": [], "problems": [], "recommended_next_action": "verify"},
            {"result": "PASS", "findings": []},
        ])
        pipeline_2 = Pipeline(issue_store_2, repo, agent_2)
        pipeline_2.run_until_ready([("noop", sys.executable + " -c 0")])

        prompt_used = agent_2.prompts[0]
        self.assertIn("ISS-001: Goal 1", prompt_used)
        self.assertIn("Step 1", prompt_used)

    def test_web_service_project_and_issue_endpoints(self):
        svc = WebService(self.work_dir)
        try:
            proj = svc.create_project({"id": "web-proj", "agent": "antigravity"})
            self.assertEqual(proj["id"], "web-proj")

            projects = svc.list_projects()
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["id"], "web-proj")

            # Create issue within project
            issue_job = svc.create_issue("web-proj", {"request": "Make a calculator", "id": "ISS-100", "request_id": "req-1"})
            self.assertEqual(issue_job["issue_id"], "ISS-100")
            self.assertEqual(issue_job["project_id"], "web-proj")

            detail = svc.issue_detail("web-proj", "ISS-100")
            self.assertEqual(detail["issue"]["id"], "ISS-100")
            self.assertEqual(detail["config"]["agent"], "antigravity")

            svc.stop_issue("web-proj", "ISS-100")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                det = svc.issue_detail("web-proj", "ISS-100")
                if det["job"]["status"] not in ("queued", "running", "stopping"):
                    break
                time.sleep(0.05)

            # Create issue with long free-text (over 100 chars) and auto-generated ID
            long_text = "Crypto-naut Phase 2 突合検証パイプラインの簡素化改修。\n要件: 1秒スナップショット基準。\n制約: 取引規模はレバレッジ込み数百万円程度。" * 3
            long_job = svc.create_issue("web-proj", {"request": long_text, "request_id": "req-long"})
            self.assertTrue(long_job["issue_id"])
            self.assertNotEqual(long_job["issue_id"], "ISS-100")
            long_detail = svc.issue_detail("web-proj", long_job["issue_id"])
            self.assertEqual(long_detail["issue"]["id"], long_job["issue_id"])
            svc.stop_issue("web-proj", long_job["issue_id"])

            new_conf = svc.configure_issue("web-proj", "ISS-100", {"agent": "codex", "repository": detail["config"]["repository"]})
            self.assertEqual(new_conf["agent"], "codex")
            detail = svc.issue_detail("web-proj", "ISS-100")
            self.assertEqual(detail["config"]["agent"], "codex")
        finally:
            svc.close()
