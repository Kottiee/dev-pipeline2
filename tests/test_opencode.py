from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_pipeline.agents import AgentError, CommandAgent, OpenCodeAgent


def events(answer, reason="stop", session="session-1"):
    return "\n".join(json.dumps(event) for event in [
        {"type": "step_start", "sessionID": session, "part": {"messageID": "msg-1"}},
        {"type": "text", "sessionID": session,
         "part": {"messageID": "msg-1", "text": answer}},
        {"type": "step_finish", "sessionID": session, "part": {"reason": reason}},
    ])


def completed(output, code=0, stderr=""):
    return subprocess.CompletedProcess("opencode", code, output, stderr)


class OpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.logs = self.repo / "logs"
        self.agent = OpenCodeAgent(log_dir=self.logs)

    def test_events_and_working_directory(self):
        with patch("dev_pipeline.agents.process.subprocess.run", return_value=completed(
            events('{"result":"PASS","findings":[]}'), stderr="diagnostic"
        )) as run:
            result = self.agent.execute("review", self.repo)
        self.assertEqual("PASS", result.data["result"])
        self.assertEqual(["opencode", "run", "--format", "json", "--dir", str(self.repo)],
                         run.call_args.args[0])
        self.assertEqual(str(self.repo), run.call_args.kwargs["env"]["PWD"])
        self.assertEqual(self.repo, run.call_args.kwargs["cwd"])
        self.assertEqual("review", run.call_args.kwargs["input"])
        log = json.loads(next(self.logs.glob("*.json")).read_text())
        self.assertEqual("diagnostic", log["stderr"])

    def test_repairs_malformed_answer_in_same_session_without_restarting_work(self):
        malformed = '{"result":"FINDINGS","findings":[{"description":"value "x" lost"}]}'
        correct = {"result": "FINDINGS", "findings": [{"description": 'value "x" lost'}]}
        with patch("dev_pipeline.agents.process.subprocess.run", side_effect=[
            completed(events(malformed)), completed(events(json.dumps(correct))),
        ]) as run:
            result = self.agent.execute("implement and review", self.repo)
        self.assertEqual(correct, result.data)
        self.assertEqual(["--session", "session-1"], run.call_args.args[0][-2:])
        self.assertIn("Do not call tools", run.call_args.kwargs["input"])
        self.assertIn(malformed, run.call_args.kwargs["input"])
        self.assertNotEqual("implement and review", run.call_args.kwargs["input"])
        self.assertEqual(2, len(list(self.logs.glob("*.json"))))

    def test_format_retries_are_bounded_and_failure_is_logged(self):
        with patch("dev_pipeline.agents.process.subprocess.run",
                   return_value=completed(events("invalid JSON"))) as run:
            with self.assertRaisesRegex(AgentError, "agent log:"):
                self.agent.execute("review", self.repo)
        self.assertEqual(3, run.call_count)
        self.assertEqual(3, len(list(self.logs.glob("*.json"))))

    def test_permission_denial_resumes_with_allowed_alternative_not_permission_override(self):
        output = events('{"result":"PASS"}', reason="tool-calls") + "\n" + json.dumps({
            "type": "tool_use", "part": {"state": {
                "status": "error", "error": "The user rejected permission to use this tool call."}},
        })
        with patch("dev_pipeline.agents.process.subprocess.run", side_effect=[
            completed(output), completed(events('{"result":"FINDINGS"}')),
        ]) as run:
            result = self.agent.execute("review", self.repo)
        self.assertEqual("FINDINGS", result.data["result"])
        self.assertEqual(["--session", "session-1"], run.call_args.args[0][-2:])
        self.assertNotIn("--auto", run.call_args.args[0])
        self.assertIn("do not retry", run.call_args.kwargs["input"])
        self.assertIn("report the blocker", run.call_args.kwargs["input"])

    def test_repeated_permission_interruption_stops(self):
        output = events("", "tool-calls") + "\n" + json.dumps({
            "type": "tool_use", "part": {"state": {
                "status": "error", "error": "The user rejected permission to use this tool."}},
        })
        with patch("dev_pipeline.agents.process.subprocess.run", return_value=completed(output)) as run:
            with self.assertRaisesRegex(AgentError, "permission interruption.*agent log:"):
                self.agent.execute("review", self.repo)
        self.assertEqual(3, run.call_count)

    def test_api_error_even_with_zero_exit_code_is_not_a_result(self):
        output = events('{"result":"PASS"}') + "\n" + json.dumps({
            "type": "error", "error": {"name": "APIError", "message": "rate limited"},
        })
        with patch("dev_pipeline.agents.process.subprocess.run", return_value=completed(output)) as run:
            with self.assertRaisesRegex(AgentError, "rate limited"):
                self.agent.execute("review", self.repo)
        run.assert_called_once()

    def test_process_error_is_logged_without_retry(self):
        with patch("dev_pipeline.agents.process.subprocess.run",
                   return_value=completed("partial", 1, "auth failed")) as run:
            with self.assertRaisesRegex(AgentError, "auth failed.*agent log:"):
                self.agent.execute("review", self.repo)
        run.assert_called_once()
        self.assertEqual("partial", json.loads(next(self.logs.glob("*.json")).read_text())["stdout"])

    def test_only_final_step_text_is_used_not_tool_output(self):
        output = events('{"result":"PASS"}', "tool-calls") + "\n" + json.dumps({
            "type": "tool_use", "part": {"state": {"status": "completed",
                "output": '{"result":"PASS"}'}}}) + "\n" + events('{"result":"FINDINGS"}')
        answer, _ = self.agent._opencode_answer(output)
        self.assertEqual('{"result":"FINDINGS"}', answer)

    def test_truncated_events_are_rejected(self):
        with self.assertRaisesRegex(AgentError, "invalid OpenCode JSON event"):
            self.agent._opencode_answer('{"type":')

    def test_custom_command_keeps_plain_json_contract(self):
        agent = CommandAgent("custom-agent --flag")
        with patch("dev_pipeline.agents.process.subprocess.run",
                   return_value=completed('{"result":"PASS"}')) as run:
            self.assertEqual("PASS", agent.execute("prompt", self.repo).data["result"])
        self.assertEqual(["custom-agent", "--flag"], run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
