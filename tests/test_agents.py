from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_pipeline.agents import (
    AGENT_FACTORIES, AgentResult, CodexAgent, CommandAgent, OpenCodeAgent, create_agent,
)
from dev_pipeline.cli import main
from dev_pipeline.store import WorkStore
from tests.test_opencode import completed, events


class AdapterSelectionTests(unittest.TestCase):
    def test_builtins_are_separate_adapters(self):
        self.assertIsInstance(create_agent("codex"), CodexAgent)
        self.assertIsInstance(create_agent("opencode"), OpenCodeAgent)
        self.assertIsInstance(create_agent("command", "other-cli"), CommandAgent)

    def test_unknown_cli_with_command_uses_generic_adapter(self):
        self.assertIsInstance(create_agent("other", "other-cli"), CommandAgent)
        with self.assertRaisesRegex(ValueError, "unknown agent"):
            create_agent("other")
        with self.assertRaisesRegex(ValueError, "requires --agent-command"):
            create_agent("command")

    def test_codex_command_override_keeps_plain_json_protocol(self):
        agent = create_agent("codex", "codex-wrapper -")
        with patch("dev_pipeline.agents.process.subprocess.run",
                   return_value=completed('{"result":"PASS"}')) as run:
            self.assertEqual("PASS", agent.execute("prompt", Path(".")).data["result"])
        self.assertEqual(["codex-wrapper", "-"], run.call_args.args[0])

    def test_opencode_command_override_preserves_events_and_recovery(self):
        agent = create_agent("opencode", "opencode-wrapper run -m provider/model")
        with patch("dev_pipeline.agents.process.subprocess.run", side_effect=[
            completed(events("invalid")), completed(events('{"result":"PASS"}')),
        ]) as run:
            self.assertEqual("PASS", agent.execute("prompt", Path(".")).data["result"])
        args = run.call_args.args[0]
        self.assertEqual(["opencode-wrapper", "run", "-m", "provider/model"], args[:4])
        self.assertIn("--format", args)
        self.assertEqual(["--session", "session-1"], args[-2:])

    def test_registered_agent_completes_cli_lifecycle_without_concrete_base_class(self):
        plan = {"goal": "ship", "requirements": ["works"], "constraints": [], "phases": [
            {"goal": "usable outcome", "scope": ["feature"], "acceptance_criteria": ["works"]},
        ]}
        replies = iter([plan,
            {"result": "GOAL_REACHED", "completed": ["feature"], "remaining": [],
             "problems": [], "recommended_next_action": "review"},
            {"result": "PASS", "findings": []},
        ])
        calls = []

        class AlternateAgent:
            def execute(self, prompt, repository):
                calls.append((prompt, repository))
                data = next(replies)
                return AgentResult(data, json.dumps(data), "")

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            args = ["--agent", "alternate", "--repo", str(repo), "--work-dir", str(repo / "work")]
            factory = lambda command, log_dir: AlternateAgent()
            with patch.dict(AGENT_FACTORIES, {"alternate": factory}), patch("builtins.print"):
                self.assertEqual(0, main(args + ["init", "--id", "TEST", "--request", "ship"]))
                check = "test=" + shlex.join([sys.executable, "-c", "print('ok')"])
                self.assertEqual(0, main(args + ["run", "TEST", "--until-ready", "--check", check]))
                self.assertEqual(0, main(args + ["human-check", "TEST", "ACCEPT"]))
            self.assertEqual("COMPLETED", WorkStore(repo / "work", "TEST").load_state().state)
        self.assertEqual(3, len(calls))
        self.assertTrue(all(repository == repo for _, repository in calls))

    def test_generic_adapter_runs_real_process_with_stdin_cwd_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            program = (
                "import json,os,sys; "
                "print(json.dumps({'prompt':sys.stdin.read(),'cwd':os.getcwd(),'pwd':os.environ['PWD']}))"
            )
            command = shlex.join([sys.executable, "-c", program])
            agent = create_agent("other", command, repo / "logs")
            result = agent.execute("test input", repo)
            self.assertEqual({"prompt": "test input", "cwd": str(repo), "pwd": str(repo)}, result.data)
            self.assertEqual(1, len(list((repo / "logs").glob("*.json"))))

    def test_generic_parse_errors_include_log_path(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            agent = create_agent("command", "other-cli", repo / "logs")
            with patch("dev_pipeline.agents.process.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, "not JSON", "")):
                with self.assertRaisesRegex(RuntimeError, "agent log:"):
                    agent.execute("test", repo)
