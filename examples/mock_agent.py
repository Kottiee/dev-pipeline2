#!/usr/bin/env python3
"""Minimal mock agent adapter for E2E testing of dev-pipeline.

Simulates AI planning, implementation, and adversarial review responses without
requiring external LLM APIs. Supports simulating failures with environment variables.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    prompt = sys.stdin.read()

    # 1. Adversarial Review Prompt
    if "adversarial reviewer" in prompt.lower():
        if os.environ.get("MOCK_REVIEW_FAIL") == "1":
            print(json.dumps({
                "result": "FINDINGS",
                "findings": [{
                    "severity": "medium",
                    "description": "Mock finding for testing feedback loops",
                    "recommendation": "Fix the reported mock finding"
                }]
            }))
            return

        print(json.dumps({
            "result": "PASS",
            "findings": []
        }))
        return

    # 2. Planner Prompt
    if "turn this human request" in prompt.lower():
        print(json.dumps({
            "goal": "E2E Verification Goal",
            "requirements": ["Requirement verified"],
            "constraints": [],
            "phases": [
                {
                    "goal": "Implement and verify minimum viable changes",
                    "scope": ["core"],
                    "acceptance_criteria": ["Code changes are committed and verified"]
                }
            ]
        }))
        return

    # 3. Implementation Run Prompt
    repo = Path.cwd()
    dummy_file = repo / "e2e_artifact.txt"
    with open(dummy_file, "a", encoding="utf-8") as f:
        f.write("Run output generated\\n")

    subprocess.run(["git", "add", "."], check=False)
    subprocess.run(
        ["git", "-c", "user.name=E2E-Agent", "-c", "user.email=e2e@agent.local", "commit", "-m", "mock agent automated changes"],
        check=False,
    )

    print(json.dumps({
        "result": "GOAL_REACHED",
        "completed": ["core"],
        "remaining": [],
        "problems": [],
        "recommended_next_action": "verify"
    }))


if __name__ == "__main__":
    main()
