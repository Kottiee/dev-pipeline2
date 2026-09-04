from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_pipeline.agents import AgentError, AntigravityAgent, create_agent


def agy_output(answer, status="SUCCESS", error="", usage=None):
    usage = usage or {"input_tokens": 100, "output_tokens": 20,
                      "thinking_tokens": 5, "cache_read_tokens": 50,
                      "total_tokens": 120}
    result = {"conversation_id": "cid-1", "status": status, "response": answer,
              "error": error, "duration_seconds": 1.0, "num_turns": 1,
              "usage": usage}
    return "\n".join(json.dumps(event) for event in [
        {"event": "init", "conversation_id": "cid-1", "init": {"cwd": "/work"}},
        {"event": "step_update", "step_update": {"conversation_id": "cid-1",
                                                 "step_index": 0, "state": "DONE",
                                                 "step_type": "user_input"}},
        {"event": "step_update", "step_update": {"conversation_id": "cid-1",
                                                 "step_index": 1, "state": "DONE",
                                                 "step_type": "agent_response",
                                                 "text_delta": answer}},
        {"event": "result", "result": result},
    ])


def completed(output, code=0, stderr=""):
    return subprocess.CompletedProcess("agy", code, output, stderr)


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.logs = self.repo / "logs"
        self.agent = AntigravityAgent(log_dir=self.logs)

    def test_parses_stream_and_uses_repository_environment(self):
        with patch("dev_pipeline.agents.process.subprocess.run", return_value=completed(
            agy_output('{"result":"PASS","findings":[]}'), stderr="diagnostic"
        )) as run:
            result = self.agent.execute("review", self.repo)
        self.assertEqual("PASS", result.data["result"])
        args = run.call_args.args[0]
        self.assertEqual(["agy", "--input-format", "stream-json",
                          "--output-format", "stream-json"], args)
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual("user", payload["event"])
        self.assertEqual("review", payload["message"]["content"])
        self.assertEqual(str(self.repo), run.call_args.kwargs["env"]["PWD"])
        self.assertEqual(self.repo, run.call_args.kwargs["cwd"])
        log = json.loads(next(self.logs.glob("*.json")).read_text())
        self.assertEqual("diagnostic", log["stderr"])

    def test_command_override_preserves_stream_protocol(self):
        agent = create_agent("antigravity", "agy-wrapper --dangerously-skip-permissions")
        with patch("dev_pipeline.agents.process.subprocess.run", return_value=completed(
            agy_output('{"result":"GOAL_REACHED"}'))) as run:
            data = agent.execute("implement", self.repo).data
        self.assertEqual("GOAL_REACHED", data["result"])
        self.assertEqual(["agy-wrapper", "--dangerously-skip-permissions",
                          "--input-format", "stream-json",
                          "--output-format", "stream-json"],
                         run.call_args.args[0])

    def test_extracts_trailing_json_from_prose_answer(self):
        answer = 'Let me verify.\n\n{"result":"FINDINGS","findings":[{"severity":"low",' \
                 '"description":"edge case","recommendation":"handle it"}]}'
        with patch("dev_pipeline.agents.process.subprocess.run",
                   return_value=completed(agy_output(answer))):
            result = self.agent.execute("review", self.repo)
        self.assertEqual("FINDINGS", result.data["result"])
        self.assertEqual(1, len(result.data["findings"]))

    def test_non_success_status_is_an_error(self):
        output = agy_output("", status="ERROR", error="authentication required")
        with patch("dev_pipeline.agents.process.subprocess.run",
                   return_value=completed(output)) as run:
            with self.assertRaisesRegex(AgentError, "status ERROR.*authentication required"):
                self.agent.execute("implement", self.repo)
        run.assert_called_once()

    def test_missing_result_event_is_an_error(self):
        output = json.dumps({"event": "init", "conversation_id": "cid",
                             "init": {"cwd": "/work"}})
        with patch("dev_pipeline.agents.process.subprocess.run",
                   return_value=completed(output)) as run:
            with self.assertRaisesRegex(AgentError, "without a result event.*agent log:"):
                self.agent.execute("implement", self.repo)
        run.assert_called_once()

    def test_malformed_answer_includes_log_path(self):
        with patch("dev_pipeline.agents.process.subprocess.run",
                   return_value=completed(agy_output("not JSON"))) as run:
            with self.assertRaisesRegex(AgentError, "valid JSON object.*agent log:"):
                self.agent.execute("implement", self.repo)
        self.assertEqual(1, len(list(self.logs.glob("*.json"))))

    def test_process_error_is_logged_without_retry(self):
        with patch("dev_pipeline.agents.process.subprocess.run",
                   return_value=completed("partial", 1, "auth failed")) as run:
            with self.assertRaisesRegex(AgentError, "auth failed.*agent log:"):
                self.agent.execute("implement", self.repo)
        run.assert_called_once()
        self.assertEqual("partial", json.loads(next(self.logs.glob("*.json")).read_text())["stdout"])


if __name__ == "__main__":
    unittest.main()
