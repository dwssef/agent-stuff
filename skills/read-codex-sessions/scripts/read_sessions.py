#!/usr/bin/env python3
"""Read user questions and final assistant answers from local Codex rollouts."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence, TextIO


UUID_PATTERN = re.compile(
    r"(?<![0-9A-Za-z_-])"
    r"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
    r"(?![0-9A-Za-z_-])"
)
ROLLOUT_ID_PATTERN = re.compile(
    r"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\.jsonl$"
)
DEFAULT_MAX_MESSAGES = 80
DEFAULT_MAX_CHARS = 30_000


@dataclass(frozen=True)
class TranscriptMessage:
    role: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "text": self.text}


@dataclass
class TranscriptTurn:
    messages: list[TranscriptMessage]

    @property
    def char_count(self) -> int:
        return sum(len(message.text) for message in self.messages)


@dataclass
class TurnDraft:
    started_by_task: bool = False
    event_user: Optional[str] = None
    response_users: list[str] = field(default_factory=list)
    final_assistant: Optional[str] = None
    task_complete_assistant: Optional[str] = None
    assistant_candidates: list[tuple[Optional[str], str]] = field(default_factory=list)

    def has_evidence(self) -> bool:
        return any(
            (
                self.event_user is not None,
                bool(self.response_users),
                self.final_assistant is not None,
                self.task_complete_assistant is not None,
                bool(self.assistant_candidates),
            )
        )

    def has_assistant_evidence(self) -> bool:
        return any(
            (
                self.final_assistant is not None,
                self.task_complete_assistant is not None,
                bool(self.assistant_candidates),
            )
        )


@dataclass(frozen=True)
class SessionFile:
    path: Path
    source: str


@dataclass
class SessionResult:
    session_id: str
    source: str
    messages: list[TranscriptMessage]
    available_messages: list[TranscriptMessage]
    available_turns: int
    shown_turns: int
    truncated: bool
    warnings: list[str]

    @staticmethod
    def _role_counts(messages: Iterable[TranscriptMessage]) -> dict[str, int]:
        counts = {"user": 0, "assistant": 0}
        for message in messages:
            if message.role in counts:
                counts[message.role] += 1
        return counts

    def to_dict(self) -> dict[str, object]:
        available = self._role_counts(self.available_messages)
        shown = self._role_counts(self.messages)
        return {
            "session_id": self.session_id,
            "source": self.source,
            "messages": [message.to_dict() for message in self.messages],
            "counts": {
                "available_user": available["user"],
                "available_assistant": available["assistant"],
                "available_messages": len(self.available_messages),
                "available_turns": self.available_turns,
                "shown_user": shown["user"],
                "shown_assistant": shown["assistant"],
                "shown_messages": len(self.messages),
                "shown_turns": self.shown_turns,
            },
            "truncated": self.truncated,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class SessionFailure:
    session_id: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return {"session_id": self.session_id, "error": self.error}


def append_warning(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def extract_session_ids(raw_input: str) -> list[str]:
    """Extract complete UUIDs, preserving first-seen order."""
    found: list[str] = []
    seen: set[str] = set()
    for match in UUID_PATTERN.finditer(raw_input):
        session_id = match.group(1).lower()
        if session_id not in seen:
            seen.add(session_id)
            found.append(session_id)
    return found


def extract_content_text(payload: dict[str, object]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            texts.append(text)
    return "\n\n".join(texts)


def nonempty(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value != "" else None


def finalize_draft(
    draft: Optional[TurnDraft], warnings: list[str]
) -> Optional[TranscriptTurn]:
    if draft is None or not draft.has_evidence():
        return None

    user_text = nonempty(draft.event_user)
    if user_text is None:
        user_text = next(
            (text for text in reversed(draft.response_users) if text != ""), None
        )
        if user_text is not None:
            append_warning(
                warnings,
                "Schema fallback used: response_item user message substituted for missing event_msg.user_message.",
            )

    assistant_text = nonempty(draft.final_assistant)
    if assistant_text is None:
        assistant_text = nonempty(draft.task_complete_assistant)
        if assistant_text is not None:
            append_warning(
                warnings,
                "Schema fallback used: task_complete.last_agent_message substituted for missing final_answer.",
            )
    if assistant_text is None:
        assistant_text = next(
            (
                text
                for _phase, text in reversed(draft.assistant_candidates)
                if text != ""
            ),
            None,
        )
        if assistant_text is not None:
            append_warning(
                warnings,
                "Schema fallback used: the last assistant message in a turn substituted for missing final_answer.",
            )

    messages: list[TranscriptMessage] = []
    if user_text is not None:
        messages.append(TranscriptMessage("user", user_text))
    if assistant_text is not None:
        messages.append(TranscriptMessage("assistant", assistant_text))
    return TranscriptTurn(messages) if messages else None


def parse_rollout(path: Path) -> tuple[list[TranscriptTurn], list[str]]:
    """Parse a rollout while excluding runtime and reasoning records."""
    turns: list[TranscriptTurn] = []
    warnings: list[str] = []
    malformed_lines = 0
    saw_transcript_evidence = False
    draft: Optional[TurnDraft] = None

    def ensure_draft(started_by_task: bool = False) -> TurnDraft:
        nonlocal draft
        if draft is None:
            draft = TurnDraft(started_by_task=started_by_task)
        elif started_by_task:
            draft.started_by_task = True
        return draft

    def flush() -> None:
        nonlocal draft
        turn = finalize_draft(draft, warnings)
        if turn is not None:
            turns.append(turn)
        draft = None

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"cannot open session file: {exc}") from exc

    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            record_type = record.get("type")
            payload_type = payload.get("type")

            if record_type == "event_msg" and payload_type == "task_started":
                saw_transcript_evidence = True
                flush()
                draft = TurnDraft(started_by_task=True)
                continue

            if record_type == "event_msg" and payload_type == "user_message":
                saw_transcript_evidence = True
                current = ensure_draft()
                if current.has_assistant_evidence() and not current.started_by_task:
                    flush()
                    current = ensure_draft()
                message = payload.get("message")
                current.event_user = message if isinstance(message, str) else ""
                continue

            if record_type == "response_item" and payload_type == "message":
                role = payload.get("role")
                text = extract_content_text(payload)
                if role == "user":
                    saw_transcript_evidence = True
                    current = ensure_draft()
                    if current.has_assistant_evidence() and not current.started_by_task:
                        flush()
                        current = ensure_draft()
                    current.response_users.append(text)
                elif role == "assistant":
                    saw_transcript_evidence = True
                    current = ensure_draft()
                    phase = payload.get("phase")
                    phase_value = phase if isinstance(phase, str) else None
                    current.assistant_candidates.append((phase_value, text))
                    if phase_value == "final_answer":
                        current.final_assistant = text
                continue

            if record_type == "event_msg" and payload_type == "task_complete":
                saw_transcript_evidence = True
                current = ensure_draft()
                message = payload.get("last_agent_message")
                current.task_complete_assistant = (
                    message if isinstance(message, str) else ""
                )
                flush()
                continue

            if record_type == "event_msg" and payload_type == "turn_aborted":
                saw_transcript_evidence = True
                flush()

    flush()
    if malformed_lines and not saw_transcript_evidence:
        raise RuntimeError(
            "session transcript could not be parsed: all transcript records were malformed"
        )
    if malformed_lines:
        append_warning(
            warnings,
            f"Skipped {malformed_lines} malformed JSONL line(s) while parsing.",
        )
    return turns, warnings


def first_session_meta_id(path: Path) -> tuple[Optional[str], Optional[str]]:
    malformed = 0
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"cannot open session file: {exc}"
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(record, dict) or record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                return None, "first session_meta payload is not an object"
            session_id = payload.get("id")
            if not isinstance(session_id, str):
                return None, "first session_meta.id is missing"
            return session_id.lower(), None
    suffix = f" after skipping {malformed} malformed line(s)" if malformed else ""
    return None, f"session_meta record not found{suffix}"


def discover_session_files(
    codex_home: Path,
) -> tuple[dict[str, list[SessionFile]], dict[str, list[str]]]:
    discovered: dict[str, list[SessionFile]] = {}
    invalid: dict[str, list[str]] = {}
    roots = (
        (codex_home / "sessions", "active"),
        (codex_home / "archived_sessions", "archived"),
    )
    for root, source in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            match = ROLLOUT_ID_PATTERN.search(path.name)
            if match is None:
                continue
            filename_id = match.group(1).lower()
            metadata_id, error = first_session_meta_id(path)
            if error is not None:
                invalid.setdefault(filename_id, []).append(f"{path}: {error}")
                continue
            if metadata_id != filename_id:
                invalid.setdefault(filename_id, []).append(
                    f"{path}: filename UUID does not match first session_meta.id"
                )
                continue
            discovered.setdefault(filename_id, []).append(SessionFile(path, source))
    return discovered, invalid


def apply_limits(
    turns: list[TranscriptTurn],
    max_messages: int,
    max_chars: int,
    last: Optional[int],
    warnings: list[str],
) -> tuple[list[TranscriptTurn], bool]:
    candidates = turns[-last:] if last is not None else list(turns)
    selected_reversed: list[TranscriptTurn] = []
    message_count = 0
    char_count = 0

    for turn in reversed(candidates):
        next_messages = len(turn.messages)
        next_chars = turn.char_count
        exceeds = (
            message_count + next_messages > max_messages
            or char_count + next_chars > max_chars
        )
        if exceeds and selected_reversed:
            break
        if exceeds:
            append_warning(
                warnings,
                "The most recent complete turn exceeds a configured context limit; retained it intact.",
            )
        selected_reversed.append(turn)
        message_count += next_messages
        char_count += next_chars
        if exceeds:
            break

    selected = list(reversed(selected_reversed))
    truncated = len(selected) < len(turns)
    if truncated:
        shown_messages = sum(len(turn.messages) for turn in selected)
        available_messages = sum(len(turn.messages) for turn in turns)
        append_warning(
            warnings,
            "Session truncated: "
            f"showing {shown_messages} of {available_messages} message(s) "
            f"from {len(selected)} of {len(turns)} turn(s).",
        )
    return selected, truncated


def flatten(turns: Iterable[TranscriptTurn]) -> list[TranscriptMessage]:
    return [message for turn in turns for message in turn.messages]


def read_requested_sessions(
    session_ids: Sequence[str],
    codex_home: Path,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    max_chars: int = DEFAULT_MAX_CHARS,
    last: Optional[int] = None,
) -> tuple[list[SessionResult], list[SessionFailure]]:
    discovered, invalid = discover_session_files(codex_home)
    results: list[SessionResult] = []
    failures: list[SessionFailure] = []

    for session_id in session_ids:
        matches = discovered.get(session_id, [])
        if not matches:
            invalid_details = invalid.get(session_id)
            error = (
                "invalid session file metadata: " + "; ".join(invalid_details)
                if invalid_details
                else "session not found"
            )
            failures.append(SessionFailure(session_id, error))
            continue
        if len(matches) > 1:
            failures.append(
                SessionFailure(
                    session_id,
                    f"ambiguous session ID: found {len(matches)} exact rollout files",
                )
            )
            continue
        session_file = matches[0]
        try:
            turns, warnings = parse_rollout(session_file.path)
        except RuntimeError as exc:
            failures.append(SessionFailure(session_id, str(exc)))
            continue
        available_messages = flatten(turns)
        selected_turns, truncated = apply_limits(
            turns, max_messages, max_chars, last, warnings
        )
        results.append(
            SessionResult(
                session_id=session_id,
                source=session_file.source,
                messages=flatten(selected_turns),
                available_messages=available_messages,
                available_turns=len(turns),
                shown_turns=len(selected_turns),
                truncated=truncated,
                warnings=warnings,
            )
        )
    return results, failures


def render_text(
    sessions: Sequence[SessionResult], failures: Sequence[SessionFailure]
) -> str:
    sections: list[str] = []
    for session in sessions:
        lines = [
            f"===== CODEX SESSION: {session.session_id} =====",
            f"[source: {session.source}]",
            "",
        ]
        if session.messages:
            for message in session.messages:
                lines.extend((f"[{message.role}]", message.text, ""))
        else:
            lines.extend(("[No user/final-assistant transcript messages found.]", ""))
        if session.warnings:
            lines.append("[warnings]")
            lines.extend(f"- {warning}" for warning in session.warnings)
            lines.append("")
        lines.append(f"===== END SESSION: {session.session_id} =====")
        sections.append("\n".join(lines))

    summary: list[str] = []
    if sessions:
        summary.append("Successfully read:")
        for session in sessions:
            counts = SessionResult._role_counts(session.available_messages)
            shown = SessionResult._role_counts(session.messages)
            display = f"- {session.session_id}: {counts['user']} user / {counts['assistant']} assistant"
            if session.truncated:
                display += (
                    f" (shown {shown['user']} user / {shown['assistant']} assistant)"
                )
            summary.append(display)
    if failures:
        if summary:
            summary.append("")
        summary.append("Failed:")
        summary.extend(f"- {failure.session_id}: {failure.error}" for failure in failures)
    if summary:
        sections.append("\n".join(summary))
    return "\n\n".join(sections) + ("\n" if sections else "")


def render_json(
    sessions: Sequence[SessionResult], failures: Sequence[SessionFailure]
) -> str:
    payload = {
        "sessions": [session.to_dict() for session in sessions],
        "failures": [failure.to_dict() for failure in failures],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read exact local Codex session Q&A without modifying sessions."
    )
    parser.add_argument("session_input", nargs="*", help="Session UUIDs or text containing them")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument(
        "--max-messages-per-session",
        type=positive_integer,
        default=DEFAULT_MAX_MESSAGES,
    )
    parser.add_argument(
        "--max-chars-per-session", type=positive_integer, default=DEFAULT_MAX_CHARS
    )
    parser.add_argument("--last", type=positive_integer)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Unsupported in this version; retained for an explicit error.",
    )
    return parser


def fatal_error(message: str, output_format: str, stdout: TextIO, stderr: TextIO) -> int:
    if output_format == "json":
        json.dump(
            {"sessions": [], "failures": [], "error": message},
            stdout,
            ensure_ascii=False,
            indent=2,
        )
        stdout.write("\n")
    else:
        stderr.write(f"error: {message}\n")
    return 2


def main(
    argv: Optional[Sequence[str]] = None,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.full:
        return fatal_error(
            "--full is not supported in this version; only user and final assistant messages are available",
            args.format,
            stdout,
            stderr,
        )

    raw_input = " ".join(args.session_input) if args.session_input else stdin.read()
    session_ids = extract_session_ids(raw_input)
    if not session_ids:
        return fatal_error(
            "no complete Codex session UUIDs were found",
            args.format,
            stdout,
            stderr,
        )

    configured_home = args.codex_home or Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    )
    sessions, failures = read_requested_sessions(
        session_ids,
        configured_home.expanduser(),
        max_messages=args.max_messages_per_session,
        max_chars=args.max_chars_per_session,
        last=args.last,
    )
    if args.format == "json":
        stdout.write(render_json(sessions, failures))
    else:
        stdout.write(render_text(sessions, failures))
    return 0 if sessions else 1


if __name__ == "__main__":
    raise SystemExit(main())
