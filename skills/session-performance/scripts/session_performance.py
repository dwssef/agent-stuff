#!/usr/bin/env python3
"""Read-only Codex session performance monitor.

The monitor deliberately treats the rollout JSONL and SQLite files as an event stream.  It
does not write, checkpoint, vacuum, or otherwise modify any Codex state.  Unknown fields are
ignored but counted so a forward-compatible format cannot silently look complete.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote


UTC = timezone.utc
TOOL_CALL_TYPES = {"custom_tool_call", "function_call"}
TOOL_OUTPUT_TYPES = {"custom_tool_call_output", "function_call_output"}
FAIL_RE = re.compile(
    r"(?i)(?:\b(?:error|failed|failure|timeout|timed\s*out|cancelled|canceled|terminated)\b|"
    r"exit\s+code\s*[1-9]|non[- ]zero|permission denied|traceback)"
)
SECRET_RE = re.compile(r"(?i)(?:token|secret|password|api[_-]?key|authorization)\s*[=:]\s*[^\s,;]+")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", re.I)
LONG_ID_RE = re.compile(r"\b[0-9a-f]{24,}\b", re.I)
TURN_RE = re.compile(r"(?:turn_id|turn\.id)=([0-9a-f-]{16,})", re.I)
CALL_RE = re.compile(r"call_id=(call_[^\s}]+)")
TOOL_RE = re.compile(r"tool_name=([^\s}]+)")
DISPATCH_RE = re.compile(r"dispatch_duration_ms=(-?\d+(?:\.\d+)?)")
HANDLER_RE = re.compile(r"handler_duration_ms=(-?\d+(?:\.\d+)?)")
TOTAL_RE = re.compile(r"total_duration_ms=(-?\d+(?:\.\d+)?)")
RETRY_RE = re.compile(r"\((\d+)\s*/\s*(\d+)\s+in\s+(\d+(?:\.\d+)?)ms\)")
ESTIMATED_RE = re.compile(r"estimated_token_count=(?:Some\()?([0-9]+)")
AUTO_SCOPE_RE = re.compile(r"auto_compact_scope_tokens=([0-9]+)")
AUTO_LIMIT_RE = re.compile(r"auto_compact_scope_limit=(?:Some\()?([0-9]+)")
TOTAL_USAGE_RE = re.compile(r"total_usage_tokens=([0-9]+)")
BOOL_RE = re.compile(r"full_context_window_limit_reached=(true|false)")


def parse_time(value: Any) -> Optional[float]:
    """Parse an ISO timestamp into UTC seconds."""
    if not isinstance(value, str):
        return None
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        result = datetime.fromisoformat(text)
        if result.tzinfo is None:
            result = result.replace(tzinfo=UTC)
        return result.astimezone(UTC).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def epoch_time(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def number(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def integer(value: Any, default: Optional[int] = None) -> Optional[int]:
    result = number(value, None)
    if result is None:
        return default
    return int(result)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def percentile(values: Sequence[float], quantile: float) -> Optional[float]:
    clean = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    position = (len(clean) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return clean[lower]
    fraction = position - lower
    return clean[lower] + (clean[upper] - clean[lower]) * fraction


def iso_time(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def extract_text(value: Any) -> str:
    """Extract text for private heuristics; never return it in a report."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(extract_text(item) for item in value)
    if isinstance(value, dict):
        preferred = []
        for key in ("text", "message", "output_text", "content", "summary", "stderr", "stdout"):
            if key in value:
                preferred.append(extract_text(value[key]))
        if preferred:
            return " ".join(preferred)
        return " ".join(extract_text(item) for item in value.values())
    return ""


def text_size(value: Any) -> int:
    return len(extract_text(value).encode("utf-8", "replace"))


def scrub(value: str) -> str:
    value = SECRET_RE.sub("<secret>", value)
    value = UUID_RE.sub("<id>", value)
    value = LONG_ID_RE.sub("<id>", value)
    return re.sub(r"\s+", " ", value).strip()


def digest(value: str) -> str:
    return hashlib.sha256(scrub(value).encode("utf-8", "replace")).hexdigest()[:16]


def normalize_for_signature(value: Any) -> Any:
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key in sorted(value):
            if re.search(r"(?i)(token|secret|password|api[_-]?key|authorization)", str(key)):
                result[str(key)] = "<secret>"
            else:
                result[str(key)] = normalize_for_signature(value[key])
        return result
    if isinstance(value, list):
        return [normalize_for_signature(item) for item in value]
    if isinstance(value, str):
        return scrub(value)
    return value


def canonical_call_input(payload: Dict[str, Any]) -> str:
    raw = payload.get("input", payload.get("arguments", ""))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = scrub(raw)
    try:
        return json.dumps(normalize_for_signature(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return scrub(text_value(raw))


def classify_tool(name: str, raw: Any) -> str:
    text = (name + " " + text_value(raw)).lower()
    if any(word in text for word in ("pytest", "npm test", "pnpm test", "yarn test", "cargo test", "quick_validate")):
        return "test"
    if re.search(r"\b(rg|grep|ripgrep|find|git\s+(?:grep|log))\b", text):
        return "search"
    if re.search(r"\b(sed|head|tail|less|more|cat|jq|git\s+(?:show|diff|status))\b", text):
        return "read"
    if "apply_patch" in text or re.search(r"\b(cp|mv|mkdir|tee|write)\b", text):
        return "write"
    if name in {"request_user_input", "request_plugin_install"}:
        return "interaction"
    if name in {"exec", "exec_command"}:
        return "shell"
    return "other"


def get_turn_id(payload: Dict[str, Any]) -> Optional[str]:
    direct = payload.get("turn_id")
    if direct:
        return str(direct)
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(metadata, dict) and metadata.get("turn_id"):
        return str(metadata["turn_id"])
    return None


def usage_dict(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, int] = {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
        parsed = integer(value.get(key))
        if parsed is not None and parsed >= 0:
            result[key] = parsed
    return result


@dataclass
class SessionMeta:
    line_no: int
    timestamp: Optional[float]
    session_id: Optional[str]


@dataclass
class TokenSample:
    line_no: int
    timestamp: Optional[float]
    cumulative: Dict[str, int]
    last: Dict[str, int]
    context_window: Optional[int]


@dataclass
class CompactEvent:
    line_no: int
    timestamp: Optional[float]
    window_id: Optional[str]
    previous_window_id: Optional[str]
    window_number: Optional[int]
    marker: bool = False


@dataclass
class Turn:
    turn_id: str
    start_line: Optional[int] = None
    start_ts: Optional[float] = None
    started_at_payload: Optional[float] = None
    complete_line: Optional[int] = None
    complete_ts: Optional[float] = None
    completed_at_payload: Optional[float] = None
    duration_ms: Optional[float] = None
    duration_exact: bool = False
    ttft_ms: Optional[float] = None
    ttft_exact: bool = False
    last_agent_message_present: bool = False
    aborted: bool = False
    tool_call_ids: List[str] = field(default_factory=list)
    reasoning_timestamps: List[float] = field(default_factory=list)
    message_timestamps: List[float] = field(default_factory=list)
    other_model_timestamps: List[float] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.complete_line is not None


@dataclass
class ToolCall:
    line_no: int
    timestamp: Optional[float]
    call_id: Optional[str]
    turn_id: Optional[str]
    name: str
    category: str
    signature: str
    status: str = ""
    output_seen: bool = False
    output_timestamp: Optional[float] = None
    output_bytes: int = 0
    failed: bool = False
    record_id: Optional[str] = None
    timing_source: Optional[str] = None
    total_ms: Optional[float] = None
    handler_ms: Optional[float] = None
    dispatch_ms: Optional[float] = None
    log_timestamp: Optional[float] = None


@dataclass
class TurnLogContext:
    timestamp: float
    turn_id: Optional[str]
    estimated_token_count: Optional[int]
    auto_compact_scope_tokens: Optional[int]
    auto_compact_scope_limit: Optional[int]
    total_usage_tokens: Optional[int]
    full_context_window_limit_reached: Optional[bool]


@dataclass
class RetryEvent:
    timestamp: float
    turn_id: Optional[str]
    attempt: int
    attempts: int
    backoff_ms: float


@dataclass
class RolloutData:
    path: Path
    raw_lines: int = 0
    malformed_lines: int = 0
    missing_timestamps: int = 0
    unknown_records: int = 0
    unknown_event_types: Counter = field(default_factory=Counter)
    session_metas: List[SessionMeta] = field(default_factory=list)
    token_samples: List[TokenSample] = field(default_factory=list)
    compactions: List[CompactEvent] = field(default_factory=list)
    turns: Dict[str, Turn] = field(default_factory=dict)
    calls: Dict[str, ToolCall] = field(default_factory=dict)
    output_only_calls: int = 0
    reasoning_events: int = 0
    message_events: int = 0
    turn_aborted_events: int = 0
    last_timestamp: Optional[float] = None
    first_timestamp: Optional[float] = None
    file_changed_during_read: bool = False
    partial_last_line: bool = False
    segment_start_line: int = 1
    target_session_id: Optional[str] = None
    context_windows: List[int] = field(default_factory=list)


@dataclass
class LogData:
    db_paths: List[Path] = field(default_factory=list)
    rows: int = 0
    target_counts: Counter = field(default_factory=Counter)
    tool_timings: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    turn_contexts: List[TurnLogContext] = field(default_factory=list)
    retries: List[RetryEvent] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    truncated: bool = False
    retention_cap: Optional[int] = None


@dataclass
class StateData:
    db_path: Optional[Path] = None
    row: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def parse_rollout(path: Path) -> RolloutData:
    data = RolloutData(path=path)
    try:
        before = path.stat()
    except OSError:
        before = None
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"无法读取 rollout：{path} ({exc})") from exc
    with handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            data.raw_lines += 1
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                if not raw.endswith("\n"):
                    data.partial_last_line = True
                else:
                    data.malformed_lines += 1
                continue
            if not isinstance(record, dict):
                data.unknown_records += 1
                continue
            timestamp = parse_time(record.get("timestamp"))
            if timestamp is None:
                data.missing_timestamps += 1
            else:
                data.first_timestamp = timestamp if data.first_timestamp is None else min(data.first_timestamp, timestamp)
                data.last_timestamp = timestamp if data.last_timestamp is None else max(data.last_timestamp, timestamp)
            record_type = record.get("type")
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            payload_type = payload.get("type")

            if record_type == "session_meta":
                session_id = payload.get("id") or payload.get("session_id")
                data.session_metas.append(SessionMeta(line_no, timestamp, str(session_id) if session_id else None))
                context_window = integer(payload.get("context_window"))
                if context_window and context_window not in data.context_windows:
                    data.context_windows.append(context_window)
                continue

            if record_type == "compacted":
                data.compactions.append(
                    CompactEvent(
                        line_no,
                        timestamp,
                        str(payload.get("window_id")) if payload.get("window_id") else None,
                        str(payload.get("previous_window_id")) if payload.get("previous_window_id") else None,
                        integer(payload.get("window_number")),
                    )
                )
                continue

            if record_type == "event_msg":
                event_type = str(payload_type or "unknown")
                if event_type == "task_started":
                    turn_id = payload.get("turn_id")
                    context_window = integer(payload.get("model_context_window"))
                    if context_window and context_window not in data.context_windows:
                        data.context_windows.append(context_window)
                    if turn_id:
                        turn = data.turns.setdefault(str(turn_id), Turn(str(turn_id)))
                        start = timestamp if timestamp is not None else epoch_time(payload.get("started_at"))
                        if turn.start_ts is None or (start is not None and start < turn.start_ts):
                            turn.start_ts = start
                            turn.start_line = line_no
                        turn.started_at_payload = epoch_time(payload.get("started_at"))
                elif event_type == "task_complete":
                    turn_id = payload.get("turn_id")
                    if turn_id:
                        turn = data.turns.setdefault(str(turn_id), Turn(str(turn_id)))
                        completed = timestamp if timestamp is not None else epoch_time(payload.get("completed_at"))
                        turn.complete_ts = completed
                        turn.complete_line = line_no
                        turn.completed_at_payload = epoch_time(payload.get("completed_at"))
                        duration = number(payload.get("duration_ms"))
                        turn.duration_ms = duration if duration is not None and duration >= 0 else None
                        turn.duration_exact = turn.duration_ms is not None
                        ttft = number(payload.get("time_to_first_token_ms"))
                        turn.ttft_ms = ttft if ttft is not None and ttft >= 0 else None
                        turn.ttft_exact = turn.ttft_ms is not None
                        turn.last_agent_message_present = "last_agent_message" in payload
                elif event_type == "token_count":
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    data.token_samples.append(
                        TokenSample(
                            line_no,
                            timestamp,
                            usage_dict(info.get("total_token_usage")),
                            usage_dict(info.get("last_token_usage")),
                            integer(info.get("model_context_window")),
                        )
                    )
                elif event_type == "context_compacted":
                    data.compactions.append(CompactEvent(line_no, timestamp, None, None, None, marker=True))
                elif event_type == "turn_aborted":
                    data.turn_aborted_events += 1
                    turn_id = get_turn_id(payload)
                    if turn_id and turn_id in data.turns:
                        data.turns[turn_id].aborted = True
                elif event_type == "agent_reasoning":
                    turn_id = get_turn_id(payload)
                    if turn_id and timestamp is not None:
                        data.turns.setdefault(turn_id, Turn(turn_id)).reasoning_timestamps.append(timestamp)
                else:
                    data.unknown_event_types[event_type] += 1
                continue

            if record_type == "response_item":
                item_type = str(payload_type or "unknown")
                turn_id = get_turn_id(payload)
                if turn_id:
                    data.turns.setdefault(turn_id, Turn(turn_id))
                if item_type in TOOL_CALL_TYPES:
                    call_id = payload.get("call_id") or payload.get("id")
                    if call_id:
                        key = str(call_id)
                        if key not in data.calls:
                            raw_input = payload.get("input", payload.get("arguments", ""))
                            canonical = canonical_call_input(payload)
                            data.calls[key] = ToolCall(
                                line_no=line_no,
                                timestamp=timestamp,
                                call_id=key,
                                turn_id=turn_id,
                                name=str(payload.get("name") or "unknown"),
                                category=classify_tool(str(payload.get("name") or "unknown"), raw_input),
                                signature=digest(canonical),
                                status=str(payload.get("status") or ""),
                                record_id=str(payload.get("id")) if payload.get("id") else None,
                            )
                        else:
                            existing = data.calls[key]
                            if existing.status == "" and payload.get("status"):
                                existing.status = str(payload.get("status"))
                        if turn_id and key not in data.turns[turn_id].tool_call_ids:
                            data.turns[turn_id].tool_call_ids.append(key)
                elif item_type in TOOL_OUTPUT_TYPES:
                    call_id = payload.get("call_id")
                    if call_id and str(call_id) in data.calls:
                        call = data.calls[str(call_id)]
                        call.output_seen = True
                        call.output_timestamp = timestamp
                        call.output_bytes += text_size(payload.get("output", payload.get("result", "")))
                        status = call.status.lower()
                        call.failed = call.failed or status in {"failed", "error", "cancelled", "canceled", "timeout"}
                        call.failed = call.failed or bool(FAIL_RE.search(extract_text(payload.get("output", payload.get("result", "")))))
                    else:
                        data.output_only_calls += 1
                elif item_type == "reasoning":
                    data.reasoning_events += 1
                    if turn_id and timestamp is not None:
                        data.turns[turn_id].reasoning_timestamps.append(timestamp)
                elif item_type == "message":
                    data.message_events += 1
                    if turn_id and timestamp is not None:
                        data.turns[turn_id].message_timestamps.append(timestamp)
                elif turn_id and timestamp is not None:
                    data.turns[turn_id].other_model_timestamps.append(timestamp)
                continue

            # world_state and forward-compatible record types are not performance events.
            if record_type not in {"world_state", "turn_context"}:
                data.unknown_records += 1

    try:
        after = path.stat()
        data.file_changed_during_read = bool(before and (before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns))
    except OSError:
        pass
    return data


def choose_segment(data: RolloutData, target_session_id: Optional[str]) -> None:
    """Exclude replayed history when a rollout contains another session's metadata block.

    A resumed rollout can contain a long prefix whose session_meta id differs from the current
    thread.  session_meta entries may be repeated between turns, so the boundary is the first
    target entry after the last different id, not simply the last target entry.
    """
    data.target_session_id = target_session_id
    entries = [item for item in data.session_metas if item.session_id]
    if not entries:
        data.segment_start_line = 1
        return
    target = target_session_id or entries[0].session_id
    target_lines = [item.line_no for item in entries if item.session_id == target]
    if not target_lines:
        data.segment_start_line = 1
        return
    different_lines = [item.line_no for item in entries if item.session_id != target]
    last_different = max(different_lines, default=0)
    after_different = [line for line in target_lines if line > last_different]
    data.segment_start_line = min(after_different or target_lines)


def active_line(data: RolloutData, line_no: Optional[int]) -> bool:
    return line_no is not None and line_no >= data.segment_start_line


def active_turns(data: RolloutData) -> List[Turn]:
    result = []
    for turn in data.turns.values():
        if active_line(data, turn.start_line) or active_line(data, turn.complete_line):
            result.append(turn)
    result.sort(key=lambda item: (item.start_ts is None, item.start_ts or item.complete_ts or 0.0, item.start_line or item.complete_line or 0))
    return result


def active_calls(data: RolloutData) -> List[ToolCall]:
    result = [call for call in data.calls.values() if active_line(data, call.line_no)]
    result.sort(key=lambda item: (item.timestamp is None, item.timestamp or 0.0, item.line_no))
    return result


def active_token_samples(data: RolloutData) -> List[TokenSample]:
    result = [sample for sample in data.token_samples if active_line(data, sample.line_no)]
    result.sort(key=lambda item: (item.timestamp is None, item.timestamp or 0.0, item.line_no))
    return result


def active_compactions(data: RolloutData) -> Tuple[List[CompactEvent], List[CompactEvent]]:
    current = [item for item in data.compactions if active_line(data, item.line_no) and not item.marker]
    inherited = [item for item in data.compactions if not active_line(data, item.line_no) and not item.marker]
    current.sort(key=lambda item: (item.timestamp is None, item.timestamp or 0.0, item.line_no))
    return current, inherited


def sqlite_connect(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=1000")
    return connection


def candidate_databases(codex_home: Path, prefix: str) -> List[Path]:
    paths = list(codex_home.glob(f"{prefix}_*.sqlite")) + list(codex_home.glob(f"{prefix}.sqlite"))
    return sorted({path for path in paths if path.is_file()}, key=lambda item: item.stat().st_mtime, reverse=True)


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return bool(row)


def read_state(codex_home: Path, thread_id: Optional[str]) -> StateData:
    result = StateData()
    for path in candidate_databases(codex_home, "state"):
        try:
            connection = sqlite_connect(path)
            if not table_exists(connection, "threads"):
                connection.close()
                continue
            columns = [row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()]
            wanted = [column for column in ("id", "rollout_path", "created_at", "updated_at", "source", "model_provider", "cwd", "tokens_used", "archived", "cli_version", "model", "reasoning_effort", "updated_at_ms", "recency_at_ms", "has_user_event") if column in columns]
            if not wanted:
                connection.close()
                continue
            select = ",".join('"' + column + '"' for column in wanted)
            row = None
            if thread_id:
                row = connection.execute(f"SELECT {select} FROM threads WHERE id=?", (thread_id,)).fetchone()
            if row is None and not thread_id:
                order_column = "recency_at_ms" if "recency_at_ms" in columns else "updated_at_ms" if "updated_at_ms" in columns else "updated_at"
                row = connection.execute(f"SELECT {select} FROM threads ORDER BY {order_column} DESC LIMIT 1").fetchone()
            if row is not None:
                result.db_path = path
                result.row = dict(zip(wanted, row))
                connection.close()
                return result
            connection.close()
        except sqlite3.Error as exc:
            result.error = f"state DB {path.name}: {exc}"
    return result


def find_rollout_files(sessions_dir: Path) -> List[Path]:
    if not sessions_dir.exists():
        return []
    return sorted((path for path in sessions_dir.rglob("rollout-*.jsonl") if path.is_file()), key=lambda item: item.stat().st_mtime, reverse=True)


def infer_session_id_from_name(path: Path) -> Optional[str]:
    match = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27,})\.jsonl$", path.name, re.I)
    return match.group(1) if match else None


def resolve_session(args: argparse.Namespace, codex_home: Path) -> Tuple[Path, Optional[str], StateData]:
    requested = args.thread
    if requested is None:
        requested = os.environ.get("CODEX_THREAD_ID")
    looks_like_path = bool(requested and ("/" in requested or requested.endswith(".jsonl")))
    requested_path = Path(requested).expanduser() if looks_like_path else None
    state = read_state(codex_home, requested if not looks_like_path else None)

    if requested_path and requested_path.is_file():
        return requested_path, infer_session_id_from_name(requested_path), state
    state_path = state.row.get("rollout_path") if state.row else None
    if state_path:
        candidate = Path(str(state_path)).expanduser()
        if candidate.is_file():
            return candidate, str(state.row.get("id") or infer_session_id_from_name(candidate) or ""), state

    sessions_dir = Path(args.sessions_dir).expanduser() if args.sessions_dir else codex_home / "sessions"
    files = find_rollout_files(sessions_dir)
    if requested and not looks_like_path:
        matching = [path for path in files if requested in path.name]
        if matching:
            return matching[0], requested, state
        # A filename can be non-standard; inspect the small metadata prefix only as a fallback.
        for path in files:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for _ in range(80):
                        line = handle.readline()
                        if not line:
                            break
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = record.get("payload") if isinstance(record, dict) and isinstance(record.get("payload"), dict) else {}
                        if record.get("type") == "session_meta" and str(payload.get("id")) == requested:
                            return path, requested, state
            except OSError:
                continue
    if files:
        return files[0], infer_session_id_from_name(files[0]), state
    raise RuntimeError(f"找不到 rollout 文件：{sessions_dir}/**/rollout-*.jsonl")


def parse_log_body(body: Any) -> str:
    return body if isinstance(body, str) else text_value(body)


def read_logs(codex_home: Path, thread_id: Optional[str], cap: int) -> LogData:
    result = LogData()
    if not thread_id:
        result.errors.append("没有 thread_id，无法关联 logs")
        return result
    for path in candidate_databases(codex_home, "logs"):
        try:
            connection = sqlite_connect(path)
            if not table_exists(connection, "logs"):
                connection.close()
                continue
            count = integer(connection.execute("SELECT COUNT(*) FROM logs WHERE thread_id=?", (thread_id,)).fetchone()[0], 0) or 0
            if count == 0:
                connection.close()
                continue
            result.db_paths.append(path)
            result.rows += count
            if count >= cap:
                result.truncated = True
                result.retention_cap = cap
            rows = connection.execute(
                "SELECT ts, ts_nanos, target, feedback_log_body FROM logs WHERE thread_id=? ORDER BY ts, ts_nanos, id",
                (thread_id,),
            )
            for ts, ts_nanos, target, body in rows:
                timestamp = float(ts or 0) + float(ts_nanos or 0) / 1_000_000_000.0
                target_text = str(target or "")
                body_text = parse_log_body(body)
                result.target_counts[target_text] += 1
                turn_match = TURN_RE.search(body_text)
                turn_id = turn_match.group(1) if turn_match else None
                if target_text == "codex_core::tools::parallel" and "tool call completed" in body_text:
                    call_match = CALL_RE.search(body_text)
                    tool_match = TOOL_RE.search(body_text)
                    total_match = TOTAL_RE.search(body_text)
                    if call_match and total_match:
                        call_id = call_match.group(1)
                        timing = {
                            "call_id": call_id,
                            "turn_id": turn_id,
                            "tool_name": tool_match.group(1) if tool_match else "unknown",
                            "dispatch_ms": number(DISPATCH_RE.search(body_text).group(1)) if DISPATCH_RE.search(body_text) else None,
                            "handler_ms": number(HANDLER_RE.search(body_text).group(1)) if HANDLER_RE.search(body_text) else None,
                            "total_ms": number(total_match.group(1)),
                            "timestamp": timestamp,
                        }
                        old = result.tool_timings.get(call_id)
                        if old is None or (timing.get("total_ms") or -1) >= (old.get("total_ms") or -1):
                            result.tool_timings[call_id] = timing
                elif target_text == "codex_core::session::turn" and "post sampling token usage" in body_text:
                    estimated_match = ESTIMATED_RE.search(body_text)
                    scope_match = AUTO_SCOPE_RE.search(body_text)
                    limit_match = AUTO_LIMIT_RE.search(body_text)
                    usage_match = TOTAL_USAGE_RE.search(body_text)
                    bool_match = BOOL_RE.search(body_text)
                    result.turn_contexts.append(
                        TurnLogContext(
                            timestamp,
                            turn_id,
                            integer(estimated_match.group(1)) if estimated_match else None,
                            integer(scope_match.group(1)) if scope_match else None,
                            integer(limit_match.group(1)) if limit_match else None,
                            integer(usage_match.group(1)) if usage_match else None,
                            bool_match.group(1) == "true" if bool_match else None,
                        )
                    )
                elif target_text == "codex_core::responses_retry":
                    retry_match = RETRY_RE.search(body_text)
                    if retry_match:
                        result.retries.append(
                            RetryEvent(timestamp, turn_id, int(retry_match.group(1)), int(retry_match.group(2)), float(retry_match.group(3)))
                        )
            connection.close()
        except sqlite3.Error as exc:
            result.errors.append(f"logs DB {path.name}: {exc}")
    result.turn_contexts.sort(key=lambda item: item.timestamp)
    result.retries.sort(key=lambda item: item.timestamp)
    return result


def nearest_samples(samples: Sequence[TokenSample], timestamp: Optional[float], before: bool) -> Optional[TokenSample]:
    if timestamp is None:
        return None
    candidates = [sample for sample in samples if sample.timestamp is not None and ((sample.timestamp <= timestamp) if before else (sample.timestamp >= timestamp))]
    if not candidates:
        return None
    return (max(candidates, key=lambda item: (item.timestamp or 0.0, item.line_no)) if before else min(candidates, key=lambda item: (item.timestamp or 0.0, item.line_no)))


def token_delta_for_turn(turn: Turn, all_samples: Sequence[TokenSample], active_samples: Sequence[TokenSample]) -> Dict[str, Any]:
    fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    if turn.start_ts is None or turn.complete_ts is None:
        return {"values": {}, "method": "unavailable", "confidence": "low"}
    ordered_all = sorted((sample for sample in all_samples if sample.timestamp is not None), key=lambda item: (item.timestamp or 0.0, item.line_no))
    inside = [sample for sample in ordered_all if turn.start_ts <= (sample.timestamp or 0.0) <= turn.complete_ts]
    before = nearest_samples(ordered_all, turn.start_ts, True)
    after = max(inside, key=lambda item: (item.timestamp or 0.0, item.line_no)) if inside else nearest_samples(ordered_all, turn.complete_ts, True)
    if before and after and before.cumulative and after.cumulative and after is not before:
        values: Dict[str, int] = {}
        reset = False
        for field_name in fields:
            left = before.cumulative.get(field_name)
            right = after.cumulative.get(field_name)
            if left is not None and right is not None:
                if right < left:
                    reset = True
                else:
                    values[field_name] = right - left
        if values and not reset:
            confidence = "high" if inside and active_line_placeholder(before, active_samples) else "medium"
            return {"values": values, "method": "cumulative_boundary_delta", "confidence": confidence}
    if inside:
        values = {field_name: sum(sample.last.get(field_name, 0) for sample in inside) for field_name in fields}
        return {"values": values, "method": "last_usage_sum", "confidence": "low"}
    return {"values": {}, "method": "unavailable", "confidence": "low"}


def active_line_placeholder(sample: TokenSample, active_samples: Sequence[TokenSample]) -> bool:
    return any(sample.line_no == item.line_no for item in active_samples)


def latest_usage(samples: Sequence[TokenSample]) -> Dict[str, int]:
    ordered = sorted((sample for sample in samples if sample.timestamp is not None), key=lambda item: (item.timestamp or 0.0, item.line_no))
    return dict(ordered[-1].cumulative) if ordered else {}


def context_snapshots(data: RolloutData, logs: LogData) -> List[Dict[str, Any]]:
    snapshots: List[Dict[str, Any]] = []
    for sample in active_token_samples(data):
        value = sample.last.get("input_tokens")
        if value is None:
            value = sample.last.get("total_tokens")
        if value is not None and sample.timestamp is not None:
            snapshots.append(
                {
                    "timestamp": sample.timestamp,
                    "value": value,
                    "window": sample.context_window,
                    "source": "token_count.last.input_tokens" if "input_tokens" in sample.last else "token_count.last.total_tokens",
                }
            )
    start_ts = None
    if data.segment_start_line > 1:
        metas = [item for item in data.session_metas if item.line_no == data.segment_start_line]
        start_ts = metas[0].timestamp if metas else None
    for item in logs.turn_contexts:
        if start_ts is not None and item.timestamp < start_ts:
            continue
        value = item.estimated_token_count or item.auto_compact_scope_tokens
        if value is not None:
            snapshots.append(
                {
                    "timestamp": item.timestamp,
                    "value": value,
                    "window": item.auto_compact_scope_limit,
                    "source": "logs.estimated_token_count" if item.estimated_token_count is not None else "logs.auto_compact_scope_tokens",
                    "turn_id": item.turn_id,
                }
            )
    snapshots.sort(key=lambda item: item["timestamp"])
    return snapshots


def tool_interval(call: ToolCall) -> Optional[Tuple[float, float]]:
    if call.timestamp is not None and call.output_timestamp is not None and call.output_timestamp >= call.timestamp:
        return call.timestamp, call.output_timestamp
    if call.log_timestamp is not None and call.total_ms is not None:
        return call.log_timestamp - call.total_ms / 1000.0, call.log_timestamp
    return None


def union_duration(intervals: Iterable[Tuple[float, float]], start: Optional[float], end: Optional[float]) -> Optional[float]:
    normalized = []
    for left, right in intervals:
        if end is not None:
            right = min(right, end)
        if start is not None:
            left = max(left, start)
        if right > left:
            normalized.append((left, right))
    if not normalized:
        return None
    normalized.sort()
    total = 0.0
    current_left, current_right = normalized[0]
    for left, right in normalized[1:]:
        if left <= current_right:
            current_right = max(current_right, right)
        else:
            total += current_right - current_left
            current_left, current_right = left, right
    return (total + current_right - current_left) * 1000.0


def enrich_calls(calls: Sequence[ToolCall], logs: LogData) -> Tuple[List[ToolCall], int]:
    unmatched_timing = 0
    call_ids = {call.call_id for call in calls if call.call_id}
    for call_id, timing in logs.tool_timings.items():
        if call_id not in call_ids:
            unmatched_timing += 1
    for call in calls:
        timing = logs.tool_timings.get(call.call_id or "")
        if timing:
            call.timing_source = "logs.codex_core.tools.parallel"
            call.total_ms = timing.get("total_ms")
            call.handler_ms = timing.get("handler_ms")
            call.dispatch_ms = timing.get("dispatch_ms")
            call.log_timestamp = timing.get("timestamp")
        elif call.timestamp is not None and call.output_timestamp is not None and call.output_timestamp >= call.timestamp:
            call.timing_source = "rollout.call_to_output"
            call.total_ms = (call.output_timestamp - call.timestamp) * 1000.0
    return list(calls), unmatched_timing


def context_for_turn(turn: Turn, snapshots: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if turn.complete_ts is None:
        return None
    candidates = [item for item in snapshots if item["timestamp"] <= turn.complete_ts]
    if not candidates:
        return None
    return dict(candidates[-1])


def turn_token_values(turn: Turn, data: RolloutData) -> Dict[str, Any]:
    return token_delta_for_turn(turn, data.token_samples, active_token_samples(data))


def retry_metrics_for_turn(turn: Turn, retries: Sequence[RetryEvent]) -> Dict[str, Any]:
    values = [item for item in retries if turn.start_ts is not None and turn.complete_ts is not None and turn.start_ts <= item.timestamp <= turn.complete_ts]
    return {"count": len(values), "backoff_ms": round(sum(item.backoff_ms for item in values), 1)}


def tool_metrics_for_turn(turn: Turn, calls: Sequence[ToolCall], logs: LogData) -> Dict[str, Any]:
    by_id = {call.call_id: call for call in calls}
    selected = [by_id[call_id] for call_id in turn.tool_call_ids if call_id in by_id]
    timed = [call for call in selected if call.total_ms is not None]
    values = [call.total_ms for call in timed if call.total_ms is not None]
    intervals = [interval for call in timed if (interval := tool_interval(call)) is not None]
    by_name: Dict[str, Dict[str, float]] = defaultdict(lambda: {"calls": 0, "output_bytes": 0, "total_ms": 0})
    for call in selected:
        item = by_name[call.name]
        item["calls"] += 1
        item["output_bytes"] += call.output_bytes
        item["total_ms"] += call.total_ms or 0.0
    failed = sum(1 for call in selected if call.failed or call.status.lower() in {"failed", "error", "timeout", "cancelled", "canceled"})
    signature_counts = Counter((call.category, call.name, call.signature) for call in selected)
    duplicate_excess = sum(max(0, count - 1) for count in signature_counts.values())
    longest = max(timed, key=lambda call: call.total_ms or -1, default=None)
    top_output = max(selected, key=lambda call: call.output_bytes, default=None)
    return {
        "calls": len(selected),
        "timed_calls": len(timed),
        "timing_coverage": (len(timed) / len(selected)) if selected else None,
        "sum_total_ms": round(sum(values), 1) if values else 0.0,
        "sum_handler_ms": round(sum(call.handler_ms or 0.0 for call in timed), 1) if timed else 0.0,
        "median_ms": round(median(values), 1) if values else None,
        "p95_ms": round(percentile(values, 0.95), 1) if values else None,
        "max_ms": round(max(values), 1) if values else None,
        "longest": {"name": longest.name, "category": longest.category, "total_ms": round(longest.total_ms or 0.0, 1), "source": longest.timing_source} if longest else None,
        "failed": failed,
        "failure_rate": failed / len(selected) if selected else None,
        "duplicate_excess": duplicate_excess,
        "duplicate_ratio": duplicate_excess / len(selected) if selected else None,
        "output_bytes": sum(call.output_bytes for call in selected),
        "top_output": {"name": top_output.name, "category": top_output.category, "bytes": top_output.output_bytes} if top_output and top_output.output_bytes else None,
        "tool_wall_ms_approx": round(union_duration(intervals, turn.start_ts, turn.complete_ts), 1) if intervals else None,
        "parallel_overlap_possible": len(intervals) > 1,
        "by_name": {name: {key: round(value, 1) if isinstance(value, float) else value for key, value in stats.items()} for name, stats in by_name.items()},
        "retry": retry_metrics_for_turn(turn, logs.retries),
    }


def trend(values: Sequence[Optional[float]], absolute_floor: float) -> Dict[str, Any]:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if len(clean) < 4:
        return {"direction": "unknown", "n": len(clean), "baseline": None, "recent": None, "ratio": None, "delta": None, "worsening": False}
    split = max(2, len(clean) // 2)
    baseline_values = clean[:split]
    recent_values = clean[split:]
    baseline = median(baseline_values)
    recent = median(recent_values)
    delta = recent - baseline
    ratio = recent / baseline if baseline > 0 else None
    worsening = bool(ratio is not None and ratio >= 1.25 and delta >= absolute_floor)
    improving = bool(ratio is not None and ratio <= 0.80 and -delta >= absolute_floor)
    direction = "up" if worsening else "down" if improving else "flat"
    return {
        "direction": direction,
        "n": len(clean),
        "baseline": round(baseline, 2),
        "recent": round(recent, 2),
        "ratio": round(ratio, 3) if ratio is not None else None,
        "delta": round(delta, 2),
        "worsening": worsening,
    }


def compact_metrics(data: RolloutData, turns: Sequence[Turn], snapshots: Sequence[Dict[str, Any]], limit: Optional[int]) -> Dict[str, Any]:
    current, inherited = active_compactions(data)
    marker_count = sum(1 for item in data.compactions if item.marker and active_line(data, item.line_no))
    unique: Dict[str, CompactEvent] = {}
    for item in current:
        key = item.window_id or f"line:{item.line_no}"
        unique.setdefault(key, item)
    events = sorted(unique.values(), key=lambda item: (item.timestamp is None, item.timestamp or 0.0, item.line_no))
    timestamps = [item.timestamp for item in events if item.timestamp is not None]
    intervals = [(right - left) / 60.0 for left, right in zip(timestamps, timestamps[1:]) if right >= left]
    before_after = []
    rapid_refills = 0
    short_intervals = 0
    completed = [turn for turn in turns if turn.completed and turn.complete_ts is not None]
    for index, event in enumerate(events):
        if event.timestamp is None:
            continue
        before = max((item for item in snapshots if item["timestamp"] <= event.timestamp), key=lambda item: item["timestamp"], default=None)
        after = min((item for item in snapshots if item["timestamp"] > event.timestamp + 1e-6), key=lambda item: item["timestamp"], default=None)
        next_event_ts = events[index + 1].timestamp if index + 1 < len(events) else None
        target = (limit or 0) * 0.80 if limit else None
        refill = None
        if target:
            candidates = [item for item in snapshots if item["timestamp"] > event.timestamp + 1e-6 and item["value"] >= target]
            if candidates:
                refill = {"minutes": round((candidates[0]["timestamp"] - event.timestamp) / 60.0, 2), "turns": sum(1 for turn in completed if event.timestamp <= (turn.complete_ts or 0) <= candidates[0]["timestamp"])}
                if refill["minutes"] <= 10 or refill["turns"] <= 2:
                    rapid_refills += 1
        if next_event_ts is not None:
            turns_between = sum(1 for turn in completed if event.timestamp <= (turn.complete_ts or 0) < next_event_ts)
            interval_minutes = (next_event_ts - event.timestamp) / 60.0
            if interval_minutes <= 10 or turns_between <= 2:
                short_intervals += 1
        before_after.append(
            {
                "timestamp": iso_time(event.timestamp),
                "window_id": event.window_id,
                "window_number": event.window_number,
                "context_before": before.get("value") if before else None,
                "context_after": after.get("value") if after else None,
                "refill": refill,
            }
        )
    return {
        "raw_count": len(current),
        "unique_count": len(events),
        "marker_count": marker_count,
        "inherited_count": len(inherited),
        "last_interval_minutes": round(intervals[-1], 2) if intervals else None,
        "median_interval_minutes": round(median(intervals), 2) if intervals else None,
        "mean_interval_minutes": round(mean(intervals), 2) if intervals else None,
        "density_per_completed_turn": len(events) / len(completed) if completed else None,
        "short_interval_count": short_intervals,
        "rapid_refill_count": rapid_refills,
        "events": before_after,
    }


def context_metrics(data: RolloutData, turns: Sequence[Turn], snapshots: Sequence[Dict[str, Any]], compactions: Dict[str, Any]) -> Dict[str, Any]:
    latest = snapshots[-1] if snapshots else None
    compact_events, _ = active_compactions(data)
    last_compact_ts = max((item.timestamp for item in compact_events if item.timestamp is not None), default=None)
    model_windows = [item["window"] for item in snapshots if item.get("source", "").startswith("token_count.") and item.get("window")]
    window = model_windows[-1] if model_windows else (data.context_windows[-1] if data.context_windows else None)
    runtime_limits = [item.get("window") for item in snapshots if item.get("source", "").startswith("logs.") and item.get("window")]
    auto_limit = runtime_limits[-1] if runtime_limits else None
    recent_contexts: List[Tuple[float, float]] = []
    for turn in turns:
        if not turn.completed or turn.complete_ts is None:
            continue
        item = context_for_turn(turn, snapshots)
        if item:
            recent_contexts.append((turn.complete_ts, float(item["value"])))
    recent_contexts = recent_contexts[-10:]
    deltas = [right[1] - left[1] for left, right in zip(recent_contexts, recent_contexts[1:]) if right[1] >= left[1]]
    rates = [delta / max((right[0] - left[0]) / 60.0, 1e-6) for (left, right), delta in zip(zip(recent_contexts, recent_contexts[1:]), [right[1] - left[1] for left, right in zip(recent_contexts, recent_contexts[1:])]) if delta >= 0]
    growth = median(rates) if rates else None
    current_value = latest.get("value") if latest else None
    occupancy = current_value / window if current_value is not None and window else None
    auto_occupancy = current_value / auto_limit if current_value is not None and auto_limit else None
    positive_per_turn = median(deltas) if deltas else None
    projected_turns = None
    if current_value is not None and auto_limit and positive_per_turn and positive_per_turn > 0 and current_value < auto_limit:
        projected_turns = (auto_limit - current_value) / positive_per_turn
    growth_trend = trend(deltas, max(100.0, abs(positive_per_turn or 0.0) * 0.25)) if len(deltas) >= 4 else {"direction": "unknown", "n": len(deltas), "worsening": False}
    return {
        "current_tokens": current_value,
        "current_source": latest.get("source") if latest else None,
        "snapshot_predates_latest_compaction": bool(latest and last_compact_ts is not None and latest["timestamp"] < last_compact_ts),
        "context_window": window,
        "auto_compact_limit": auto_limit,
        "occupancy": occupancy,
        "auto_compact_occupancy": auto_occupancy,
        "growth_tokens_per_min": round(growth, 1) if growth is not None else None,
        "growth_tokens_per_turn": round(positive_per_turn, 1) if positive_per_turn is not None else None,
        "projected_turns_to_auto_compact": round(projected_turns, 1) if projected_turns is not None else None,
        "growth_trend": growth_trend,
        "snapshots": len(snapshots),
        "last_10_turn_contexts": [{"timestamp": iso_time(ts), "tokens": value} for ts, value in recent_contexts],
        "compaction": compactions,
    }


def ratio_risk(value: Optional[float], baseline: Optional[float], soft: float, hard: float) -> Optional[float]:
    if value is None:
        return None
    absolute = clamp((value - soft) / max(hard - soft, 1e-9))
    relative = 0.0
    if baseline is not None and baseline > 0:
        relative = clamp((value / baseline - 1.0) / 0.75)
    return max(absolute, relative)


def score_report(
    turns: Sequence[Turn],
    recent_turn_reports: Sequence[Dict[str, Any]],
    trends: Dict[str, Any],
    tools: Dict[str, Any],
    context: Dict[str, Any],
    compactions: Dict[str, Any],
    token_confidence: str,
    data_quality: Dict[str, Any],
) -> Dict[str, Any]:
    components: List[Tuple[str, float, Optional[float]]] = []
    ttft_values = [item.get("ttft_ms") for item in recent_turn_reports]
    duration_values = [item.get("duration_ms") for item in recent_turn_reports]
    tool_p95_values = [item.get("tools", {}).get("p95_ms") for item in recent_turn_reports]
    throughput_values = [item.get("agent", {}).get("output_rate_tps") for item in recent_turn_reports]

    def latest_or_median(values: Sequence[Optional[float]]) -> Optional[float]:
        clean = [float(value) for value in values if value is not None]
        return median(clean) if clean else None

    ttft = latest_or_median(ttft_values)
    duration = latest_or_median(duration_values)
    tool_p95 = latest_or_median(tool_p95_values)
    throughput = latest_or_median(throughput_values)
    ttft_base = trends.get("ttft", {}).get("baseline")
    duration_base = trends.get("duration", {}).get("baseline")
    tool_base = trends.get("tool_p95", {}).get("baseline")
    throughput_base = trends.get("agent_output_rate", {}).get("baseline")
    components.append(("ttft", 20.0, ratio_risk(ttft, ttft_base, 8000.0, 20000.0)))
    components.append(("turn_duration", 20.0, ratio_risk(duration, duration_base, 60000.0, 300000.0)))

    tool_risk = ratio_risk(tool_p95, tool_base, 2000.0, 10000.0)
    failure_rate = tools.get("failure_rate")
    retry_rate = tools.get("retry_rate")
    if tool_risk is not None:
        tool_risk = max(tool_risk, clamp((failure_rate or 0.0) / 0.35), clamp((retry_rate or 0.0) / 0.25))
    elif failure_rate is not None or retry_rate is not None:
        tool_risk = max(clamp((failure_rate or 0.0) / 0.35), clamp((retry_rate or 0.0) / 0.25))
    components.append(("tools", 20.0, tool_risk))

    occupancy = context.get("auto_compact_occupancy") or context.get("occupancy")
    context_risk = clamp((occupancy - 0.75) / 0.20) if occupancy is not None else None
    if context_risk is not None:
        context_risk = max(context_risk, clamp((compactions.get("unique_count", 0) - 2) / 6.0))
        context_risk = max(context_risk, clamp(compactions.get("short_interval_count", 0) / 3.0))
        context_risk = max(context_risk, clamp(compactions.get("rapid_refill_count", 0) / 2.0))
        if context.get("growth_trend", {}).get("worsening"):
            context_risk = max(context_risk, 0.45)
    elif compactions.get("unique_count"):
        context_risk = max(clamp((compactions.get("unique_count", 0) - 2) / 6.0), clamp(compactions.get("short_interval_count", 0) / 3.0))
    elif context.get("growth_trend", {}).get("worsening"):
        context_risk = 0.45
    components.append(("context_compaction", 20.0, context_risk))

    duplicate_ratio = tools.get("duplicate_ratio")
    output_risk = clamp((tools.get("output_bytes", 0) - 500_000) / 2_000_000)
    churn_risk = max(clamp((duplicate_ratio or 0.0) / 0.45), output_risk) if duplicate_ratio is not None else output_risk
    components.append(("tool_churn", 10.0, churn_risk if tools.get("calls", 0) else None))

    throughput_risk = None
    if throughput is not None:
        throughput_risk = clamp((throughput_base - throughput) / max(throughput_base * 0.75, 1.0)) if throughput_base else 0.0
    components.append(("agent_throughput", 10.0, throughput_risk))

    available = [(name, weight, risk) for name, weight, risk in components if risk is not None]
    available_weight = sum(weight for _, weight, _ in available)
    if available_weight < 40.0:
        # A nearly blind report is neutral rather than falsely healthy or falsely alarming.
        raw_score = 50.0
    else:
        raw_score = 100.0 - 100.0 * sum(weight * risk for _, weight, risk in available) / available_weight
    raw_score = round(clamp(raw_score, 0.0, 100.0), 1)

    recent_failures = tools.get("recent_failure_rate")
    recent_retries = tools.get("recent_retry_count", 0)
    trend_pair = trends.get("ttft", {}).get("worsening") and trends.get("duration", {}).get("worsening")
    hard_reasons: List[str] = []
    if occupancy is not None and occupancy >= 0.90 and (context.get("projected_turns_to_auto_compact") is None or context.get("projected_turns_to_auto_compact") <= 2):
        hard_reasons.append("context_near_compact_limit")
    if compactions.get("short_interval_count", 0) >= 2 and compactions.get("rapid_refill_count", 0) >= 1:
        hard_reasons.append("compact_storm_and_refill")
    if recent_failures is not None and recent_failures >= 0.40 and tools.get("recent_calls", 0) >= 3:
        hard_reasons.append("consecutive_tool_failures")
    if trend_pair and (recent_failures or 0.0) >= 0.20:
        hard_reasons.append("latency_and_tool_failures_worsening")
    if compactions.get("unique_count", 0) >= 8 and (compactions.get("density_per_completed_turn") or 0.0) >= 0.25:
        hard_reasons.append("high_compaction_density")

    effective_score = min(raw_score, 34.0) if hard_reasons else raw_score
    no_completed_turn = data_quality.get("completed_turns", 0) == 0
    if no_completed_turn and not hard_reasons:
        # A live first turn can have tool observations but no completed-turn latency baseline.
        # Keep the score neutral and make the uncertainty explicit rather than reporting GOOD.
        effective_score = 50.0
        status = "SLOW"
        recommendation = "continue_current_session"
        recommendation_text = "继续当前 session，完成首个 turn 后再评估"
    elif effective_score >= 80:
        status = "GOOD"
        recommendation = "continue_current_session"
        recommendation_text = "继续当前 session"
    elif effective_score >= 60:
        status = "SLOW"
        recommendation = "finish_milestone_then_switch"
        recommendation_text = "完成当前 milestone 后切换（当前 session 仅做收尾）"
    elif effective_score >= 35:
        status = "DEGRADED"
        recommendation = "finish_milestone_then_switch"
        recommendation_text = "完成当前 milestone 后切换"
    else:
        status = "SWITCH SESSION"
        recommendation = "switch_now"
        recommendation_text = "立即新开 session"
    if hard_reasons:
        status = "SWITCH SESSION"
        recommendation = "switch_now"
        recommendation_text = "立即新开 session"

    confidence = "high"
    if data_quality.get("logs_truncated") or data_quality.get("partial_last_line") or token_confidence != "high" or data_quality.get("tool_timing_coverage", 1.0) < 0.80:
        confidence = "medium"
    if data_quality.get("malformed_lines") or data_quality.get("completed_turn_coverage", 1.0) < 0.75 or not data_quality.get("rollout_available"):
        confidence = "low"
    return {
        "health_score": effective_score,
        "raw_score": raw_score,
        "status": status,
        "recommendation": recommendation,
        "recommendation_text": recommendation_text,
        "confidence": confidence,
        "available_weight": available_weight,
        "components": {name: {"weight": weight, "risk": round(risk, 3) if risk is not None else None} for name, weight, risk in components},
        "hard_override": hard_reasons,
    }


def fmt_int(value: Any) -> str:
    return "--" if value is None else f"{int(value):,}"


def fmt_ms(value: Any) -> str:
    if value is None:
        return "--"
    value = float(value)
    if value >= 60_000:
        return f"{value / 60_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}s"
    return f"{value:.0f}ms"


def fmt_pct(value: Any) -> str:
    return "--" if value is None else f"{float(value) * 100:.1f}%"


def arrow(direction: str) -> str:
    return {"up": "↑", "down": "↓", "flat": "→", "unknown": "?"}.get(direction, "?")


def build_report(data: RolloutData, state: StateData, logs: LogData, args: argparse.Namespace, session_id: Optional[str]) -> Dict[str, Any]:
    choose_segment(data, session_id or (data.session_metas[0].session_id if data.session_metas else None))
    turns = active_turns(data)
    calls, unmatched_timing = enrich_calls(active_calls(data), logs)
    snapshots = context_snapshots(data, logs)
    auto_limit = None
    for item in snapshots:
        if item.get("source", "").startswith("logs.") and item.get("window"):
            auto_limit = item["window"]
    compactions = compact_metrics(data, turns, snapshots, auto_limit)
    context = context_metrics(data, turns, snapshots, compactions)
    turn_reports: List[Dict[str, Any]] = []
    for turn in turns:
        token_info = turn_token_values(turn, data)
        token_values = token_info.get("values", {})
        tool_info = tool_metrics_for_turn(turn, calls, logs)
        output_tokens = token_values.get("output_tokens")
        duration_s = (turn.duration_ms or 0.0) / 1000.0 if turn.duration_ms is not None else None
        output_rate = output_tokens / duration_s if output_tokens is not None and duration_s and duration_s > 0 else None
        active_duration = None
        if duration_s is not None and tool_info.get("tool_wall_ms_approx") is not None:
            active_duration = max(0.001, duration_s - tool_info["tool_wall_ms_approx"] / 1000.0 - tool_info.get("retry", {}).get("backoff_ms", 0.0) / 1000.0)
        active_rate = output_tokens / active_duration if output_tokens is not None and active_duration else None
        elapsed_ms = round(max(0.0, time.time() - turn.start_ts) * 1000.0, 1) if turn.start_ts is not None and not turn.completed else None
        turn_reports.append(
            {
                "turn_id": turn.turn_id,
                "started_at": iso_time(turn.start_ts),
                "completed_at": iso_time(turn.complete_ts),
                "in_flight": not turn.completed,
                "aborted": turn.aborted,
                "duration_ms": round(turn.duration_ms, 1) if turn.duration_ms is not None else None,
                "elapsed_ms": elapsed_ms,
                "duration_exact": turn.duration_exact,
                "ttft_ms": round(turn.ttft_ms, 1) if turn.ttft_ms is not None else None,
                "ttft_exact": turn.ttft_exact,
                "tokens": {**token_values, "method": token_info.get("method"), "confidence": token_info.get("confidence")},
                "agent": {"output_rate_tps": round(output_rate, 3) if output_rate is not None else None, "active_output_rate_tps": round(active_rate, 3) if active_rate is not None else None, "model_generation_tps": None},
                "tools": tool_info,
                "context": context_for_turn(turn, snapshots),
                "reasoning_events": len(turn.reasoning_timestamps),
                "reasoning_span_ms": round((max(turn.reasoning_timestamps) - min(turn.reasoning_timestamps)) * 1000.0, 1) if len(turn.reasoning_timestamps) >= 2 else None,
                "final_message_at": iso_time(max(turn.message_timestamps)) if turn.message_timestamps else None,
            }
        )
    recent = turn_reports[-max(1, args.last):]
    recent_completed = [item for item in recent if not item["in_flight"] and not item["aborted"]]
    trends = {
        "ttft": trend([item.get("ttft_ms") for item in recent_completed], 1000.0),
        "duration": trend([item.get("duration_ms") for item in recent_completed], 5000.0),
        "tool_p95": trend([item.get("tools", {}).get("p95_ms") for item in recent_completed], 250.0),
        "context_growth": context.get("growth_trend", {"direction": "unknown", "worsening": False}),
        "agent_output_rate": trend([item.get("agent", {}).get("output_rate_tps") for item in recent_completed], 1.0),
    }
    recent_completed_ids = {item["turn_id"] for item in recent_completed}
    recent_calls = [call for call in calls if call.turn_id in recent_completed_ids]
    all_values = [call.total_ms for call in calls if call.total_ms is not None]
    recent_values = [call.total_ms for call in recent_calls if call.total_ms is not None]
    recent_timed_calls = [call for call in recent_calls if call.total_ms is not None]
    recent_longest = max(recent_timed_calls, key=lambda call: call.total_ms or -1, default=None)
    duplicate_counts = Counter((call.category, call.name, call.signature) for call in calls)
    duplicate_excess = sum(max(0, count - 1) for count in duplicate_counts.values())
    recent_failed = sum(1 for call in recent_calls if call.failed)
    recent_start = min((parse_time(item["started_at"]) for item in recent_completed if item.get("started_at")), default=None)
    recent_end = max((parse_time(item["completed_at"]) for item in recent_completed if item.get("completed_at")), default=None)
    recent_retry_count = sum(
        1
        for retry in logs.retries
        if recent_start is not None and retry.timestamp >= recent_start and (recent_end is None or retry.timestamp <= recent_end)
    )
    tools = {
        "calls": len(calls),
        "timed_calls": sum(1 for call in calls if call.total_ms is not None),
        "timing_coverage": (sum(1 for call in calls if call.total_ms is not None) / len(calls)) if calls else None,
        "median_ms": round(median(all_values), 1) if all_values else None,
        "p95_ms": round(percentile(all_values, 0.95), 1) if all_values else None,
        "max_ms": round(max(all_values), 1) if all_values else None,
        "sum_total_ms": round(sum(all_values), 1),
        "recent_sum_total_ms": round(sum(recent_values), 1),
        "recent_sum_handler_ms": round(sum(call.handler_ms or 0.0 for call in recent_timed_calls), 1),
        "recent_median_ms": round(median(recent_values), 1) if recent_values else None,
        "recent_p95_ms": round(percentile(recent_values, 0.95), 1) if recent_values else None,
        "recent_timed_calls": len(recent_timed_calls),
        "recent_timing_coverage": len(recent_timed_calls) / len(recent_calls) if recent_calls else None,
        "recent_longest": {"name": recent_longest.name, "category": recent_longest.category, "total_ms": round(recent_longest.total_ms or 0.0, 1), "source": recent_longest.timing_source} if recent_longest else None,
        "longest": (lambda longest: {"name": longest.name, "category": longest.category, "total_ms": round(longest.total_ms or 0.0, 1), "source": longest.timing_source} if longest else None)(max((call for call in calls if call.total_ms is not None), key=lambda call: call.total_ms or -1, default=None)),
        "failed": sum(1 for call in calls if call.failed),
        "failure_rate": sum(1 for call in calls if call.failed) / len(calls) if calls else None,
        "duplicate_excess": duplicate_excess,
        "duplicate_ratio": duplicate_excess / len(calls) if calls else None,
        "output_bytes": sum(call.output_bytes for call in calls),
        "top_output": (lambda top: {"name": top.name, "category": top.category, "bytes": top.output_bytes} if top and top.output_bytes else None)(max(calls, key=lambda call: call.output_bytes, default=None)),
        "recent_calls": len(recent_calls),
        "recent_failure_rate": recent_failed / len(recent_calls) if recent_calls else None,
        "recent_retry_count": recent_retry_count,
        "retry_count": len(logs.retries),
        "retry_rate": len(logs.retries) / len(calls) if calls else None,
        "retry_backoff_ms": round(sum(item.backoff_ms for item in logs.retries), 1),
        "unmatched_log_timings": unmatched_timing,
        "by_name": {},
    }
    for call in calls:
        item = tools["by_name"].setdefault(call.name, {"calls": 0, "output_bytes": 0, "total_ms": 0.0, "failed": 0})
        item["calls"] += 1
        item["output_bytes"] += call.output_bytes
        item["total_ms"] += call.total_ms or 0.0
        item["failed"] += int(call.failed)
    for item in tools["by_name"].values():
        item["total_ms"] = round(item["total_ms"], 1)

    recent_output_values = [item["tokens"].get("output_tokens") for item in recent_completed if item["tokens"].get("output_tokens") is not None]
    recent_output_tokens = sum(recent_output_values) if recent_output_values else None
    recent_duration_ms = sum(item["duration_ms"] for item in recent_completed if item.get("duration_ms") is not None)
    recent_wall_rate = recent_output_tokens / (recent_duration_ms / 1000.0) if recent_output_tokens is not None and recent_duration_ms > 0 else None
    residual_ms = 0.0
    residual_output_tokens = 0
    residual_turns = 0
    for item in recent_completed:
        duration = item.get("duration_ms")
        tool_wall = item.get("tools", {}).get("tool_wall_ms_approx")
        retry_backoff = item.get("tools", {}).get("retry", {}).get("backoff_ms", 0.0)
        output_tokens = item["tokens"].get("output_tokens")
        if duration is None or tool_wall is None or output_tokens is None:
            continue
        residual_ms += max(0.0, duration - tool_wall - retry_backoff)
        residual_output_tokens += output_tokens
        residual_turns += 1
    recent_active_rate = residual_output_tokens / (residual_ms / 1000.0) if residual_turns and residual_ms > 0 else None
    agent_confidences = [item["tokens"].get("confidence") for item in recent_completed if item["tokens"].get("confidence")]
    agent_confidence = "high" if agent_confidences and all(value == "high" for value in agent_confidences) else "medium" if agent_confidences else "low"
    agent_throughput = {
        "recent_completed_turns": len(recent_completed),
        "output_tokens_approx": recent_output_tokens,
        "wall_tps_approx": round(recent_wall_rate, 3) if recent_wall_rate is not None else None,
        "active_tps_approx": round(recent_active_rate, 3) if recent_active_rate is not None else None,
        "active_rate_turns": residual_turns,
        "turns_per_min": round(len(recent_completed) / (recent_duration_ms / 60_000.0), 3) if recent_duration_ms > 0 else None,
        "confidence": agent_confidence,
        "warning": "这些是 agent-level output rates，不是 model generation tok/s",
    }
    model_generation_estimate = {
        "tok_s": None,
        "estimate_tps": round(recent_active_rate, 3) if recent_active_rate is not None else None,
        "estimate_status": "residual_effective_rate_approx" if recent_active_rate is not None else "unavailable",
        "estimate_confidence": "low" if recent_active_rate is not None else "none",
        "reason": "缺少服务端 generation start/end 和逐 token timestamps；残差仍包含 reasoning/network/post-processing",
    }

    reasoning_spans = [item["reasoning_span_ms"] for item in recent_completed if item.get("reasoning_span_ms") is not None and item.get("tools", {}).get("calls", 0) == 0]
    reasoning_event_count = sum(item.get("reasoning_events", 0) for item in recent_completed)
    final_tails: List[float] = []
    tool_wall_values = [item.get("tools", {}).get("tool_wall_ms_approx") for item in recent_completed if item.get("tools", {}).get("tool_wall_ms_approx") is not None]
    for turn in turns:
        if turn.completed and turn.complete_ts is not None and turn.message_timestamps:
            final_tails.append(max(0.0, (turn.complete_ts - max(turn.message_timestamps)) * 1000.0))
    phase_estimates = {
        "model_wait": {
            "ttft_ms": round(median([item["ttft_ms"] for item in recent_completed if item.get("ttft_ms") is not None]), 1) if any(item.get("ttft_ms") is not None for item in recent_completed) else None,
            "status": "exact_runtime_metric",
            "subphase_breakdown": "unavailable_without_queue_network_server_timestamps",
        },
        "reasoning": {
            "event_span_ms_median": round(median(reasoning_spans), 1) if reasoning_spans else None,
            "event_count": reasoning_event_count,
            "isolated_turns": len(reasoning_spans),
            "status": "approximate_event_span",
            "warning": "only turns without interleaved tools are shown; event span is not hidden reasoning compute time",
        },
        "tool_execution": {
            "sum_total_ms": tools.get("sum_total_ms"),
            "wall_ms_approx_median_per_turn": round(median(tool_wall_values), 1) if tool_wall_values else None,
            "timing_coverage": tools.get("timing_coverage"),
            "status": "exact_per_call_when_log_matched",
            "parallel_overlap_possible": any(item.get("tools", {}).get("parallel_overlap_possible") for item in recent_completed),
        },
        "final_response": {
            "tail_ms_approx_median": round(median(final_tails), 1) if final_tails else None,
            "status": "approximate_post_message_tail",
            "warning": "no persisted final-token event or server completion duration",
        },
    }

    latest = latest_usage(data.token_samples)
    state_tokens = integer(state.row.get("tokens_used")) if state.row else None
    token_confidences = [item["tokens"].get("confidence") for item in turn_reports if item["tokens"].get("confidence")]
    token_confidence = "high" if token_confidences and all(value == "high" for value in token_confidences) else "medium" if token_confidences else "low"
    current_context_window = context.get("context_window")
    data_quality = {
        "rollout_available": True,
        "raw_lines": data.raw_lines,
        "malformed_lines": data.malformed_lines,
        "missing_timestamps": data.missing_timestamps,
        "unknown_records": data.unknown_records,
        "unknown_event_types": dict(data.unknown_event_types),
        "file_changed_during_read": data.file_changed_during_read,
        "partial_last_line": data.partial_last_line,
        "segment_start_line": data.segment_start_line,
        "inherited_compactions": compactions.get("inherited_count", 0),
        "completed_turns": sum(1 for turn in turns if turn.completed),
        "started_turns": len(turns),
        "completed_turn_coverage": sum(1 for turn in turns if turn.completed) / len(turns) if turns else 0.0,
        "token_samples": len(data.token_samples),
        "active_token_samples": len(active_token_samples(data)),
        "token_per_turn_attribution": token_confidence,
        "logs_rows": logs.rows,
        "logs_truncated": logs.truncated,
        "logs_retention_cap": logs.retention_cap,
        "logs_db_paths": [str(path) for path in logs.db_paths],
        "log_errors": logs.errors,
        "tool_timing_coverage": tools.get("timing_coverage") or 0.0,
        "unmatched_tool_outputs": data.output_only_calls,
        "unmatched_log_timings": unmatched_timing,
        "state_db_path": str(state.db_path) if state.db_path else None,
        "state_row_available": bool(state.row),
        "state_error": state.error,
        "state_tokens_used": state_tokens,
        "rollout_cumulative_total_tokens": latest.get("total_tokens"),
        "state_rollout_token_delta": (state_tokens - latest.get("total_tokens")) if state_tokens is not None and latest.get("total_tokens") is not None else None,
        "context_window": current_context_window,
    }
    score = score_report(turns, recent_completed, trends, tools, context, compactions, token_confidence, data_quality)
    risks: List[str] = []
    if trends["ttft"].get("worsening"):
        risks.append("最近 TTFT 持续上升")
    if trends["duration"].get("worsening"):
        risks.append("最近 turn duration 持续上升")
    if trends["tool_p95"].get("worsening"):
        risks.append("工具 p95 latency 上升")
    if tools.get("failure_rate") is not None and tools["failure_rate"] >= 0.20:
        risks.append("工具失败/超时比例偏高")
    if tools.get("duplicate_ratio") is not None and tools["duplicate_ratio"] >= 0.30:
        risks.append("重复工具调用较多")
    if context.get("auto_compact_occupancy") is not None and context["auto_compact_occupancy"] >= 0.75:
        risks.append("context 接近 auto-compaction limit")
    if compactions.get("short_interval_count", 0):
        risks.append("存在短间隔 compaction")
    if compactions.get("rapid_refill_count", 0):
        risks.append("compaction 后 context 快速回涨")
    if context.get("snapshot_predates_latest_compaction"):
        risks.append("最新 context snapshot 早于最近 compaction")
    if data_quality["logs_truncated"]:
        risks.append("logs 达到保留上限，工具统计可能不完整")
    if not risks:
        risks.append("未发现明确的持续性性能风险")
    report = {
        "schema_version": 1,
        "generated_at": iso_time(time.time()),
        "session": {
            "thread_id": session_id,
            "rollout_path": str(data.path),
            "rollout_basename": data.path.name,
            "started_at": iso_time(data.first_timestamp),
            "last_event_at": iso_time(data.last_timestamp),
            "state": state.row,
        },
        **score,
        "current_turn": turn_reports[-1] if turn_reports else None,
        "turns": turn_reports,
        "trends": trends,
        "tokens": {
            "latest_cumulative": latest,
            "model_context_window": current_context_window,
            "cache_hit_rate": (latest.get("cached_input_tokens") / latest.get("input_tokens")) if latest.get("input_tokens") else None,
            "state_tokens_used": state_tokens,
        },
        "context": context,
        "compactions": compactions,
        "tools": tools,
        "tool_execution": {
            "session_sum_total_ms": tools.get("sum_total_ms"),
            "session_sum_handler_ms": round(sum(call.handler_ms or 0.0 for call in calls if call.total_ms is not None), 1),
            "recent_completed_sum_total_ms": tools.get("recent_sum_total_ms"),
            "recent_completed_sum_handler_ms": tools.get("recent_sum_handler_ms"),
            "recent_completed_p95_ms": tools.get("recent_p95_ms"),
            "timing_coverage": tools.get("timing_coverage"),
            "parallel_overlap_possible": any(item.get("tools", {}).get("parallel_overlap_possible") for item in recent_completed),
        },
        "agent_throughput": agent_throughput,
        "phase_estimates": phase_estimates,
        "model_generation": model_generation_estimate,
        "data_quality": data_quality,
        "risks": risks,
    }
    return report


def print_human(report: Dict[str, Any], last: int) -> None:
    print("Session Performance")
    print(f"Health: {report['health_score']:.1f}/100  {report['status']}")
    print(f"Recommendation: {report['recommendation_text']}")
    print(f"Confidence: {report['confidence']}")
    session = report.get("session", {})
    print(f"Session: {session.get('thread_id') or '--'} | {session.get('rollout_basename') or '--'}")
    current = report.get("current_turn")
    if current:
        tokens = current.get("tokens", {})
        tools = current.get("tools", {})
        agent = current.get("agent", {})
        duration_display = current.get("duration_ms") if current.get("duration_ms") is not None else current.get("elapsed_ms")
        duration_label = "exact" if current.get("duration_exact") else "active/approx"
        aggregate_agent = report.get("agent_throughput", {})
        current_rate = agent.get("output_rate_tps")
        agent_rate_text = f"current {current_rate} tok/s approx" if current_rate is not None else f"recent {aggregate_agent.get('wall_tps_approx') or '--'} tok/s approx, output~{fmt_int(aggregate_agent.get('output_tokens_approx'))} ({aggregate_agent.get('recent_completed_turns', 0)} completed turns)"
        model_generation = report.get("model_generation", {})
        model_estimate = model_generation.get("estimate_tps")
        model_text = "N/A exact" if model_estimate is None else f"N/A exact (residual estimate {model_estimate} tok/s, low confidence)"
        print("\nLast turn")
        print(f"TTFT: {fmt_ms(current.get('ttft_ms'))} {'exact' if current.get('ttft_exact') else 'approx'} | Turn duration: {fmt_ms(duration_display)} {duration_label}")
        print(f"Model generation tok/s: {model_text} | Agent output throughput: {agent_rate_text}")
        print(f"Tools (current): {tools.get('calls', 0)} calls | exec sum {fmt_ms(tools.get('sum_total_ms'))} | handler {fmt_ms(tools.get('sum_handler_ms'))} | p95 {fmt_ms(tools.get('p95_ms'))} | longest {(tools.get('longest') or {}).get('name', '--')} {fmt_ms((tools.get('longest') or {}).get('total_ms'))}")
        print(f"Tool execution (recent completed): sum {fmt_ms(report.get('tool_execution', {}).get('recent_completed_sum_total_ms'))} | handler {fmt_ms(report.get('tool_execution', {}).get('recent_completed_sum_handler_ms'))} | p95 {fmt_ms(report.get('tool_execution', {}).get('recent_completed_p95_ms'))}")
        print(f"Repeated calls: {fmt_pct(tools.get('duplicate_ratio'))} | failures: {fmt_pct(tools.get('failure_rate'))}")
        phases = report.get("phase_estimates", {})
        print(f"Phases: model wait {fmt_ms((phases.get('model_wait') or {}).get('ttft_ms'))} exact | reasoning {(phases.get('reasoning') or {}).get('event_span_ms_median') or '--'}ms approx | tool wall {(phases.get('tool_execution') or {}).get('wall_ms_approx_median_per_turn') or '--'}ms approx | final tail {(phases.get('final_response') or {}).get('tail_ms_approx_median') or '--'}ms approx")
    context = report.get("context", {})
    print("\nContext")
    print(f"Estimated: {fmt_int(context.get('current_tokens'))} / {fmt_int(context.get('context_window'))} | occupancy {fmt_pct(context.get('occupancy'))} | auto-limit {fmt_pct(context.get('auto_compact_occupancy'))}")
    print(f"Growth: {fmt_int(context.get('growth_tokens_per_min'))} tokens/min | Compactions: {report.get('compactions', {}).get('unique_count', 0)} unique | interval {report.get('compactions', {}).get('median_interval_minutes') or '--'} min")
    tokens_report = report.get("tokens", {})
    latest = tokens_report.get("latest_cumulative", {})
    print("\nTokens")
    print(f"Cumulative input {fmt_int(latest.get('input_tokens'))} | Cached input {fmt_int(latest.get('cached_input_tokens'))} | Cache hit {fmt_pct(tokens_report.get('cache_hit_rate'))} | Output {fmt_int(latest.get('output_tokens'))} | Reasoning {fmt_int(latest.get('reasoning_output_tokens'))}")
    trends = report.get("trends", {})
    print(f"\nTrend (last {last} completed turns)")
    print(f"TTFT {arrow(trends.get('ttft', {}).get('direction'))} {fmt_pct((trends.get('ttft', {}).get('ratio') or 0) - 1 if trends.get('ttft', {}).get('ratio') is not None else None)} | duration {arrow(trends.get('duration', {}).get('direction'))} {fmt_pct((trends.get('duration', {}).get('ratio') or 0) - 1 if trends.get('duration', {}).get('ratio') is not None else None)} | tool p95 {arrow(trends.get('tool_p95', {}).get('direction'))} | context growth {arrow(trends.get('context_growth', {}).get('direction'))}")
    print("\nMain risks")
    for risk in report.get("risks", [])[:5]:
        print(f"- {risk}")
    quality = report.get("data_quality", {})
    print(f"\nData: rollout lines {quality.get('raw_lines', 0)} | completed {quality.get('completed_turns', 0)}/{quality.get('started_turns', 0)} turns | logs {quality.get('logs_rows', 0)}{(' (retention cap)' if quality.get('logs_truncated') else '')}")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Codex session performance monitor")
    parser.add_argument("--thread", "--session", dest="thread", help="thread ID or rollout JSONL path")
    parser.add_argument("--sessions-dir", help="override ~/.codex/sessions")
    parser.add_argument("--codex-home", help="override CODEX_HOME (default: ~/.codex)")
    parser.add_argument("--last", type=int, default=10, help="number of completed turns for trend analysis (default: 10)")
    parser.add_argument("--log-cap", type=int, default=1000, help="retention watermark used for logs completeness (default: 1000)")
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    args.last = max(2, min(args.last, 1000))
    args.log_cap = max(1, args.log_cap)
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    try:
        path, session_id, state = resolve_session(args, codex_home)
        if not session_id and state.row.get("id"):
            session_id = str(state.row["id"])
        data = parse_rollout(path)
        logs = read_logs(codex_home, session_id, args.log_cap)
        report = build_report(data, state, logs, args, session_id)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        else:
            print_human(report, args.last)
        return 0
    except RuntimeError as exc:
        print(f"session-performance: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"session-performance: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
