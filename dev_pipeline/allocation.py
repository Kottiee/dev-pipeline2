from __future__ import annotations

import json
import queue
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


ALLOCATION_TIMEOUT_SECONDS = 15


def _valid_percent(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 100
    )


def _iso_from_epoch(value: Any) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _bucket(bucket_id: str, label: str, used_percent: Any,
            resets_at: str | None = None) -> dict[str, Any] | None:
    if not bucket_id or not label or not _valid_percent(used_percent):
        return None
    result: dict[str, Any] = {
        "id": bucket_id,
        "label": label,
        "used_percent": round(float(used_percent), 2),
    }
    if resets_at:
        result["resets_at"] = resets_at
    return result


def _response_reader(stream: Any, responses: queue.Queue[Any]) -> None:
    for line in stream:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            responses.put(value)


def _wait_for_response(responses: queue.Queue[Any], request_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + ALLOCATION_TIMEOUT_SECONDS
    while True:
        value = responses.get(timeout=max(0, deadline - time.monotonic()))
        if value.get("id") == request_id:
            return value


def _collect_codex(command: str) -> dict[str, Any] | None:
    executable = shlex.split(command)[0]
    process = subprocess.Popen(
        [executable, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    responses: queue.Queue[Any] = queue.Queue()
    assert process.stdout is not None
    threading.Thread(
        target=_response_reader, args=(process.stdout, responses), daemon=True
    ).start()
    try:
        assert process.stdin is not None
        initialize = {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "dev-pipeline", "version": "1"},
                "capabilities": {},
            },
        }
        process.stdin.write(json.dumps(initialize) + "\n")
        process.stdin.flush()
        initialized = _wait_for_response(responses, 1)
        if "error" in initialized:
            return None
        request = {"id": 2, "method": "account/rateLimits/read", "params": None}
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        response = _wait_for_response(responses, 2)
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        if process.stdin is not None:
            process.stdin.close()
        process.stdout.close()

    result = response.get("result")
    if not isinstance(result, dict):
        return None
    snapshots = result.get("rateLimitsByLimitId")
    if not isinstance(snapshots, dict) or not snapshots:
        fallback = result.get("rateLimits")
        snapshots = {"codex": fallback} if isinstance(fallback, dict) else {}
    buckets = []
    for limit_id, snapshot in snapshots.items():
        if not isinstance(limit_id, str) or not isinstance(snapshot, dict):
            continue
        limit_name = snapshot.get("limitName") or limit_id
        for window_name in ("primary", "secondary"):
            window = snapshot.get(window_name)
            if not isinstance(window, dict):
                continue
            duration = window.get("windowDurationMins")
            if duration == 300:
                window_label = "5h"
            elif duration == 10080:
                window_label = "weekly"
            elif isinstance(duration, int) and not isinstance(duration, bool):
                window_label = f"{duration}m"
            else:
                window_label = window_name
            item = _bucket(
                f"{limit_id}:{window_name}",
                f"{limit_name} {window_label}",
                window.get("usedPercent"),
                _iso_from_epoch(window.get("resetsAt")),
            )
            if item is not None:
                buckets.append(item)
    return {"provider": "codex", "buckets": buckets} if buckets else None


def _collect_antigravity(command: str) -> dict[str, Any] | None:
    completed = subprocess.run(
        shlex.split(command) + ["-p", "/usage", "--output-format", "json"],
        text=True,
        capture_output=True,
        check=False,
        timeout=ALLOCATION_TIMEOUT_SECONDS,
    )
    if completed.returncode:
        return None
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    command_result = response.get("command") if isinstance(response, dict) else None
    data = command_result.get("data") if isinstance(command_result, dict) else None
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return None
    buckets = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = group.get("name")
        raw_buckets = group.get("buckets")
        if not isinstance(raw_buckets, list):
            continue
        for raw in raw_buckets:
            if not isinstance(group_name, str) or not isinstance(raw, dict):
                continue
            remaining = raw.get("remaining_fraction")
            if (
                not isinstance(remaining, (int, float))
                or isinstance(remaining, bool)
                or not 0 <= remaining <= 1
            ):
                continue
            bucket_id = raw.get("id")
            name = raw.get("window") or bucket_id
            item = _bucket(
                bucket_id if isinstance(bucket_id, str) else "",
                f"{group_name} {name}" if isinstance(name, str) else group_name,
                (1 - remaining) * 100,
                raw.get("reset_time") if isinstance(raw.get("reset_time"), str) else None,
            )
            if item is not None:
                buckets.append(item)
    return {"provider": "antigravity", "buckets": buckets} if buckets else None


ALLOCATION_COLLECTORS: dict[str, Callable[[str], dict[str, Any] | None]] = {
    "codex": _collect_codex,
    "antigravity": _collect_antigravity,
}


def collect_agent_allocation(agent: Any) -> dict[str, Any] | None:
    format_name = getattr(agent, "allocation_usage_format", None)
    command = getattr(agent, "allocation_command", None)
    collector = ALLOCATION_COLLECTORS.get(format_name)
    if collector is None or not isinstance(command, str) or not command.strip():
        return None
    try:
        return collector(command)
    except (OSError, OverflowError, ValueError, subprocess.SubprocessError, queue.Empty):
        return None


def summarize_allocation_usage(events: Iterable[Any]) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        allocation = event.get("allocation_usage")
        if not isinstance(allocation, dict):
            continue
        provider = allocation.get("provider")
        buckets = allocation.get("buckets")
        if not isinstance(provider, str) or not isinstance(buckets, list):
            continue
        valid_buckets = []
        for raw in buckets:
            if not isinstance(raw, dict):
                continue
            item = _bucket(
                raw.get("id", ""), raw.get("label", ""), raw.get("used_percent"),
                raw.get("resets_at") if isinstance(raw.get("resets_at"), str) else None,
            )
            if item is not None:
                valid_buckets.append(item)
        if valid_buckets:
            latest[provider] = {"provider": provider, "buckets": valid_buckets}
    return latest


def format_allocation_usage(summary: dict[str, Any]) -> str:
    entries = []
    for allocation in summary.values():
        if not isinstance(allocation, dict):
            continue
        for bucket in allocation.get("buckets", []):
            if not isinstance(bucket, dict) or not _valid_percent(bucket.get("used_percent")):
                continue
            percent = bucket["used_percent"]
            rendered = f"{percent:g}" if isinstance(percent, float) else str(percent)
            entries.append(f"{bucket.get('label', bucket.get('id', 'unknown'))}: {rendered}% used")
    return "Allocation usage: " + ("; ".join(entries) if entries else "unavailable")
