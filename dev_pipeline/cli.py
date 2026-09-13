from __future__ import annotations

import argparse
import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

from .agents import AgentError, create_agent
from .agents.base import Agent
from .allocation import collect_agent_allocation, format_allocation_usage
from .models import Issue, Project, now_iso
from .pipeline import Pipeline, TransitionError
from .prompts import render_prompt
from .reporting import read_issue, read_pipeline, read_project
from .store import IssueStore, ProjectStore, WorkStore
from .usage import (
    DEFAULT_TOKEN_USAGE_FORMAT, extract_token_usage, format_token_usage,
    resolve_token_usage_format,
)


def _agent(args: argparse.Namespace, custom_store: IssueStore | WorkStore | None = None) -> Agent:
    if custom_store is not None:
        log_dir = custom_store.path / "agent-logs"
    else:
        log_dir = WorkStore(Path(args.work_dir), getattr(args, "id", "")).path / "agent-logs"
    agent_name = getattr(args, "agent", None) or "codex"
    agent_command = getattr(args, "agent_command", None)
    return create_agent(agent_name, agent_command, log_dir=log_dir)


def _resolved_token_usage_format(args: argparse.Namespace, agent: Agent) -> str:
    backend = getattr(agent, "token_usage_format", DEFAULT_TOKEN_USAGE_FORMAT)
    return resolve_token_usage_format(getattr(args, "token_usage_format", None), backend)


def _checks(values: list[str]) -> list[tuple[str, str]]:
    parsed = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"check must use NAME=COMMAND: {value}")
        parsed.append(tuple(value.split("=", 1)))
    return parsed


def _repository(store: IssueStore | WorkStore, repo: str | None) -> Path:
    if repo is not None:
        return Path(repo)
    if hasattr(store, "project_store") and store.project_store.exists():
        return store.project_store.repository_path()
    return store.path / "repo"


def _plan(args: argparse.Namespace, agent: Agent, repository: Path,
          resolved_issues: str = "none") -> tuple[str, list[dict], str | None]:
    if getattr(args, "phase_file", None):
        phases = json.loads(Path(args.phase_file).read_text(encoding="utf-8"))
        if not isinstance(phases, list) or not phases:
            raise ValueError("phase file must contain a non-empty JSON array")
        return args.request or getattr(args, "goal", None) or "", phases, None
    request = getattr(args, "request", None) or getattr(args, "goal", None)
    if not request:
        raise ValueError("--request is required")
    prompt = render_prompt("planner", request=request,
                           resolved_issues=resolved_issues)
    result = agent.execute(prompt, repository)
    data = result.data
    keys = {"description", "phases"}
    if not keys.issubset(data) or not isinstance(data["phases"], list) or not data["phases"]:
        raise ValueError("planner returned an invalid issue")
    planner_output = f"{result.stdout}\n{result.stderr}".strip() if result.stderr.strip() else result.stdout
    return data["description"], data["phases"], planner_output


def _project_resolved_context(p_store: ProjectStore) -> str:
    resolved = []
    for issue_id in p_store.list_issue_ids():
        try:
            istore = p_store.issue_store(issue_id)
            st = istore.load_state()
            if st.state in {"RESOLVED", "COMPLETED"}:
                iss = istore.load_issue()
                resolved.append(f"- Issue {issue_id}: {iss.description}")
        except Exception:
            pass
    return "\n".join(resolved) if resolved else "none"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dev-pipeline", description="Phase-oriented AI development pipeline")
    parser.add_argument("--work-dir", default="work", help="pipeline state directory")
    parser.add_argument("--repo", default=None,
                        help="repository agents and checks operate in (default: <work-dir>/<id>/repo; auto-created for init)")
    parser.add_argument("--agent", default="codex", help="adapter name (codex, opencode, command, or a registered adapter)")
    parser.add_argument("--agent-command", help="override adapter command (prompt is provided on stdin)")
    parser.add_argument("--token-usage-format",
                        help="agent output format for token usage extraction (codex, opencode, or a registered format)")
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. Project subcommands
    p_parser = sub.add_parser("project", help="manage projects")
    p_sub = p_parser.add_subparsers(dest="subcommand", required=True)

    p_init = p_sub.add_parser("init", help="create a new project")
    p_init.add_argument("project_id")
    p_init.add_argument("--repo", help="path to repository")
    p_init.add_argument("--agent", default="codex", help="default agent adapter")
    p_init.add_argument("--agent-command", help="override agent command")
    p_init.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND",
                        help="register project verification checks")

    p_list = p_sub.add_parser("list", help="list projects")

    p_show = p_sub.add_parser("show", help="show project details")
    p_show.add_argument("project_id")

    # 2. Issue subcommands
    i_parser = sub.add_parser("issue", help="manage issues within a project")
    i_sub = i_parser.add_subparsers(dest="subcommand", required=True)

    i_create = i_sub.add_parser("create", help="create and start a new issue")
    i_create.add_argument("project_id")
    i_create.add_argument("--id", help="issue identifier (default: auto-generated, e.g. 0001)")
    i_create.add_argument("--request", help="natural-language description of what to build")
    i_create.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND",
                          help="register verification checks on the issue")
    i_create.add_argument("--phase-file", help="JSON phase array; skips AI planning")
    i_create.add_argument("--plan-only", action="store_true",
                          help="stop after planning instead of automatically starting first run")
    i_create.add_argument("--agent", help="override agent adapter")
    i_create.add_argument("--agent-command", help="override agent command")
    i_create.add_argument("--repo", help="override repository path")

    i_run = i_sub.add_parser("run", help="execute one run and automatically verify/review when done")
    i_run.add_argument("project_id")
    i_run.add_argument("issue_id")
    i_run.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND")
    i_run.add_argument("--until-ready", action="store_true",
                       help="continue implementation and repair until Human Check is ready")
    i_run.add_argument("--max-review-retries", type=int, default=2,
                       help="maximum review retries before auto-passing verified phase")
    i_run.add_argument("--no-verify", action="store_true",
                       help="skip mechanical verification and proceed straight to review")
    i_run.add_argument("--agent", help="override agent adapter")
    i_run.add_argument("--agent-command", help="override agent command")
    i_run.add_argument("--repo", help="override repository path")

    i_verify = i_sub.add_parser("verify", help="run configured checks for the current phase")
    i_verify.add_argument("project_id")
    i_verify.add_argument("issue_id")
    i_verify.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND")
    i_verify.add_argument("--repo", help="override repository path")

    i_review = i_sub.add_parser("review", help="adversarially review a successfully verified phase")
    i_review.add_argument("project_id")
    i_review.add_argument("issue_id")
    i_review.add_argument("--agent", help="override agent adapter")
    i_review.add_argument("--agent-command", help="override agent command")
    i_review.add_argument("--repo", help="override repository path")

    i_human = i_sub.add_parser("human-check", help="accept a ready phase or return feedback")
    i_human.add_argument("project_id")
    i_human.add_argument("issue_id")
    i_human.add_argument("result", choices=("ACCEPT", "FEEDBACK"))
    i_human.add_argument("--feedback")
    i_human.add_argument("--expected-revision", help="reject a decision made against stale state")

    i_list = i_sub.add_parser("list", help="list issues in a project")
    i_list.add_argument("project_id")

    i_show = i_sub.add_parser("show", help="show human-readable issue summary")
    i_show.add_argument("project_id")
    i_show.add_argument("issue_id")

    i_status = i_sub.add_parser("status", help="show durable issue state")
    i_status.add_argument("project_id")
    i_status.add_argument("issue_id")

    # 3. Legacy top-level commands
    init = sub.add_parser("init", help="legacy plan and start")
    init.add_argument("--id", required=True)
    init.add_argument("--request", help="natural-language description of what to build")
    init.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND")
    init.add_argument("--phase-file", help="JSON phase array")
    init.add_argument("--plan-only", action="store_true")

    run = sub.add_parser("run", help="legacy run")
    run.add_argument("id")
    run.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND")
    run.add_argument("--until-ready", action="store_true")
    run.add_argument("--max-review-retries", type=int, default=2,
                     help="maximum review retries before auto-passing verified phase")
    run.add_argument("--no-verify", action="store_true")

    verify = sub.add_parser("verify", help="legacy verify")
    verify.add_argument("id")
    verify.add_argument("--check", action="append", default=[], metavar="NAME=COMMAND")

    review = sub.add_parser("review", help="legacy review")
    review.add_argument("id")

    human = sub.add_parser("human-check", help="legacy human-check")
    human.add_argument("id")
    human.add_argument("result", choices=("ACCEPT", "FEEDBACK"))
    human.add_argument("--feedback")
    human.add_argument("--expected-revision")

    status = sub.add_parser("status", help="legacy status")
    status.add_argument("id")

    show = sub.add_parser("show", help="legacy show")
    show.add_argument("id")

    return parser


def _show_issue(issue_store: IssueStore) -> str:
    state = issue_store.load_state()
    issue = issue_store.load_issue()
    phases = [issue_store.load_phase(p_id) for p_id in issue.phase_order]
    lines = [
        f"Issue: {issue.id}",
        f"Status: {state.state}",
        f"Description: {issue.description}",
    ]
    if state.current_phase:
        current = next((p for p in phases if p.id == state.current_phase), None)
        suffix = f" ({current.status})" if current else ""
        lines.append(f"Current phase: {state.current_phase}{suffix}")
    if phases:
        lines.append("Phases:")
        lines.extend(f"  {p.id}: {p.status}" for p in phases)
    lines.append(format_token_usage(issue_store.load_token_usage()))
    lines.append(format_allocation_usage(issue_store.load_allocation_usage()))
    return "\n".join(lines)


def _show_project(p_store: ProjectStore) -> str:
    project = p_store.load_project() if p_store.exists() else Project(id=p_store.project_id)
    lines = [
        f"Project: {project.id}",
        f"Repository: {p_store.repository_path()}",
        f"Agent: {project.agent}",
    ]
    if project.checks:
        lines.append("Checks:")
        lines.extend(f"  {name}: {cmd}" for name, cmd in project.checks)
    issues = p_store.list_issue_ids()
    lines.append(f"Issues ({len(issues)}):")
    for i_id in issues:
        try:
            istore = p_store.issue_store(i_id)
            state = istore.load_state()
            iss = istore.load_issue()
            lines.append(f"  {i_id} [{state.state}]: {iss.description}")
        except Exception:
            lines.append(f"  {i_id}")
    return "\n".join(lines)


def _show(store: IssueStore | WorkStore) -> str:
    report = read_pipeline(store.root, getattr(store, "issue_id", getattr(store, "initiative_id", "")), include_evidence=False)
    state = report["state"]
    issue = report.get("issue", {})
    lines = [
        f"Issue: {issue.get('id', '')}",
        f"Status: {state['state']}",
        f"Description: {issue.get('description', '')}",
    ]
    phases = report["phases"]
    if state["current_phase"]:
        current = next((phase for phase in phases if phase["id"] == state["current_phase"]), None)
        suffix = f" ({current['status']})" if current else ""
        lines.append(f"Current phase: {state['current_phase']}{suffix}")
    if phases:
        lines.append("Phases:")
        lines.extend(f"  {phase['id']}: {phase['status']}" for phase in phases)
    lines.append(format_token_usage(report["usage"]))
    lines.append(format_allocation_usage(report["allocation_usage"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        work_dir = Path(args.work_dir)
        if args.command == "project":
            p_store = ProjectStore(work_dir, getattr(args, "project_id", ""))
            if args.subcommand in {"list", "show"}:
                with nullcontext():
                    return _execute_project(args, p_store)
            with p_store.execution_lock():
                return _execute_project(args, p_store)
        elif args.command == "issue":
            p_store = ProjectStore(work_dir, args.project_id)
            if args.subcommand in {"list", "show", "status"}:
                with nullcontext():
                    return _execute_issue(args, p_store)
            issue_id = getattr(args, "issue_id", None) or getattr(args, "id", None) or ""
            issue_store = p_store.issue_store(issue_id) if issue_id else None
            lock_ctx = issue_store.execution_lock() if issue_store else p_store.execution_lock()
            with lock_ctx:
                return _execute_issue(args, p_store)
        else:
            store = WorkStore(work_dir, getattr(args, "id", ""))
            with (nullcontext() if args.command in {"status", "show"} else store.execution_lock()):
                return _execute_legacy(args, store)
    except (ValueError, OSError, AgentError, TransitionError, FileNotFoundError, FileExistsError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _execute_project(args: argparse.Namespace, p_store: ProjectStore) -> int:
    if args.subcommand == "init":
        repo_path = args.repo
        if repo_path is not None:
            repo_path = str(Path(repo_path).resolve())
        else:
            default_repo = p_store.path / "repo"
            default_repo.mkdir(parents=True, exist_ok=True)
            if not (default_repo / ".git").exists():
                subprocess.run(["git", "init", str(default_repo)], check=False)
            repo_path = str(default_repo)
        project = Project(id=args.project_id, repo=repo_path, agent=args.agent,
                          agent_command=args.agent_command, checks=_checks(args.check))
        p_store.create(project)
        print(f"Project '{args.project_id}' initialized.")
        return 0
    elif args.subcommand == "list":
        root = Path(args.work_dir).resolve()
        if not root.is_dir():
            return 0
        for name in sorted(path.name for path in root.iterdir() if path.is_dir() and (path / "project.json").is_file()):
            print(name)
        return 0
    elif args.subcommand == "show":
        if not p_store.exists():
            raise FileNotFoundError(f"project not found: {args.project_id}")
        print(_show_project(p_store))
        return 0
    return 0


def _execute_issue(args: argparse.Namespace, p_store: ProjectStore) -> int:
    if not p_store.exists():
        repo_path = getattr(args, "repo", None)
        if repo_path is not None:
            repo_path = str(Path(repo_path).resolve())
        else:
            default_repo = p_store.path / "repo"
            default_repo.mkdir(parents=True, exist_ok=True)
            if not (default_repo / ".git").exists():
                subprocess.run(["git", "init", str(default_repo)], check=False)
            repo_path = str(default_repo)
        p_store.create(Project(id=args.project_id, repo=repo_path,
                               agent=getattr(args, "agent", None) or "codex"))

    project = p_store.load_project()
    repository = Path(args.repo).resolve() if getattr(args, "repo", None) else p_store.repository_path()
    repository.mkdir(parents=True, exist_ok=True)
    if not (repository / ".git").exists():
        subprocess.run(["git", "init", str(repository)], check=False)

    if args.subcommand == "create":
        issue_id = args.id or p_store.next_issue_id(args.request or "")
        issue_store = p_store.issue_store(issue_id)
        agent_name = args.agent or project.agent or "codex"
        agent_command = args.agent_command or project.agent_command
        agent = create_agent(agent_name, agent_command, log_dir=issue_store.path / "agent-logs")
        agent.token_usage_format = _resolved_token_usage_format(args, agent)
        resolved_context = _project_resolved_context(p_store)
        description, phases, planner_output = _plan(
            args, agent, repository, resolved_issues=resolved_context)
        Pipeline.initialize(issue_store, description, phases,
                            checks=_checks(args.check))
        usage = extract_token_usage(planner_output, agent.token_usage_format)
        allocation = collect_agent_allocation(agent)
        if usage is not None or allocation is not None:
            event = {
                "stage": "planner", "phase": None, "run": None,
                "recorded_at": now_iso(),
            }
            if usage is not None:
                event["usage"] = usage
            if allocation is not None:
                event["allocation_usage"] = allocation
            issue_store.record_usage_event(event)
        if args.plan_only:
            print(json.dumps(issue_store.load_state().as_dict(), ensure_ascii=False, indent=2))
            return 0
        pipeline = Pipeline(issue_store, repository, agent)
        print(pipeline.run_until_ready(_checks(args.check)))
        return 0

    elif args.subcommand == "list":
        issues = p_store.list_issue_ids()
        if not issues:
            print(f"No issues in project {args.project_id}")
            return 0
        for i_id in issues:
            try:
                istore = p_store.issue_store(i_id)
                st = istore.load_state()
                iss = istore.load_issue()
                print(f"{i_id}\t{st.state}\t{iss.description}")
            except Exception:
                print(i_id)
        return 0

    issue_store = p_store.issue_store(args.issue_id)
    if args.subcommand == "show":
        print(_show_issue(issue_store))
        return 0
    elif args.subcommand == "status":
        print(json.dumps(issue_store.load_state().as_dict(), ensure_ascii=False, indent=2))
        return 0

    agent_name = getattr(args, "agent", None) or project.agent or "codex"
    agent_command = getattr(args, "agent_command", None) or project.agent_command
    agent = create_agent(agent_name, agent_command, log_dir=issue_store.path / "agent-logs")
    agent.token_usage_format = _resolved_token_usage_format(args, agent)
    pipeline = Pipeline(issue_store, repository, agent)

    if args.subcommand == "run":
        checks = _checks(args.check)
        verify = not args.no_verify
        if args.until_ready:
            print(pipeline.run_until_ready(checks, verify=verify, max_review_retries=args.max_review_retries))
        else:
            print(pipeline.run(checks, verify=verify))
    elif args.subcommand == "verify":
        passed = pipeline.verify(_checks(args.check))
        print("PASS" if passed else "FAIL")
        return 0 if passed else 1
    elif args.subcommand == "review":
        passed = pipeline.review()
        print("PASS" if passed else "FINDINGS")
        return 0 if passed else 1
    elif args.subcommand == "human-check":
        print(pipeline.human_check(args.result, args.feedback, args.expected_revision))
    return 0


def _execute_legacy(args: argparse.Namespace, store: WorkStore) -> int:
    repository = _repository(store, getattr(args, "repo", None))
    if getattr(args, "repo", None) is None:
        repository.mkdir(parents=True, exist_ok=True)
    if args.command == "init":
        agent = _agent(args, custom_store=store)
        agent.token_usage_format = _resolved_token_usage_format(args, agent)
        description, phases, planner_output = _plan(args, agent, repository)
        Pipeline.initialize(store, description, phases,
                            checks=_checks(args.check))
        usage = extract_token_usage(planner_output, agent.token_usage_format)
        allocation = collect_agent_allocation(agent)
        if usage is not None or allocation is not None:
            event = {
                "stage": "planner", "phase": None, "run": None,
                "recorded_at": now_iso(),
            }
            if usage is not None:
                event["usage"] = usage
            if allocation is not None:
                event["allocation_usage"] = allocation
            store.record_usage_event(event)
        if args.plan_only:
            print(json.dumps(store.load_state().as_dict(), ensure_ascii=False, indent=2))
            return 0
        print(Pipeline(store, repository, agent).run_until_ready(_checks(args.check)))
        return 0
    if args.command == "status":
        print(json.dumps(store.load_state().as_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "show":
        print(_show(store))
        return 0
    agent = _agent(args, custom_store=store)
    agent.token_usage_format = _resolved_token_usage_format(args, agent)
    pipeline = Pipeline(store, repository, agent)
    if args.command == "run":
        checks = _checks(args.check)
        verify = not args.no_verify
        if args.until_ready:
            print(pipeline.run_until_ready(checks, verify=verify, max_review_retries=args.max_review_retries))
        else:
            print(pipeline.run(checks, verify=verify))
    elif args.command == "verify":
        passed = pipeline.verify(_checks(args.check))
        print("PASS" if passed else "FAIL")
        return 0 if passed else 1
    elif args.command == "review":
        passed = pipeline.review()
        print("PASS" if passed else "FINDINGS")
        return 0 if passed else 1
    elif args.command == "human-check":
        print(pipeline.human_check(args.result, args.feedback, args.expected_revision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
