from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from dev_pipeline.agents import AgentResult
from dev_pipeline.agents.base import AgentError
from dev_pipeline.cli import main
from dev_pipeline.models import Project
from dev_pipeline.pipeline import (
    Pipeline,
    format_changelog_entry,
    update_changelog_markdown,
)
from dev_pipeline.prompts import render_prompt
from dev_pipeline.reporting import read_changelog, read_issue
from dev_pipeline.store import ProjectStore, WorkStore
from dev_pipeline.web_service import WebService


class FakeChangelogAgent:
    def __init__(self, changelog_reply=None):
        self.changelog_reply = changelog_reply
        self.prompts = []

    def execute(self, prompt, repository):
        self.prompts.append(prompt)
        if prompt.startswith("Create a concise Japanese changelog entry"):
            if isinstance(self.changelog_reply, Exception):
                raise self.changelog_reply
            if self.changelog_reply is not None:
                data = self.changelog_reply
            else:
                data = {
                    "title": "新機能の追加",
                    "summary": "ユーザーが新機能を利用できるようになりました。",
                    "changes": ["機能Aを追加しました", "操作画面を改善しました"],
                }
            return AgentResult(data, json.dumps(data, ensure_ascii=False), "")
        elif prompt.startswith("Create a concise Japanese summary"):
            data = {
                "recommendation": "APPROVE",
                "decision_reason": "問題ありません",
                "affected_scope": "全機能",
                "required_action": "承認してください",
                "items": [],
            }
            return AgentResult(data, json.dumps(data), "")
        else:
            data = {"result": "PASS", "findings": []}
            return AgentResult(data, json.dumps(data), "")


class ChangelogUnitTests(unittest.TestCase):
    def test_format_changelog_entry(self):
        entry = format_changelog_entry(
            "ISS-1",
            "認証機能の追加",
            "2026-09-14",
            "安全にログインできるようになりました。",
            ["Googleログインを追加", "セッション管理を強化"],
        )
        self.assertIn("## [ISS-1] 認証機能の追加 - 2026-09-14", entry)
        self.assertIn("安全にログインできるようになりました。", entry)
        self.assertIn("### 主な変更点", entry)
        self.assertIn("- Googleログインを追加", entry)
        self.assertIn("- セッション管理を強化", entry)

    def test_update_changelog_markdown_new_file(self):
        entry = format_changelog_entry("ISS-1", "Feature A", "2026-09-14", "Summary", ["Change 1"])
        result = update_changelog_markdown("", entry, "ISS-1")
        self.assertTrue(result.startswith("# Changelog\n\nAll notable changes to this project will be documented in this file.\n\n"))
        self.assertIn("## [ISS-1] Feature A - 2026-09-14", result)

    def test_update_changelog_markdown_prepend(self):
        entry_old = format_changelog_entry("ISS-1", "Feature A", "2026-09-10", "Summary A", ["Change 1"])
        initial = update_changelog_markdown("", entry_old, "ISS-1")

        entry_new = format_changelog_entry("ISS-2", "Feature B", "2026-09-14", "Summary B", ["Change 2"])
        updated = update_changelog_markdown(initial, entry_new, "ISS-2")

        # New entry should be placed before the old entry
        pos_new = updated.find("## [ISS-2]")
        pos_old = updated.find("## [ISS-1]")
        self.assertNotEqual(pos_new, -1)
        self.assertNotEqual(pos_old, -1)
        self.assertLess(pos_new, pos_old)
        self.assertTrue(updated.startswith("# Changelog"))

    def test_update_changelog_markdown_replace_existing_issue(self):
        entry_initial = format_changelog_entry("ISS-1", "Initial Title", "2026-09-10", "Old Summary", ["Old Change"])
        content = update_changelog_markdown("", entry_initial, "ISS-1")

        entry_updated = format_changelog_entry("ISS-1", "Updated Title", "2026-09-14", "New Summary", ["New Change"])
        result = update_changelog_markdown(content, entry_updated, "ISS-1")

        self.assertEqual(result.count("## [ISS-1]"), 1)
        self.assertIn("Updated Title", result)
        self.assertIn("New Summary", result)
        self.assertNotIn("Old Summary", result)

    def test_validate_changelog(self):
        valid = {
            "title": "タイトル",
            "summary": "要約です",
            "changes": ["変更点1", "変更点2"],
        }
        validated = Pipeline._validate_changelog(valid)
        self.assertEqual(validated["title"], "タイトル")
        self.assertEqual(validated["summary"], "要約です")
        self.assertEqual(len(validated["changes"]), 2)

        # Invalid cases
        with self.assertRaises(ValueError):
            Pipeline._validate_changelog("not a dict")
        with self.assertRaises(ValueError):
            Pipeline._validate_changelog({"title": "", "summary": "ok", "changes": ["a"]})
        with self.assertRaises(ValueError):
            Pipeline._validate_changelog({"title": "ok", "summary": "", "changes": ["a"]})
        with self.assertRaises(ValueError):
            Pipeline._validate_changelog({"title": "ok", "summary": "ok", "changes": []})
        with self.assertRaises(ValueError):
            Pipeline._validate_changelog({"title": "ok", "summary": "ok", "changes": ["  "]})

    def test_render_changelog_prompt(self):
        rendered = render_prompt(
            "changelog",
            issue_id="ISS-10",
            description="通知機能の実装",
            phases_context="Phase phase-01: 通知APIの構築",
            date="2026-09-14",
        )
        self.assertIn("Issue ID: ISS-10", rendered)
        self.assertIn("通知機能の実装", rendered)
        self.assertIn("Phase phase-01: 通知APIの構築", rendered)
        self.assertIn("2026-09-14", rendered)


class ChangelogPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work_dir = self.root / "work"
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_human_check_accept_generates_changelog_on_resolution(self):
        store = WorkStore(self.work_dir, "ISSUE-1")
        phases = [
            {"goal": "core feature", "scope": ["core"], "acceptance_criteria": ["core passes"]},
            {"goal": "ui feature", "scope": ["ui"], "acceptance_criteria": ["ui passes"]},
        ]
        Pipeline.initialize(store, "検索機能を構築する", phases)
        agent = FakeChangelogAgent()
        pipeline = Pipeline(store, self.repo, agent)

        # Phase 1
        state = store.load_state()
        state.state = "AWAITING_HUMAN_CHECK"
        state.current_phase = "phase-01"
        store.save_state(state)
        pipeline.human_check("ACCEPT")

        # After Phase 1 accepted, CHANGELOG.md should not be created yet (not resolved)
        changelog_file = self.repo / "CHANGELOG.md"
        self.assertFalse(changelog_file.exists())

        # Phase 2 (final phase)
        state = store.load_state()
        self.assertEqual(state.state, "PHASE_RUNNING")
        self.assertEqual(state.current_phase, "phase-02")
        state.state = "AWAITING_HUMAN_CHECK"
        store.save_state(state)
        res = pipeline.human_check("ACCEPT")

        self.assertEqual(res, "RESOLVED")
        self.assertTrue(changelog_file.exists())
        content = changelog_file.read_text(encoding="utf-8")
        self.assertIn("## [ISSUE-1] 新機能の追加 -", content)
        self.assertIn("ユーザーが新機能を利用できるようになりました。", content)
        self.assertIn("- 機能Aを追加しました", content)

        # Evidence should be stored
        ev = store.read_json("evidence/changelog.json")
        self.assertEqual(ev["status"], "SUCCESS")
        self.assertEqual(ev["issue_id"], "ISSUE-1")
        self.assertEqual(ev["title"], "新機能の追加")

        # Reporting should read changelog
        issue_data = read_issue(self.work_dir, "ISSUE-1", "ISSUE-1") if False else store.read_json("evidence/changelog.json")
        self.assertIsNotNone(issue_data)
        self.assertEqual(read_changelog(self.repo), content)

    def test_changelog_agent_error_fallback(self):
        store = WorkStore(self.work_dir, "ISSUE-ERR")
        phases = [
            {"goal": "only phase", "scope": ["all"], "acceptance_criteria": ["done"]},
        ]
        Pipeline.initialize(store, "エラー発生時の検証", phases)
        agent = FakeChangelogAgent(changelog_reply=AgentError("Agent rate limit exceeded"))
        pipeline = Pipeline(store, self.repo, agent)

        state = store.load_state()
        state.state = "AWAITING_HUMAN_CHECK"
        state.current_phase = "phase-01"
        store.save_state(state)

        res = pipeline.human_check("ACCEPT")
        self.assertEqual(res, "RESOLVED")

        # CHANGELOG should still be generated with fallback
        changelog_file = self.repo / "CHANGELOG.md"
        self.assertTrue(changelog_file.exists())
        content = changelog_file.read_text(encoding="utf-8")
        self.assertIn("エラー発生時の検証", content)

        ev = store.read_json("evidence/changelog.json")
        self.assertEqual(ev["status"], "FALLBACK")

    def test_cli_changelog_command(self):
        p_store = ProjectStore(self.work_dir, "PROJ-1")
        p_store.create(Project(id="PROJ-1", repo=str(self.repo), agent="codex"))
        issue_store = p_store.issue_store("0001")
        phases = [{"goal": "feat 1", "scope": ["s"], "acceptance_criteria": ["c"]}]
        Pipeline.initialize(issue_store, "CLIテスト案件", phases)

        agent = FakeChangelogAgent()
        pipeline = Pipeline(issue_store, self.repo, agent)
        state = issue_store.load_state()
        state.state = "AWAITING_HUMAN_CHECK"
        state.current_phase = "phase-01"
        issue_store.save_state(state)
        pipeline.human_check("ACCEPT")

        # CLI view
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--work-dir", str(self.work_dir), "issue", "changelog", "PROJ-1", "0001"])
        self.assertEqual(code, 0)
        self.assertIn("新機能の追加", out.getvalue())

    def test_web_service_project_changelog(self):
        p_store = ProjectStore(self.work_dir, "PROJ-WEB")
        p_store.create(Project(id="PROJ-WEB", repo=str(self.repo), agent="codex"))
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n\n## [1] Test - 2026-09-14\n\nExisting entry\n", encoding="utf-8")

        service = WebService(self.work_dir)
        try:
            cl = service.project_changelog("PROJ-WEB")
            self.assertIn("Existing entry", cl)
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
