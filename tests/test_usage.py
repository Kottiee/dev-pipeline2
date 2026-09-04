from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_pipeline.agents import AgentResult, create_agent
from dev_pipeline.agents.base import AgentError
from dev_pipeline.cli import _show, main
from dev_pipeline.pipeline import Pipeline
from dev_pipeline.store import WorkStore
from dev_pipeline.usage import (
    TOKEN_USAGE_EXTRACTORS, extract_token_usage, format_token_usage,
    resolve_token_usage_format, summarize_token_usage,
)

from tests.test_pipeline import PHASES


def codex_line(**usage) -> str:
    event = {"type": "turn.completed", "usage": {}}
    event["usage"].update({key: value for key, value in usage.items()})
    return json.dumps(event)


def codex_output(*events: str) -> str:
    return "\n".join(events)


class UsageExtractionTests(unittest.TestCase):
    def test_codex_extracts_totals_and_reported_events(self):
        output = codex_output(
            "not json {",
            json.dumps({"type": "message", "usage": {"input_tokens": 999}}),
            codex_line(input_tokens=100, cached_input_tokens=10,
                       output_tokens=50, reasoning_output_tokens=5),
            codex_line(input_tokens=30, output_tokens=20),
            codex_line(input_tokens=-1, output_tokens=20),
            codex_line(input_tokens=True, output_tokens=20),
            codex_line(input_tokens="50", output_tokens=20),
        )
        usage = extract_token_usage(output, "codex")
        self.assertEqual(130, usage["input_tokens"])
        self.assertEqual(10, usage["cached_input_tokens"])
        self.assertEqual(70, usage["output_tokens"])
        self.assertEqual(5, usage["reasoning_output_tokens"])
        self.assertEqual(200, usage["total_tokens"])
        self.assertEqual(2, usage["reported_invocations"])

    def test_codex_missing_usage_returns_none(self):
        output = codex_output("{}", "plain text", json.dumps({"type": "turn.completed"}))
        self.assertIsNone(extract_token_usage(output, "codex"))

    def test_opencode_normalizes_cache_and_reasoning_into_totals(self):
        step = {"type": "step_finish", "part": {"tokens": {
            "input": 10, "cache": {"read": 5, "write": 3},
            "output": 8, "reasoning": 2,
        }}}
        output = "\n".join(json.dumps(step) for _ in range(2))
        usage = extract_token_usage(output, "opencode")
        self.assertEqual(36, usage["input_tokens"])
        self.assertEqual(10, usage["cached_input_tokens"])
        self.assertEqual(20, usage["output_tokens"])
        self.assertEqual(4, usage["reasoning_output_tokens"])
        self.assertEqual(56, usage["total_tokens"])
        self.assertEqual(1, usage["reported_invocations"])

    def test_opencode_ignores_invalid_steps_but_keeps_valid_ones(self):
        valid = {"type": "step_finish", "part": {"tokens": {
            "input": 4, "cache": {}, "output": 2, "reasoning": 0}}}
        invalid = [
            {"type": "step_finish", "part": {"tokens": {"input": "4"}}},
            {"type": "step_finish", "part": {"tokens": {"output": True}}},
            {"type": "step_finish", "part": {"tokens": {"input": -1}}},
            {"type": "step_finish", "part": {}},
            {"type": "text", "part": {"text": "ignored"}},
            "not json",
        ]
        output = "\n".join([json.dumps(valid), *[json.dumps(x) if not isinstance(x, str) else x for x in invalid]])
        usage = extract_token_usage(output, "opencode")
        self.assertEqual({"input_tokens": 4, "cached_input_tokens": 0,
                          "output_tokens": 2, "reasoning_output_tokens": 0,
                          "total_tokens": 6, "reported_invocations": 1}, usage)

    def test_opencode_with_no_valid_events_returns_none(self):
        output = json.dumps({"type": "step_finish", "part": {"tokens": {"input": -1}}})
        self.assertIsNone(extract_token_usage(output, "opencode"))

    def test_summarize_recomputes_total_and_ignores_bad_rows(self):
        summary = summarize_token_usage([
            {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5,
             "reasoning_output_tokens": 1, "reported_invocations": 1},
            {"input_tokens": 20, "output_tokens": 15, "total_tokens": 999},
            None,
            {"input_tokens": -1},
            {"input_tokens": "bad", "reported_invocations": True},
        ])
        self.assertEqual(30, summary["input_tokens"])
        self.assertEqual(2, summary["cached_input_tokens"])
        self.assertEqual(20, summary["output_tokens"])
        self.assertEqual(1, summary["reasoning_output_tokens"])
        self.assertEqual(50, summary["total_tokens"])
        self.assertEqual(3, summary["reported_invocations"])

    def test_resolve_uses_explicit_then_backend_then_default(self):
        self.assertEqual("codex", resolve_token_usage_format(None, None))
        self.assertEqual("codex", resolve_token_usage_format(None, "codex"))
        self.assertEqual("opencode", resolve_token_usage_format(None, "opencode"))
        self.assertEqual("opencode", resolve_token_usage_format("opencode", "codex"))
        self.assertEqual("codex", resolve_token_usage_format("codex", "opencode"))
        with self.assertRaisesRegex(ValueError, "unsupported token usage format"):
            resolve_token_usage_format("bogus", None)

    def test_format_token_usage_line_and_unavailable(self):
        summary = {"input_tokens": 1234, "cached_input_tokens": 56,
                   "output_tokens": 7890, "reasoning_output_tokens": 12,
                   "total_tokens": 9124, "reported_invocations": 3}
        self.assertEqual(
            "Token usage: 9,124 total (input 1,234, output 7,890, cached 56, reasoning 12)",
            format_token_usage(summary),
        )
        self.assertEqual("Token usage: unavailable",
                         format_token_usage({"reported_invocations": 0}))

    def test_adapters_declare_output_formats(self):
        self.assertEqual("codex", create_agent("codex").token_usage_format)
        self.assertEqual("opencode", create_agent("opencode").token_usage_format)
        self.assertEqual("codex", create_agent("command", "other-cli").token_usage_format)


class UsagePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorkStore(self.root / "work", "INIT-001")
        Pipeline.initialize(self.store, "ship", ["works"], [], PHASES)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, usage):
        self.store.record_usage_event({
            "stage": "run", "phase": "phase-01", "run": 1, "usage": usage,
            "recorded_at": "2026-09-04T00:00:00+00:00",
        })

    def test_record_accumulates_and_never_double_counts(self):
        self.record({"input_tokens": 100, "output_tokens": 50, "reported_invocations": 1})
        self.assertEqual(150, self.store.load_token_usage()["total_tokens"])
        self.record({"input_tokens": 30, "output_tokens": 20, "reported_invocations": 1})
        self.assertEqual(200, self.store.load_token_usage()["total_tokens"])
        self.assertEqual(2, self.store.load_token_usage()["reported_invocations"])
        raw = self.store.read_json("usage.json")
        self.assertEqual(2, len(raw["events"]))
        self.assertEqual(200, raw["token_usage"]["total_tokens"])

    def test_load_token_usage_is_empty_before_records(self):
        usage = self.store.load_token_usage()
        self.assertEqual(0, usage["total_tokens"])
        self.assertEqual(0, usage["reported_invocations"])

    def test_usage_is_recomputed_from_events_on_load(self):
        self.record({"input_tokens": 5, "output_tokens": 5, "reported_invocations": 1})
        events = self.store._usage_events()
        events[0]["usage"] = {"input_tokens": 500, "output_tokens": 500,
                              "reported_invocations": 1}
        self.store.write_json("usage.json", {"events": events, "token_usage": {}})
        self.assertEqual(1000, self.store.load_token_usage()["total_tokens"])


class CodexRunAgent:
    def __init__(self, stdout):
        self.stdout = stdout

    def execute(self, prompt, repository):
        data = {"result": "CONTINUE", "completed": ["core"], "remaining": [],
                "problems": [], "recommended_next_action": "next"}
        return AgentResult(data, self.stdout, "")


class UsagePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WorkStore(self.root / "work", "INIT-001")
        Pipeline.initialize(self.store, "ship", ["works"], [], PHASES)

    def tearDown(self):
        self.temp.cleanup()

    def test_run_records_usage_from_agent_stdout(self):
        stdout = codex_output(
            codex_line(input_tokens=100, cached_input_tokens=10,
                       output_tokens=50, reasoning_output_tokens=5),
        )
        agent = CodexRunAgent(stdout)
        result = Pipeline(self.store, self.root, agent).run([])
        self.assertEqual("CONTINUE", result)
        events = self.store._usage_events()
        self.assertEqual(1, len(events))
        self.assertEqual("run", events[0]["stage"])
        self.assertEqual("phase-01", events[0]["phase"])
        self.assertEqual(1, events[0]["run"])
        self.assertEqual(1, self.store.load_token_usage()["reported_invocations"])

    def test_failed_execution_usage_is_recovered_from_saved_logs(self):
        log_dir = self.store.path / "agent-logs"
        log_dir.mkdir(parents=True)
        stdout = codex_output(codex_line(input_tokens=80, output_tokens=40))
        (log_dir / "deadbeef-1.json").write_text(json.dumps({
            "command": [], "repository": str(self.root), "prompt": "", "returncode": 1,
            "stdout": stdout, "stderr": "boom",
        }), encoding="utf-8")

        class FailingAgent:
            def execute(self, prompt, repository):
                raise AgentError("boom", log_dir / "deadbeef-1.json")

        pipeline = Pipeline(self.store, self.root, FailingAgent())
        with self.assertRaisesRegex(AgentError, "boom"):
            pipeline.run([])
        events = self.store._usage_events()
        self.assertEqual(1, len(events))
        self.assertEqual(80, events[0]["usage"]["input_tokens"])
        self.assertEqual(120, self.store.load_token_usage()["total_tokens"])

    def test_show_renders_totals_and_unavailable(self):
        stdout = codex_output(
            codex_line(input_tokens=1234, output_tokens=2000),
        )
        Pipeline(self.store, self.root, CodexRunAgent(stdout)).run([])
        shown = _show(self.store)
        self.assertIn("Initiative: INIT-001", shown)
        self.assertIn("Token usage: 3,234 total (input 1,234, output 2,000, cached 0, reasoning 0)", shown)
        quiet = WorkStore(self.root / "work", "QUIET-001")
        Pipeline.initialize(quiet, "quiet", [], [], PHASES)
        self.assertIn("Token usage: unavailable", _show(quiet))

    def test_init_records_planner_usage(self):
        plan = {"goal": "ship", "requirements": ["works"], "constraints": [],
                "phases": PHASES}
        stdout = codex_output(codex_line(input_tokens=50, output_tokens=25))
        with patch("dev_pipeline.agents.codex.CodexAgent.execute",
                   return_value=AgentResult(plan, stdout, "")) as execute:
            store = WorkStore(self.root / "work", "PLAN-001")
            with patch("builtins.print"):
                result = main(["--work-dir", str(self.root / "work"), "--repo", str(self.root),
                               "--agent", "codex", "init", "--id", "PLAN-001",
                               "--request", "ship something"])
        self.assertEqual(0, result)
        execute.assert_called_once()
        events = store._usage_events()
        self.assertEqual(1, len(events))
        self.assertEqual("planner", events[0]["stage"])
        self.assertEqual(75, store.load_token_usage()["total_tokens"])

    def test_unknown_explicit_token_usage_format_is_rejected(self):
        with patch("builtins.print"):
            result = main(["--token-usage-format", "bogus", "show", "X"])
        self.assertEqual(2, result)


if __name__ == "__main__":
    unittest.main()
