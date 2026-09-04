from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_pipeline.prompts import DEFAULT_PROMPTS_DIR, render_prompt


class PromptRenderingTests(unittest.TestCase):
    def test_renders_planner_without_leftover_placeholders(self):
        prompt = render_prompt("planner", request="build a tool", requirement="['a']",
                               constraint="['b']")
        self.assertIn("Request: build a tool", prompt)
        self.assertIn("Additional requirements: ['a']", prompt)
        self.assertIn('"goal":"..."', prompt)
        self.assertNotIn("$", prompt)

    def test_run_and_review_share_context_style(self):
        run = render_prompt("run", repository="/repo", goal="g", requirements="['r']",
                            constraints="[]", phase_goal="pg", scope="['s']",
                            acceptance_criteria="['c']", feedback="none",
                            accepted_phases="[]", current_run="1",
                            checks="[('t', 'pytest')]", context="none")
        review = render_prompt("review", repository="/repo", goal="g",
                               requirements="['r']", constraints="[]", phase_goal="pg",
                               scope="['s']", acceptance_criteria="['c']", context="none")
        self.assertIn("Work directly in repository: /repo", run)
        self.assertIn('result (CONTINUE or GOAL_REACHED)', run)
        self.assertIn("repository at /repo", review)
        self.assertIn('"result":"PASS"|"FINDINGS"', review)
        self.assertNotIn("$", run + review)

    def test_missing_variable_raises_to_keep_templates_in_sync(self):
        with self.assertRaises(KeyError):
            render_prompt("planner", request="only")

    def test_environment_override_directory_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            custom = Path(directory)
            (custom / "planner.txt").write_text("Custom planner for $request\n", encoding="utf-8")
            with patch.dict(os.environ, {"DEV_PIPELINE_PROMPTS_DIR": str(custom)}):
                self.assertEqual("Custom planner for hi\n",
                                 render_prompt("planner", request="hi"))

    def test_bundled_templates_exist_for_all_stages(self):
        for name in ("planner", "run", "review"):
            self.assertTrue((DEFAULT_PROMPTS_DIR / f"{name}.txt").is_file())


if __name__ == "__main__":
    unittest.main()
