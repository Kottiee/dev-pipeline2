from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agents import AgentError, create_agent
from .agents.base import Agent
from .models import now_iso
from .pipeline import Pipeline, TransitionError
from .store import WorkStore
from .usage import (
    DEFAULT_TOKEN_USAGE_FORMAT, extract_token_usage, format_token_usage,
    resolve_token_usage_format,
)


def _agent(args: argparse.Namespace) -> Agent:
    log_dir = WorkStore(Path(args.work_dir), args.id).path / "agent-logs"
    return create_agent(args.agent, args.agent_command, log_dir=log_dir)


def _resolved_token_usage_format(args: argparse.Namespace, agent: Agent) -> str:
    backend = getattr(agent, "token_usage_format", DEFAULT_TOKEN_USAGE_FORMAT)
    return resolve_token_usage_format(args.token_usage_format, backend)


def _checks(values: list[str]) -> list[tuple[str, str]]:
    parsed = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"check must use NAME=COMMAND: {value}")
        parsed.append(tuple(value.split("=", 1)))
    return parsed


def _plan(args: argparse.Namespace, agent: Agent) -> tuple[str, list[str], list[str], list[dict], str | None]:
    if args.phase_file:
        phases = json.loads(Path(args.phase_file).read_text(encoding="utf-8"))
        if not isinstance(phases, list) or not phases:
            raise ValueError("phase file must contain a non-empty JSON array")
        return args.goal or args.request, args.requirement, args.constraint, phases, None
    request = args.request or args.goal
    if not request:
        raise ValueError("--request or --goal is required")
    prompt = f"""Turn this human request into an actionable initiative and one or more human-reviewable
phases. Do not implement anything. Request: {request}
Additional requirements: {args.requirement}
Constraints: {args.constraint}
Output exactly one JSON object and nothing after it with this schema:
{{"goal":"...","requirements":["..."],"constraints":["..."],"phases":[{{"goal":"...","scope":["..."],"acceptance_criteria":["..."]}}]}}
Every phase must be independently runnable and reviewable. Do not create work items.
Use the smallest number of meaningful human-review units. A small request may be one phase.
Do not split individual implementation steps or tests into separate phases
when they belong to the same usable outcome.
"""
    result = agent.execute(prompt, Path(args.repo))
    data = result.data
    keys = {"goal", "requirements", "constraints", "phases"}
    if not keys.issubset(data) or not isinstance(data["phases"], list) or not data["phases"]:
        raise ValueError("planner returned an invalid initiative")
    return data["goal"], data["requirements"], data["constraints"], data["phases"], result.stdout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dev-pipeline", description="Phase-oriented AI development pipeline")
    parser.add_argument("--work-dir", default="work", help="pipeline state directory")
    parser.add_argument("--repo", default=".", help="repository agents and checks operate in")
    parser.add_argument("--agent", default="codex", help="adapter name (codex, opencode, command, or a registered adapter)")
    parser.add_argument("--agent-command", help="override adapter command (prompt is provided on stdin)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="plan and start a new initiative")
    init.add_argument("--id", required=True)
    init.add_argument("--request", help="natural-language human request")
    init.add_argument("--goal", help="goal used with a manually supplied phase file")
    init.add_argument("--requirement", action="append", default=[])
    init.add_argument("--constraint", action="append", default=[])
    init.add_argument("--phase-file", help="JSON phase array; skips AI planning")

    parser.add_argument("--token-usage-format",
                        help="agent output format for token usage extraction "
                             "(codex, opencode, or a registered format)")

    run = sub.add_parser("run", help="execute one run and automatically verify/review when done")
    run.add_argument("id")
    run.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND")
    run.add_argument("--until-ready", action="store_true",
                     help="continue implementation and repair until Human Check is ready")

    verify = sub.add_parser("verify", help="run configured checks for the current phase")
    verify.add_argument("id")
    verify.add_argument("--check", action="append", required=True, metavar="NAME=COMMAND")

    review = sub.add_parser("review", help="adversarially review a successfully verified phase")
    review.add_argument("id")

    human = sub.add_parser("human-check", help="accept a ready phase or return feedback")
    human.add_argument("id")
    human.add_argument("result", choices=("ACCEPT", "FEEDBACK"))
    human.add_argument("--feedback")

    status = sub.add_parser("status", help="show durable pipeline state")
    status.add_argument("id")
    show = sub.add_parser("show", help="show a human-readable initiative summary with token usage")
    show.add_argument("id")
    return parser


def _show(store: WorkStore) -> str:
    state = store.load_state()
    initiative = store.load_initiative()
    lines = [
        f"Initiative: {initiative.id}",
        f"Status: {state.state}",
        f"Goal: {initiative.goal}",
    ]
    phases = []
    for phase_id in initiative.phase_order:
        try:
            phases.append(store.load_phase(phase_id))
        except ValueError:
            continue
    if state.current_phase:
        current = next((phase for phase in phases if phase.id == state.current_phase), None)
        suffix = f" ({current.status})" if current else ""
        lines.append(f"Current phase: {state.current_phase}{suffix}")
    if phases:
        lines.append("Phases:")
        lines.extend(f"  {phase.id}: {phase.status}" for phase in phases)
    lines.append(format_token_usage(store.load_token_usage()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.token_usage_format is not None:
            resolve_token_usage_format(args.token_usage_format)
        store = WorkStore(Path(args.work_dir), getattr(args, "id", ""))
        if args.command == "init":
            store = WorkStore(Path(args.work_dir), args.id)
            agent = _agent(args)
            agent.token_usage_format = _resolved_token_usage_format(args, agent)
            goal, requirements, constraints, phases, planner_output = _plan(args, agent)
            Pipeline.initialize(store, goal, requirements, constraints, phases)
            usage = extract_token_usage(planner_output, agent.token_usage_format)
            if usage is not None:
                store.record_usage_event({
                    "stage": "planner", "phase": None, "run": None,
                    "usage": usage, "recorded_at": now_iso(),
                })
            print(json.dumps(store.load_state().as_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "status":
            print(json.dumps(store.load_state().as_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "show":
            print(_show(store))
            return 0
        agent = _agent(args)
        agent.token_usage_format = _resolved_token_usage_format(args, agent)
        pipeline = Pipeline(store, Path(args.repo), agent)
        if args.command == "run":
            checks = _checks(args.check)
            if args.until_ready:
                print(pipeline.run_until_ready(checks))
            else:
                print(pipeline.run(checks))
        elif args.command == "verify":
            passed = pipeline.verify(_checks(args.check))
            print("PASS" if passed else "FAIL")
            return 0 if passed else 1
        elif args.command == "review":
            passed = pipeline.review()
            print("PASS" if passed else "FINDINGS")
            return 0 if passed else 1
        elif args.command == "human-check":
            print(pipeline.human_check(args.result, args.feedback))
        return 0
    except (AgentError, TransitionError, ValueError, FileNotFoundError, FileExistsError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
