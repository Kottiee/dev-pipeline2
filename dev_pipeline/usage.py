from __future__ import annotations

import json
from typing import Any, Iterable


TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
DEFAULT_TOKEN_USAGE_FORMAT = "codex"


def _valid_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def summarize_token_usage(usages: Iterable[Any]) -> dict[str, int]:
    summary = {field: 0 for field in TOKEN_USAGE_FIELDS}
    summary["reported_invocations"] = 0
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        for field in TOKEN_USAGE_FIELDS:
            value = usage.get(field, 0)
            if _valid_count(value):
                summary[field] += value
        reported = usage.get("reported_invocations", 1)
        if _valid_count(reported):
            summary["reported_invocations"] += reported
    summary["total_tokens"] = summary["input_tokens"] + summary["output_tokens"]
    return summary


def _opencode_tokens_to_usage(tokens: dict[str, Any]) -> dict[str, int] | None:
    cache = tokens.get("cache")
    cache_read = cache.get("read", 0) if isinstance(cache, dict) else 0
    cache_write = cache.get("write", 0) if isinstance(cache, dict) else 0
    raw = {
        "input_tokens": tokens.get("input", 0),
        "cached_input_tokens": cache_read,
        "output_tokens": tokens.get("output", 0),
        "reasoning_output_tokens": tokens.get("reasoning", 0),
        "cache_write_tokens": cache_write,
    }
    if not all(_valid_count(value) for value in raw.values()):
        return None
    usage = {field: raw[field] for field in TOKEN_USAGE_FIELDS}
    usage["input_tokens"] += usage["cached_input_tokens"] + raw["cache_write_tokens"]
    usage["output_tokens"] += usage["reasoning_output_tokens"]
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    usage["reported_invocations"] = 1
    return usage


def _extract_codex_usage(output: str | None) -> dict[str, int] | None:
    usages = []
    for line in (output or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        raw_usage = event.get("usage")
        if not isinstance(raw_usage, dict):
            continue
        usage = {}
        for field in TOKEN_USAGE_FIELDS:
            value = raw_usage.get(field, 0)
            if not _valid_count(value):
                break
            usage[field] = value
        else:
            usage["reported_invocations"] = 1
            usages.append(usage)
    return summarize_token_usage(usages) if usages else None


def _extract_antigravity_usage(output: str | None) -> dict[str, int] | None:
    usage = None
    for line in (output or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") != "result":
            continue
        payload = event.get("result")
        if not isinstance(payload, dict):
            continue
        candidate = payload.get("usage")
        if isinstance(candidate, dict):
            usage = candidate
    if not isinstance(usage, dict):
        return None
    normalized = {
        "input_tokens": usage.get("input_tokens", 0),
        "cached_input_tokens": usage.get("cache_read_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "reasoning_output_tokens": usage.get("thinking_tokens", 0),
    }
    if not all(_valid_count(value) for value in normalized.values()):
        return None
    normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    normalized["reported_invocations"] = 1
    return normalized


def _extract_opencode_usage(output: str | None) -> dict[str, int] | None:
    steps = []
    for line in (output or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            continue
        part = event.get("part")
        tokens = part.get("tokens") if isinstance(part, dict) else None
        if not isinstance(tokens, dict):
            continue
        usage = _opencode_tokens_to_usage(tokens)
        if usage is not None:
            steps.append(usage)
    if not steps:
        return None
    summary = {field: 0 for field in TOKEN_USAGE_FIELDS}
    for usage in steps:
        for field in TOKEN_USAGE_FIELDS:
            summary[field] += usage[field]
    summary["total_tokens"] = summary["input_tokens"] + summary["output_tokens"]
    summary["reported_invocations"] = 1
    return summary


TOKEN_USAGE_EXTRACTORS: dict[str, Any] = {
    "codex": _extract_codex_usage,
    "opencode": _extract_opencode_usage,
    "antigravity": _extract_antigravity_usage,
}


def resolve_token_usage_format(explicit: str | None, backend: str | None = None) -> str:
    if explicit is not None:
        if not isinstance(explicit, str) or explicit not in TOKEN_USAGE_EXTRACTORS:
            known = ", ".join(sorted(TOKEN_USAGE_EXTRACTORS))
            raise ValueError(
                f"unsupported token usage format {explicit!r} (available: {known})"
            )
        return explicit
    if backend in TOKEN_USAGE_EXTRACTORS:
        return backend
    return DEFAULT_TOKEN_USAGE_FORMAT


def extract_token_usage(
    output: str | None, format_name: str = DEFAULT_TOKEN_USAGE_FORMAT
) -> dict[str, int] | None:
    extractor = TOKEN_USAGE_EXTRACTORS.get(format_name)
    if extractor is None:
        known = ", ".join(sorted(TOKEN_USAGE_EXTRACTORS))
        raise ValueError(f"unsupported token usage format {format_name!r} (available: {known})")
    return extractor(output)


def format_token_usage(summary: dict[str, Any]) -> str:
    if not summary.get("reported_invocations", 0):
        return "Token usage: unavailable"
    values = {}
    for field in (*TOKEN_USAGE_FIELDS, "total_tokens"):
        value = summary.get(field, 0)
        values[field] = value if _valid_count(value) else 0
    return (
        "Token usage: {total_tokens:,} total "
        "(input {input_tokens:,}, output {output_tokens:,}, "
        "cached {cached_input_tokens:,}, reasoning {reasoning_output_tokens:,})"
    ).format(**values)
