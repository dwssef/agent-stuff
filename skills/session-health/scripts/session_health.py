#!/usr/bin/env python3
"""Read-only health report for Codex rollout JSONL files.

The file format is intentionally treated as an event stream. Unknown records are ignored but
counted in the data-quality section so a newer Codex format cannot silently look complete.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple


TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]{2,}|[\u3400-\u9fff]{2,}")
SECRET_RE = re.compile(r"(?i)(?:token|secret|password|api[_-]?key)\s*[=:]\s*[^\s,;]+")
FAIL_RE = re.compile(
    r"(?i)(?:script failed|command failed|exit code\s*[1-9]|non[- ]zero|traceback|timed? out|timeout|cancel(?:led|ed)|terminated|permission denied|error:)"
)
SUCCESS_RE = re.compile(r"(?i)\b(?:script completed|process exited with code 0|exit code\s*0)\b")
COMMAND_RE = re.compile(r"(?:['\"]cmd['\"]|\bcmd)\s*:\s*['\"]((?:\\.|[^'\"])*)['\"]")

GENERIC_INTENT = {
    "the", "and", "with", "from", "this", "that", "please", "help", "check", "look",
    "use", "current", "session", "codex", "skill", "执行", "请", "帮我", "查看", "当前",
}


def parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        result = datetime.fromisoformat(text)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def pct(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 100, 1)


def safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def scrub_for_hash(value: str) -> str:
    value = SECRET_RE.sub("<secret>", value)
    return re.sub(r"\s+", " ", value).strip()


def digest(value: str) -> str:
    return hashlib.sha256(scrub_for_hash(value).encode("utf-8", "replace")).hexdigest()[:16]


def extract_text(value: Any) -> str:
    """Extract text for internal heuristics without returning it in a report."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(extract_text(item) for item in value)
    if isinstance(value, dict):
        parts: List[str] = []
        for key in ("text", "message", "input_text", "content", "summary", "title"):
            if key in value:
                parts.append(extract_text(value[key]))
        if not parts:
            parts.extend(extract_text(item) for item in value.values())
        return " ".join(parts)
    return ""


def intent_tokens(text: str) -> set[str]:
    text = re.sub(r"<[^>]{0,200}>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    tokens = {token.lower() for token in TOKEN_RE.findall(text)}
    return {token for token in tokens if token not in GENERIC_INTENT and len(token) > 1}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / float(len(left | right))


def output_length(value: Any) -> int:
    return len(extract_text(value))


def output_failed(value: Any) -> bool:
    text = extract_text(value)
    if SUCCESS_RE.search(text) and not FAIL_RE.search(text):
        return False
    return bool(FAIL_RE.search(text))


def command_from_call(name: str, raw: Any) -> str:
    if isinstance(raw, dict):
        for key in ("cmd", "command", "input", "arguments"):
            if key in raw:
                candidate = raw[key]
                if isinstance(candidate, str):
                    raw = candidate
                    break
                if isinstance(candidate, dict):
                    return command_from_call(name, candidate)
    text = safe_text(raw)
    match = COMMAND_RE.search(text)
    if match:
        text = bytes(match.group(1), "utf-8").decode("unicode_escape", "replace")
    return text


def classify_tool(name: str, raw: Any) -> Tuple[str, str]:
    command = command_from_call(name, raw)
    lower = command.lower()
    if name in {"request_user_input", "request_plugin_install"}:
        return "interaction", name
    if any(word in lower for word in ("pytest", "npm test", "pnpm test", "yarn test", "cargo test", "quick_validate", " test ")):
        category = "test"
    elif re.search(r"\b(rg|grep|find|ripgrep|git\s+(?:log|grep))\b", lower):
        category = "search"
    elif re.search(r"\b(sed|head|tail|less|more|cat|jq|git\s+(?:show|diff|status))\b", lower):
        category = "read"
    elif "apply_patch" in lower or re.search(r"\b(cp|mv|mkdir|tee)\b", lower):
        category = "write"
    elif name in {"exec", "exec_command"}:
        category = "shell"
    else:
        category = "other"
    normalized = re.sub(r"\s+", " ", scrub_for_hash(command)).strip()
    return category, normalized[:1000]


@dataclass
class TokenSample:
    timestamp: datetime
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    context_window: int


@dataclass
class CompactEvent:
    timestamp: datetime
    key: str
    window_id: Optional[str]
    previous_window_id: Optional[str]
    window_number: Optional[int]


@dataclass
class UserIntent:
    timestamp: datetime
    tokens: set[str]


@dataclass
class ToolCall:
    timestamp: datetime
    call_id: Optional[str]
    name: str
    category: str
    signature: str
    output_chars: int = 0
    failed: bool = False
    output_seen: bool = False


@dataclass
class ParseData:
    path: Path
    events: List[Tuple[datetime, str]] = field(default_factory=list)
    token_samples: List[TokenSample] = field(default_factory=list)
    compactions: List[CompactEvent] = field(default_factory=list)
    calls: List[ToolCall] = field(default_factory=list)
    intents: List[UserIntent] = field(default_factory=list)
    session_ids: List[str] = field(default_factory=list)
    session_meta_events: List[Tuple[datetime, str]] = field(default_factory=list)
    cwd_values: set[str] = field(default_factory=set)
    models: set[str] = field(default_factory=set)
    context_windows: set[int] = field(default_factory=set)
    raw_lines: int = 0
    malformed_lines: int = 0
    missing_timestamps: int = 0
    unknown_records: int = 0
    task_started: int = 0
    task_completed: int = 0
    interruptions: int = 0
    plan_updates: int = 0
    output_unmatched: int = 0
    output_total_chars: int = 0
    cumulative_usage: Dict[str, int] = field(default_factory=dict)


def parse_file(path: Path) -> ParseData:
    data = ParseData(path=path)
    calls_by_id: Dict[str, ToolCall] = {}
    compact_keys: set[str] = set()
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"无法读取 session 文件：{path} ({exc})") from exc
    with handle:
        for line in handle:
            data.raw_lines += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                data.malformed_lines += 1
                continue
            if not isinstance(record, dict):
                data.unknown_records += 1
                continue
            timestamp = parse_time(record.get("timestamp"))
            if timestamp is None:
                data.missing_timestamps += 1
            record_type = record.get("type")
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            payload_type = payload.get("type")
            if timestamp is not None:
                event_kind = payload_type if record_type == "event_msg" else record_type
                data.events.append((timestamp, str(event_kind or "unknown")))

            if record_type == "session_meta":
                session_id = payload.get("id") or payload.get("session_id")
                if session_id:
                    data.session_ids.append(str(session_id))
                    if timestamp is not None:
                        data.session_meta_events.append((timestamp, str(session_id)))
                if payload.get("cwd"):
                    data.cwd_values.add(str(payload["cwd"]))
                if payload.get("model_provider"):
                    data.models.add(str(payload["model_provider"]))
                continue
            if record_type == "turn_context":
                if payload.get("cwd"):
                    data.cwd_values.add(str(payload["cwd"]))
                if payload.get("model"):
                    data.models.add(str(payload["model"]))
                continue
            if record_type == "compacted" and timestamp is not None:
                window_id = payload.get("window_id")
                previous = payload.get("previous_window_id")
                window_number = payload.get("window_number")
                if window_id:
                    key = f"window:{window_id}"
                else:
                    key = f"fallback:{timestamp.isoformat()}:{window_number}:{previous}"
                if key not in compact_keys:
                    compact_keys.add(key)
                    data.compactions.append(
                        CompactEvent(timestamp, key, str(window_id) if window_id else None,
                                     str(previous) if previous else None,
                                     int(window_number) if isinstance(window_number, int) else None)
                    )
                continue
            if record_type == "event_msg":
                if payload_type == "token_count":
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
                    total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
                    usage = last or total
                    if timestamp is not None and usage:
                        sample = TokenSample(
                            timestamp,
                            int(number(usage.get("input_tokens"))),
                            int(number(usage.get("cached_input_tokens"))),
                            int(number(usage.get("output_tokens"))),
                            int(number(usage.get("reasoning_output_tokens"))),
                            int(number(usage.get("total_tokens"))),
                            int(number(info.get("model_context_window"))),
                        )
                        data.token_samples.append(sample)
                        if sample.context_window:
                            data.context_windows.add(sample.context_window)
                    if total:
                        data.cumulative_usage = {
                            key: int(number(total.get(key)))
                            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
                            if total.get(key) is not None
                        }
                elif payload_type == "user_message" and timestamp is not None:
                    data.intents.append(UserIntent(timestamp, intent_tokens(extract_text(payload))))
                elif payload_type == "task_started":
                    data.task_started += 1
                elif payload_type == "task_complete":
                    data.task_completed += 1
                elif payload_type in {"turn_aborted", "thread_rolled_back"}:
                    data.interruptions += 1
                elif payload_type in {
                    "agent_reasoning", "agent_message", "context_compacted", "item_completed",
                    "patch_apply_end", "thread_settings_applied", "web_search_end",
                }:
                    pass
                else:
                    data.unknown_records += 1
                continue
            if record_type == "response_item":
                item_type = payload.get("type")
                if item_type in {"function_call", "custom_tool_call"}:
                    name = str(payload.get("name") or "unknown")
                    raw = payload.get("arguments") if "arguments" in payload else payload.get("input", "")
                    if isinstance(raw, str) and name != "exec":
                        try:
                            raw_for_classification: Any = json.loads(raw)
                        except json.JSONDecodeError:
                            raw_for_classification = raw
                    else:
                        raw_for_classification = raw
                    category, signature = classify_tool(name, raw_for_classification)
                    call = ToolCall(timestamp or datetime.min.replace(tzinfo=timezone.utc),
                                    str(payload.get("call_id")) if payload.get("call_id") else None,
                                    name, category, signature)
                    data.calls.append(call)
                    if call.call_id:
                        calls_by_id[call.call_id] = call
                    if name == "update_plan":
                        data.plan_updates += 1
                elif item_type in {"function_call_output", "custom_tool_call_output"}:
                    call_id = payload.get("call_id")
                    output = payload.get("output")
                    length = output_length(output)
                    data.output_total_chars += length
                    call = calls_by_id.get(str(call_id)) if call_id else None
                    if call is None:
                        data.output_unmatched += 1
                    else:
                        call.output_chars += length
                        call.output_seen = True
                        call.failed = call.failed or output_failed(output)
                elif item_type in {"message", "reasoning"}:
                    pass
                else:
                    data.unknown_records += 1
                continue
            if record_type == "world_state":
                continue
            if record_type not in {"session_meta", "turn_context", "response_item", "event_msg", "compacted"}:
                data.unknown_records += 1

    data.compactions.sort(key=lambda item: item.timestamp)
    data.token_samples.sort(key=lambda item: item.timestamp)
    data.calls.sort(key=lambda item: item.timestamp)
    data.intents.sort(key=lambda item: item.timestamp)
    return data


def select_session(session_arg: Optional[str], sessions_dir: Path) -> Path:
    files = list(sessions_dir.rglob("rollout-*.jsonl")) if sessions_dir.exists() else []
    if session_arg:
        candidate = Path(session_arg).expanduser()
        if candidate.is_file():
            return candidate
        matches = [path for path in files if session_arg in path.name]
        if not matches:
            raise RuntimeError(f"找不到指定 session：{session_arg}")
        return max(matches, key=lambda path: path.stat().st_mtime)
    thread_id = os.environ.get("CODEX_THREAD_ID")
    if thread_id:
        matches = [path for path in files if thread_id in path.name]
        if matches:
            return max(matches, key=lambda path: path.stat().st_mtime)
    if not files:
        raise RuntimeError(f"找不到 rollout 文件：{sessions_dir}")
    return max(files, key=lambda path: path.stat().st_mtime)


def active_minutes(timestamps: Sequence[datetime], cap_seconds: int = 300) -> float:
    if len(timestamps) < 2:
        return 0.0
    ordered = sorted(set(timestamps))
    total = 0.0
    for left, right in zip(ordered, ordered[1:]):
        total += min(max((right - left).total_seconds(), 0.0), cap_seconds)
    return total / 60.0


def growth_rates(samples: Sequence[TokenSample], since: Optional[datetime] = None) -> List[Tuple[datetime, float, int]]:
    result: List[Tuple[datetime, float, int]] = []
    previous: Optional[TokenSample] = None
    for sample in samples:
        if since and sample.timestamp < since:
            continue
        if previous is not None:
            seconds = (sample.timestamp - previous.timestamp).total_seconds()
            delta = sample.total_tokens - previous.total_tokens
            if 0 < seconds <= 900 and delta > 0:
                result.append((sample.timestamp, delta / (seconds / 60.0), delta))
        previous = sample
    return result


def refill_metrics(data: ParseData, window: int, compactions: Optional[Sequence[CompactEvent]] = None,
                   samples: Optional[Sequence[TokenSample]] = None) -> Dict[str, Any]:
    refills: List[Dict[str, Any]] = []
    compactions = list(compactions if compactions is not None else data.compactions)
    samples = list(samples if samples is not None else data.token_samples)
    if not window:
        return {"count_to_80_within_30m": 0, "count_to_80_within_15m": 0, "events": refills}
    for index, compact in enumerate(compactions):
        next_compact = compactions[index + 1].timestamp if index + 1 < len(compactions) else None
        after = [
            sample for sample in data.token_samples
            if sample.timestamp > compact.timestamp and (next_compact is None or sample.timestamp < next_compact)
        ]
        if not any(sample.total_tokens > 0 for sample in after):
            continue
        reached = next((sample for sample in after if sample.total_tokens >= window * 0.8), None)
        if not reached:
            continue
        minutes = (reached.timestamp - compact.timestamp).total_seconds() / 60.0
        if minutes <= 30:
            refills.append({"minutes": round(minutes, 2), "context_tokens": reached.total_tokens})
    return {
        "count_to_80_within_30m": len(refills),
        "count_to_80_within_15m": sum(1 for item in refills if item["minutes"] <= 15),
        "events": refills[-10:],
    }


def drift_metrics(data: ParseData, intents: Optional[Sequence[UserIntent]] = None,
                  calls: Optional[Sequence[ToolCall]] = None, plan_updates: Optional[int] = None) -> Dict[str, Any]:
    intents = list(intents if intents is not None else data.intents)
    calls = list(calls if calls is not None else data.calls)
    plan_updates = data.plan_updates if plan_updates is None else plan_updates
    if len(intents) < 2:
        return {"pairs": 0, "low_similarity_pairs": 0, "corroborated_pairs": 0, "suspected": False, "severity": 0}
    low = 0
    corroborated = 0
    similarities: List[float] = []
    for previous, current in zip(intents, intents[1:]):
        similarity = jaccard(previous.tokens, current.tokens)
        similarities.append(similarity)
        if similarity < 0.12:
            low += 1
            before = [call.category for call in calls if previous.timestamp <= call.timestamp <= current.timestamp]
            after = [call.category for call in calls if call.timestamp > current.timestamp]
            before_focus = Counter(before).most_common(1)[0][0] if before else None
            after_focus = Counter(after).most_common(1)[0][0] if after else None
            if before_focus and after_focus and before_focus != after_focus:
                corroborated += 1
    severity = min(3, corroborated + (1 if low >= 2 and plan_updates >= 2 else 0))
    return {
        "pairs": len(similarities),
        "low_similarity_pairs": low,
        "corroborated_pairs": corroborated,
        "mean_similarity": round(mean(similarities), 3) if similarities else None,
        "suspected": severity > 0,
        "severity": severity,
    }


def lifecycle_metrics(data: ParseData, segment_start: Optional[datetime] = None) -> Dict[str, Any]:
    timestamps = sorted(timestamp for timestamp, _ in data.events if segment_start is None or timestamp >= segment_start)
    idle_gaps = []
    for left, right in zip(timestamps, timestamps[1:]):
        minutes = (right - left).total_seconds() / 60.0
        if minutes >= 60:
            idle_gaps.append(round(minutes, 1))
    started = sum(1 for timestamp, kind in data.events if (segment_start is None or timestamp >= segment_start) and kind == "task_started")
    completed = sum(1 for timestamp, kind in data.events if (segment_start is None or timestamp >= segment_start) and kind == "task_complete")
    interruptions = sum(1 for timestamp, kind in data.events if (segment_start is None or timestamp >= segment_start) and kind in {"turn_aborted", "thread_rolled_back"})
    return {
        "session_meta_records": len(data.session_ids),
        "cwd_count": len(data.cwd_values),
        "model_count": len(data.models),
        "context_window_count": len(data.context_windows),
        "long_idle_gaps": len(idle_gaps),
        "longest_idle_minutes": max(idle_gaps) if idle_gaps else 0,
        "task_started": started,
        "task_completed": completed,
        "interruptions": interruptions,
        "plan_updates": sum(1 for call in data.calls if (segment_start is None or call.timestamp >= segment_start) and call.name == "update_plan"),
        "in_progress_hint": started > completed,
    }


def current_segment_start(data: ParseData) -> Optional[datetime]:
    """Return the first record for the latest session identity in a rollout.

    A resumed rollout can contain a complete older session before its new session_meta
    records. The latest session identity is the best local lineage marker available in the
    JSONL format; when it is absent, retain the whole file as one segment.
    """
    if not data.session_meta_events:
        return None
    latest_id = data.session_meta_events[-1][1]
    # The first line of a resumed rollout can contain a provenance/session id before the
    # inherited source session metadata. Use the start of the final contiguous run for the
    # latest id, which is where the active session resumes.
    for index in range(len(data.session_meta_events) - 1, -1, -1):
        if data.session_meta_events[index][1] != latest_id:
            return data.session_meta_events[index + 1][0]
    return data.session_meta_events[0][0]


def build_report(data: ParseData) -> Dict[str, Any]:
    segment_start = current_segment_start(data)
    segment_id = data.session_meta_events[-1][1] if data.session_meta_events else None
    samples = data.token_samples
    segment_samples = [sample for sample in samples if segment_start is None or sample.timestamp >= segment_start]
    compactions = [compact for compact in data.compactions if segment_start is None or compact.timestamp >= segment_start]
    segment_calls = [call for call in data.calls if segment_start is None or call.timestamp >= segment_start]
    segment_intents = [intent for intent in data.intents if segment_start is None or intent.timestamp >= segment_start]
    segment_events = [timestamp for timestamp, _ in data.events if segment_start is None or timestamp >= segment_start]
    inherited_compactions = len(data.compactions) - len(compactions)
    latest = samples[-1] if samples else None
    window = latest.context_window if latest and latest.context_window else (max(data.context_windows) if data.context_windows else 0)
    usage = (latest.total_tokens / window) if latest and window and latest.total_tokens >= 0 else None
    now = latest.timestamp if latest else (data.events[-1][0] if data.events else None)
    recent_start = now.replace() if now else None
    if recent_start:
        from datetime import timedelta
        recent_start = recent_start - timedelta(minutes=30)
    all_rates = growth_rates(samples)
    recent_rates = growth_rates(segment_samples, recent_start)
    refill = refill_metrics(data, window, compactions, segment_samples)
    drift = drift_metrics(data, segment_intents, segment_calls, sum(1 for call in segment_calls if call.name == "update_plan"))
    lifecycle = lifecycle_metrics(data, segment_start)
    call_count = len(segment_calls)
    duplicate_counter = Counter((call.category, digest(call.signature)) for call in segment_calls)
    duplicate_calls = sum(count - 1 for count in duplicate_counter.values() if count > 1)
    repeat_ratio = duplicate_calls / call_count if call_count else 0.0
    category_counts = Counter(call.category for call in segment_calls)
    failed_calls = sum(1 for call in segment_calls if call.failed)
    failure_ratio = failed_calls / call_count if call_count else 0.0
    output_groups: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(lambda: {"calls": 0, "chars": 0})
    for call in segment_calls:
        group = output_groups[(call.name, call.category)]
        group["calls"] += 1
        group["chars"] += call.output_chars
    top_outputs = [
        {"tool": name, "category": category, "calls": values["calls"], "chars": values["chars"]}
        for (name, category), values in sorted(output_groups.items(), key=lambda item: item[1]["chars"], reverse=True)[:5]
        if values["chars"]
    ]
    segment_output_chars = sum(call.output_chars for call in segment_calls)
    active = active_minutes(segment_events)
    intervals = [
        (right.timestamp - left.timestamp).total_seconds() / 60.0
        for left, right in zip(compactions, compactions[1:])
        if right.timestamp >= left.timestamp
    ]
    short_intervals = [minutes for minutes in intervals if minutes <= 30]
    density = len(compactions) / (active / 60.0) if active >= 1 else None
    cache_ratio = latest.cached_input_tokens / latest.input_tokens if latest and latest.input_tokens else None
    cumulative = data.cumulative_usage or ({
        "input_tokens": samples[-1].input_tokens,
        "cached_input_tokens": samples[-1].cached_input_tokens,
        "output_tokens": samples[-1].output_tokens,
        "reasoning_output_tokens": samples[-1].reasoning_output_tokens,
        "total_tokens": samples[-1].total_tokens,
    } if samples else {})
    latest_rate = recent_rates[-1][1] if recent_rates else (all_rates[-1][1] if all_rates else None)
    avg_rate = mean(rate for _, rate, _ in recent_rates) if recent_rates else None
    lifetime_rate = mean(rate for _, rate, _ in all_rates) if all_rates else None
    time_to_80 = time_to_90 = None
    if latest_rate and latest and window and latest_rate > 0:
        time_to_80 = max(0.0, (window * 0.8 - latest.total_tokens) / latest_rate)
        time_to_90 = max(0.0, (window * 0.9 - latest.total_tokens) / latest_rate)

    raw_compact_count = sum(1 for timestamp, kind in data.events if kind == "compacted")
    segment_raw_compact_count = sum(1 for timestamp, kind in data.events if kind == "compacted" and (segment_start is None or timestamp >= segment_start))
    duplicate_compacts = max(0, raw_compact_count - len(compactions))
    quality_issues = data.malformed_lines + data.missing_timestamps + data.output_unmatched + data.unknown_records
    context_risk = 0
    if usage is not None:
        context_risk = 25 if usage >= .95 else 21 if usage >= .90 else 15 if usage >= .80 else 8 if usage >= .70 else 3 if usage >= .55 else 0
    if time_to_90 is not None and time_to_90 <= 15:
        context_risk = min(25, context_risk + 4)
    compact_count_risk = 0 if len(compactions) <= 2 else 3 if len(compactions) <= 5 else 6 if len(compactions) <= 7 else 10
    density_risk = 0 if density is None else 10 if density > 6 else 6 if density > 3 else 3 if density > 1.5 else 0
    interval_risk = 10 if any(minutes <= 5 for minutes in short_intervals) else 7 if any(minutes <= 15 for minutes in short_intervals) else 4 if any(minutes <= 30 for minutes in short_intervals) else 0
    compact_risk = min(30, compact_count_risk + density_risk + interval_risk)
    growth_risk = min(15, refill["count_to_80_within_15m"] * 5 + (3 if latest_rate and window and latest_rate > window / 60 else 0))
    if cache_ratio is not None and cache_ratio < .15 and latest and latest.input_tokens > window * .7:
        growth_risk = min(15, growth_risk + 3)
    churn_risk = min(20, round(repeat_ratio * 12) + (8 if failure_ratio >= .25 else 5 if failure_ratio >= .1 else 0) + (4 if segment_output_chars > 500000 else 2 if segment_output_chars > 100000 else 0))
    drift_risk = min(10, drift["severity"] * 3 + (2 if lifecycle["cwd_count"] > 1 or lifecycle["model_count"] > 1 else 0) + (2 if lifecycle["long_idle_gaps"] else 0) + (2 if lifecycle["interruptions"] >= 3 else 0))
    score = max(0, 100 - min(100, context_risk + compact_risk + growth_risk + churn_risk + drift_risk))
    hard_override = None
    for left in compactions:
        close_count = sum(1 for item in compactions if 0 <= (item.timestamp - left.timestamp).total_seconds() <= 15 * 60)
        if close_count >= 3:
            hard_override = "15 分钟内出现 3 次新的 compact"
            break
    if hard_override is None and refill["count_to_80_within_30m"] >= 2:
        hard_override = "至少两个 compact 周期在 30 分钟内回涨到 80% context"
    if hard_override is None and usage is not None and usage >= .9 and (repeat_ratio >= .3 or failure_ratio >= .25 or drift["severity"] >= 2):
        hard_override = "高 context 与重复工作、工具失败或任务漂移同时出现"
    if hard_override:
        status = "SWITCH"
    elif score >= 75:
        status = "GOOD"
    elif score >= 45:
        status = "WATCH"
    else:
        status = "SWITCH"
    recommendation = {"GOOD": "继续当前 session", "WATCH": "完成当前 milestone 后切换", "SWITCH": "立即新开 session"}[status]
    risks: List[str] = []
    if context_risk >= 15:
        risks.append("context 使用率偏高")
    if compact_risk >= 10:
        risks.append("compact 密度或短间隔偏高")
    if refill["count_to_80_within_30m"]:
        risks.append("compact 后 context 回涨较快")
    if repeat_ratio >= .2:
        risks.append("存在重复 tool call，尤其是读取/搜索/测试")
    if failure_ratio >= .1:
        risks.append("工具失败、超时或取消偏多")
    if drift["suspected"]:
        risks.append("检测到可能的任务状态漂移")
    if quality_issues:
        risks.append("部分事件缺失或无法完整关联，评分置信度下降")
    if not risks:
        risks.append("未发现明显异常")
    before_after = []
    for index, compact in enumerate(compactions):
        next_compact = compactions[index + 1].timestamp if index + 1 < len(compactions) else None
        after_samples = [
            sample for sample in samples
            if sample.timestamp > compact.timestamp and (next_compact is None or sample.timestamp < next_compact)
        ]
        before_after.append({
            "before_tokens": next((sample.total_tokens for sample in reversed(samples) if sample.timestamp <= compact.timestamp), None),
            "after_tokens": after_samples[0].total_tokens if after_samples else None,
            "first_nonzero_after_tokens": next((sample.total_tokens for sample in after_samples if sample.total_tokens > 0), None),
            "window_number": compact.window_number,
        })
    return {
        "session": {
            "file": data.path.name,
            "path": str(data.path),
            "ids": data.session_ids[-3:],
            "current_id": segment_id,
            "current_segment_start": segment_start.isoformat() if segment_start else None,
            "cwd": sorted(data.cwd_values)[-1] if data.cwd_values else None,
        },
        "health": {"score": score, "status": status, "recommendation": recommendation, "hard_override": hard_override},
        "context": {"current_tokens": latest.total_tokens if latest else None, "window": window or None, "usage_percent": pct(usage), "headroom_tokens": max(0, window - latest.total_tokens) if latest and window else None, "minutes_to_80": round(time_to_80, 2) if time_to_80 is not None else None, "minutes_to_90": round(time_to_90, 2) if time_to_90 is not None else None},
        "compaction": {
            "raw_count": raw_compact_count, "unique_count": len(data.compactions), "current_raw_count": segment_raw_compact_count,
            "current_unique_count": len(compactions), "inherited_unique_count": inherited_compactions,
            "duplicate_or_replayed_ignored": duplicate_compacts,
            "latest_interval_minutes": round(intervals[-1], 2) if intervals else None, "average_interval_minutes": round(mean(intervals), 2) if intervals else None,
            "density_per_active_hour": round(density, 2) if density is not None else None, "short_intervals_le_30m": len(short_intervals),
            "before_after": before_after[-10:],
            "refill": refill,
        },
        "tokens": {
            "latest": {"input_tokens": latest.input_tokens, "cached_input_tokens": latest.cached_input_tokens, "output_tokens": latest.output_tokens, "reasoning_output_tokens": latest.reasoning_output_tokens, "total_tokens": latest.total_tokens} if latest else None,
            "cumulative": cumulative, "cache_hit_percent": pct(cache_ratio),
            "recent_growth_tokens_per_minute": round(avg_rate, 2) if avg_rate is not None else None,
            "latest_growth_tokens_per_minute": round(latest_rate, 2) if latest_rate is not None else None,
            "lifetime_growth_tokens_per_minute": round(lifetime_rate, 2) if lifetime_rate is not None else None,
        },
        "tools": {
            "calls": call_count, "lifetime_calls": len(data.calls), "categories": dict(category_counts),
            "duplicate_calls": duplicate_calls, "duplicate_ratio_percent": round(repeat_ratio * 100, 1),
            "failed_calls": failed_calls, "failure_ratio_percent": round(failure_ratio * 100, 1),
            "output_chars": segment_output_chars, "lifetime_output_chars": data.output_total_chars,
            "unmatched_outputs": data.output_unmatched, "top_outputs": top_outputs,
        },
        "task": {"drift": drift, "lifecycle": lifecycle},
        "quality": {"raw_lines": data.raw_lines, "malformed_lines": data.malformed_lines, "missing_timestamps": data.missing_timestamps, "unknown_records": data.unknown_records, "unmatched_outputs": data.output_unmatched, "quality_issue_count": quality_issues},
        "risks": risks,
    }


def fmt_int(value: Any) -> str:
    return "未知" if value is None else f"{int(value):,}"


def render_markdown(report: Dict[str, Any]) -> str:
    health = report["health"]
    context = report["context"]
    compact = report["compaction"]
    tokens = report["tokens"]
    tools = report["tools"]
    lifecycle = report["task"]["lifecycle"]
    quality = report["quality"]
    usage = "未知" if context["usage_percent"] is None else f"{context['usage_percent']:.1f}%"
    latest_tokens = tokens.get("latest") or {}
    token_summary = (
        f"input {fmt_int(latest_tokens.get('input_tokens'))} / cached {fmt_int(latest_tokens.get('cached_input_tokens'))} / "
        f"output {fmt_int(latest_tokens.get('output_tokens'))} / reasoning {fmt_int(latest_tokens.get('reasoning_output_tokens'))}"
    )
    lines = [
        "Session Health", f"- Health Score: {health['score']}/100", f"- 状态: {health['status']}", f"- 建议: {health['recommendation']}", f"- Session: {report['session']['file']}", "",
        "关键指标",
        f"- Context: {usage}，剩余 {fmt_int(context['headroom_tokens'])} tokens；到 80%/90% 预计 {context['minutes_to_80'] if context['minutes_to_80'] is not None else '未知'}/{context['minutes_to_90'] if context['minutes_to_90'] is not None else '未知'} 分钟",
        f"- Compaction: 当前段 {compact['current_unique_count']} 次 / 全程 {compact['unique_count']} 次（继承历史 {compact['inherited_unique_count']}，原始重复 {compact['duplicate_or_replayed_ignored']}）；最近/平均间隔 {compact['latest_interval_minutes'] if compact['latest_interval_minutes'] is not None else '未知'}/{compact['average_interval_minutes'] if compact['average_interval_minutes'] is not None else '未知'} 分钟",
        f"- Token: {token_summary}；最近增长 {tokens['recent_growth_tokens_per_minute'] if tokens['recent_growth_tokens_per_minute'] is not None else '未知'} /分钟；cache 命中 {tokens['cache_hit_percent'] if tokens['cache_hit_percent'] is not None else '未知'}%",
        f"- Tools: {tools['calls']} 次，重复 {tools['duplicate_ratio_percent']}%，失败/超时/取消 {tools['failure_ratio_percent']}%，输出 {fmt_int(tools['output_chars'])} 字符",
        f"- 生命周期: task started/completed {lifecycle['task_started']}/{lifecycle['task_completed']}，中断/回滚 {lifecycle['interruptions']}，工作区 {lifecycle['cwd_count']} 个，模型 {lifecycle['model_count']} 个，长空闲间隔 {lifecycle['long_idle_gaps']} 次", "",
        "主要风险",
    ]
    lines.extend(f"- {risk}" for risk in report["risks"])
    if health.get("hard_override"):
        lines.append(f"- 强制升级原因: {health['hard_override']}")
    lines.extend(["", f"数据可信度: {quality['quality_issue_count']} 个质量问题（损坏行 {quality['malformed_lines']}，缺时间戳 {quality['missing_timestamps']}，未知事件 {quality['unknown_records']}，未关联输出 {quality['unmatched_outputs']}）"])
    if tools["top_outputs"]:
        top = "；".join(f"{item['tool']}/{item['category']} {fmt_int(item['chars'])} 字符" for item in tools["top_outputs"][:3])
        lines.append(f"主要 tool output（仅元数据）: {top}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Codex session health report")
    parser.add_argument("--session", help="session ID or rollout JSONL path")
    parser.add_argument("--sessions-dir", help="sessions directory override")
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    args = parser.parse_args(argv)
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    sessions_dir = Path(args.sessions_dir).expanduser() if args.sessions_dir else home / "sessions"
    try:
        selected = select_session(args.session, sessions_dir)
        report = build_report(parse_file(selected))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
