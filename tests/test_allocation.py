from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dev_pipeline.allocation import (
    _collect_antigravity,
    _collect_codex,
    collect_agent_allocation,
    format_allocation_usage,
    summarize_allocation_usage,
)
from dev_pipeline.pipeline import Pipeline
from dev_pipeline.store import WorkStore

from tests.test_pipeline import PHASES


class AllocationCollectorTests(unittest.TestCase):
    def test_codex_collects_all_rate_limit_windows(self):
        lines = [
            json.dumps({"id": 1, "result": {}}) + "\n",
            json.dumps({"method": "unrelated", "params": {}}) + "\n",
            json.dumps({"id": 2, "result": {"rateLimitsByLimitId": {"codex": {
                "limitId": "codex",
                "primary": {"usedPercent": 12, "windowDurationMins": 300,
                            "resetsAt": 1788609662},
                "secondary": {"usedPercent": 34, "windowDurationMins": 10080,
                              "resetsAt": 1788883515},
            }}}}) + "\n",
        ]
        process = Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO("".join(lines))
        process.wait.return_value = 0
        with patch("dev_pipeline.allocation.subprocess.Popen", return_value=process):
            result = _collect_codex("codex exec -")
        self.assertEqual([12.0, 34.0], [b["used_percent"] for b in result["buckets"]])
        self.assertEqual("codex 5h", result["buckets"][0]["label"])
        process.terminate.assert_called_once()

    def test_antigravity_converts_remaining_fraction_to_used_percent(self):
        payload = {"command": {"data": {"groups": [{
            "name": "Gemini Models",
            "buckets": [{"id": "gemini-weekly", "window": "weekly",
                         "remaining_fraction": .9237,
                         "reset_time": "2026-09-11T01:41:29Z"}],
        }]}}}
        completed = Mock(returncode=0, stdout=json.dumps(payload))
        with patch("dev_pipeline.allocation.subprocess.run", return_value=completed) as run:
            result = _collect_antigravity("agy --dangerously-skip-permissions")
        self.assertEqual(7.63, result["buckets"][0]["used_percent"])
        self.assertEqual("Gemini Models weekly", result["buckets"][0]["label"])
        self.assertEqual("/usage", run.call_args.args[0][-3])

    def test_collection_failure_is_non_fatal(self):
        agent = Mock(allocation_usage_format="codex", allocation_command="missing")
        with patch.dict("dev_pipeline.allocation.ALLOCATION_COLLECTORS",
                        {"codex": Mock(side_effect=OSError("missing"))}):
            self.assertIsNone(collect_agent_allocation(agent))


class AllocationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkStore(Path(self.temp.name) / "work", "INIT-001")
        Pipeline.initialize(self.store, "ship", [], [], PHASES)

    def tearDown(self):
        self.temp.cleanup()

    def test_latest_snapshot_per_provider_is_persisted_and_formatted(self):
        for used in (10, 25):
            self.store.record_usage_event({
                "allocation_usage": {"provider": "codex", "buckets": [
                    {"id": "codex:primary", "label": "codex 5h", "used_percent": used},
                ]},
            })
        summary = self.store.load_allocation_usage()
        self.assertEqual(25.0, summary["codex"]["buckets"][0]["used_percent"])
        self.assertEqual("Allocation usage: codex 5h: 25% used",
                         format_allocation_usage(summary))
        raw = self.store.read_json("usage.json")
        self.assertEqual(summary, raw["allocation_usage"])

    def test_invalid_snapshots_are_ignored(self):
        summary = summarize_allocation_usage([
            {"allocation_usage": {"provider": "codex", "buckets": [
                {"id": "bad", "label": "bad", "used_percent": 101},
            ]}},
        ])
        self.assertEqual({}, summary)
        self.assertEqual("Allocation usage: unavailable", format_allocation_usage(summary))

    def test_pipeline_records_allocation_even_without_token_event(self):
        class AllocationAgent:
            allocation_usage_format = "codex"
            allocation_command = "codex"

            def execute(self, prompt, repository):
                from dev_pipeline.agents import AgentResult
                data = {"result": "CONTINUE", "completed": [], "remaining": ["work"],
                        "problems": [], "recommended_next_action": "continue"}
                return AgentResult(data, json.dumps(data), "")

        allocation = {"provider": "codex", "buckets": [
            {"id": "codex:primary", "label": "codex 5h", "used_percent": 20},
        ]}
        with patch("dev_pipeline.pipeline.collect_agent_allocation",
                   return_value=allocation):
            Pipeline(self.store, Path(self.temp.name), AllocationAgent()).run([])
        usage = self.store.load_allocation_usage()
        self.assertEqual(20.0, usage["codex"]["buckets"][0]["used_percent"])
        self.assertEqual(0, self.store.load_token_usage()["reported_invocations"])


if __name__ == "__main__":
    unittest.main()
