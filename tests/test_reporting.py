from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dev_pipeline.pipeline import Pipeline
from dev_pipeline.reporting import (
    default_repository,
    initiative_exists,
    list_initiative_ids,
    read_pipeline,
)
from dev_pipeline.store import JsonStore, WorkStore


PHASES = [
    {"goal": "利用できる機能", "scope": ["implementation"],
     "acceptance_criteria": ["テストに合格する"]},
]


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp.name) / "work"
        self.store = WorkStore(self.work_dir, "TEST-1")
        Pipeline.initialize(self.store, "完成させる", ["正しく動く"], [], PHASES)

    def tearDown(self):
        self.temp.cleanup()

    def test_read_model_contains_state_and_available_actions(self):
        result = read_pipeline(self.work_dir, "TEST-1")
        self.assertEqual("PHASE_RUNNING", result["state"]["state"])
        self.assertEqual({"run": True, "human_check": False, "completed": False},
                         result["actions"])
        self.assertEqual("phase-01", result["evidence"][0]["phase_id"])
        self.assertIsNone(result["evidence"][0]["verification"])
        self.assertEqual("", result["checkpoint"])

        state = self.store.load_state()
        state.state = "AWAITING_HUMAN_CHECK"
        self.store.save_state(state)
        result = read_pipeline(self.work_dir, "TEST-1", include_evidence=False)
        self.assertEqual({"run": False, "human_check": True, "completed": False},
                         result["actions"])
        self.assertNotIn("evidence", result)

    def test_discovery_and_default_repository_hide_storage_layout(self):
        self.assertTrue(initiative_exists(self.work_dir, "TEST-1"))
        self.assertEqual(["TEST-1"], list_initiative_ids(self.work_dir))
        self.assertEqual(self.store.path / "repo", default_repository(self.work_dir, "TEST-1"))

    def test_json_store_is_independent_of_pipeline_metadata(self):
        store = JsonStore(self.work_dir / ".web")
        store.write_json("settings.json", {"enabled": True})
        self.assertEqual({"enabled": True}, store.read_json("settings.json"))
        self.assertFalse((store.path / "initiative.json").exists())


if __name__ == "__main__":
    unittest.main()
