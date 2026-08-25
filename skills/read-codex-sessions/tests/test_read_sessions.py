import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "read_sessions.py"
SPEC = importlib.util.spec_from_file_location("read_sessions", SCRIPT)
reader = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = reader
SPEC.loader.exec_module(reader)


ID_A = "019abcde-1234-7abc-8def-1234567890ab"
ID_B = "019defab-5678-7def-8abc-abcdef123456"
ID_C = "01901234-abcd-7123-8abc-0123456789ab"


def record(record_type, payload):
    return {"timestamp": "2026-01-01T00:00:00Z", "type": record_type, "payload": payload}


def session_meta(session_id):
    return record("session_meta", {"id": session_id, "session_id": session_id})


def response_message(role, text, phase=None):
    item_type = "output_text" if role == "assistant" else "input_text"
    payload = {
        "type": "message",
        "role": role,
        "content": [{"type": item_type, "text": text}],
    }
    if phase is not None:
        payload["phase"] = phase
    return record("response_item", payload)


def event(event_type, **values):
    return record("event_msg", {"type": event_type, **values})


def modern_turn(user, assistant, commentary=None):
    rows = [
        event("task_started"),
        response_message("user", "injected context"),
        event("user_message", message=user),
    ]
    if commentary is not None:
        rows.extend(
            [
                event("agent_message", message=commentary),
                response_message("assistant", commentary, "commentary"),
            ]
        )
    rows.extend(
        [
            response_message("assistant", assistant, "final_answer"),
            event("task_complete", last_agent_message=assistant),
        ]
    )
    return rows


def write_rollout(home, session_id, rows, archived=False, malformed=False):
    if archived:
        directory = home / "archived_sessions"
    else:
        directory = home / "sessions" / "2026" / "01" / "01"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-01-01T00-00-00-{session_id}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(rows):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            if malformed and index == 1:
                handle.write("{not-json\n")
    return path


class SessionIdParsingTests(unittest.TestCase):
    def test_extracts_one_id(self):
        self.assertEqual(reader.extract_session_ids(ID_A), [ID_A])

    def test_extracts_space_comma_and_list_ids(self):
        raw = f"sessions:\n- {ID_A}, {ID_B}\n- {ID_C}"
        self.assertEqual(reader.extract_session_ids(raw), [ID_A, ID_B, ID_C])

    def test_deduplicates_and_preserves_order(self):
        self.assertEqual(reader.extract_session_ids(f"{ID_B} {ID_A} {ID_B}"), [ID_B, ID_A])

    def test_ignores_natural_language_and_abbreviated_ids(self):
        raw = f"read session 019abc... and ordinary text; exact={ID_A}suffix"
        self.assertEqual(reader.extract_session_ids(raw), [])

    def test_normalizes_uppercase(self):
        self.assertEqual(reader.extract_session_ids(ID_A.upper()), [ID_A])


class ReaderFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def create_session(self, session_id=ID_A, archived=False, turns=None, malformed=False):
        rows = [session_meta(session_id)]
        for turn in turns or [("question", "answer")]:
            rows.extend(modern_turn(*turn))
        return write_rollout(self.home, session_id, rows, archived, malformed)

    def read(self, ids, **options):
        return reader.read_requested_sessions(ids, self.home, **options)

    def test_reads_active_and_archived_sessions(self):
        self.create_session(ID_A)
        self.create_session(ID_B, archived=True)
        sessions, failures = self.read([ID_A, ID_B])
        self.assertEqual([item.session_id for item in sessions], [ID_A, ID_B])
        self.assertEqual([item.source for item in sessions], ["active", "archived"])
        self.assertEqual(failures, [])

    def test_nonexistent_session_is_partial_failure(self):
        self.create_session(ID_A)
        sessions, failures = self.read([ID_A, ID_C])
        self.assertEqual([item.session_id for item in sessions], [ID_A])
        self.assertEqual(failures[0].session_id, ID_C)
        self.assertEqual(failures[0].error, "session not found")

    def test_duplicate_exact_rollout_is_ambiguous(self):
        self.create_session(ID_A)
        self.create_session(ID_A, archived=True)
        sessions, failures = self.read([ID_A])
        self.assertEqual(sessions, [])
        self.assertIn("ambiguous", failures[0].error)

    def test_filename_must_match_first_metadata_id(self):
        write_rollout(self.home, ID_A, [session_meta(ID_B)])
        sessions, failures = self.read([ID_A])
        self.assertEqual(sessions, [])
        self.assertIn("metadata", failures[0].error)

    def test_extracts_user_and_final_answer_not_commentary_or_tool_noise(self):
        rows = [session_meta(ID_A), event("task_started")]
        rows.extend(
            [
                response_message("developer", "secret"),
                response_message("user", "bootstrap"),
                event("user_message", message="为什么？"),
                response_message("assistant", "处理中", "commentary"),
                record("response_item", {"type": "function_call", "name": "shell"}),
                record("response_item", {"type": "function_call_output", "output": "noise"}),
                response_message("assistant", "最终\n回答", "final_answer"),
                event("task_complete", last_agent_message="最终\n回答"),
            ]
        )
        write_rollout(self.home, ID_A, rows, malformed=True)
        sessions, failures = self.read([ID_A])
        self.assertEqual(failures, [])
        self.assertEqual(
            [(m.role, m.text) for m in sessions[0].messages],
            [("user", "为什么？"), ("assistant", "最终\n回答")],
        )
        self.assertTrue(any("malformed" in warning for warning in sessions[0].warnings))

    def test_entirely_corrupt_transcript_fails_only_that_session(self):
        path = write_rollout(self.home, ID_A, [session_meta(ID_A)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not-json\n")
        self.create_session(ID_B)
        sessions, failures = self.read([ID_A, ID_B])
        self.assertEqual([session.session_id for session in sessions], [ID_B])
        self.assertEqual(failures[0].session_id, ID_A)
        self.assertIn("could not be parsed", failures[0].error)

    def test_multiple_turns_preserve_unicode_and_order(self):
        self.create_session(
            turns=[("第一行\n第二行", "回答一"), ("Question two", "Answer two")]
        )
        sessions, _ = self.read([ID_A])
        self.assertEqual(
            [message.text for message in sessions[0].messages],
            ["第一行\n第二行", "回答一", "Question two", "Answer two"],
        )

    def test_empty_content_is_ignored(self):
        rows = [
            session_meta(ID_A),
            event("task_started"),
            event("user_message", message=""),
            response_message("assistant", "", "final_answer"),
            event("task_complete", last_agent_message=""),
        ]
        write_rollout(self.home, ID_A, rows)
        sessions, _ = self.read([ID_A])
        self.assertEqual(sessions[0].messages, [])

    def test_schema_fallback_prefers_task_complete_over_last_assistant(self):
        rows = [
            session_meta(ID_A),
            event("task_started"),
            response_message("user", "legacy question"),
            response_message("assistant", "intermediate"),
            event("task_complete", last_agent_message="legacy final"),
        ]
        write_rollout(self.home, ID_A, rows)
        sessions, _ = self.read([ID_A])
        self.assertEqual(
            [message.text for message in sessions[0].messages],
            ["legacy question", "legacy final"],
        )
        self.assertTrue(any("response_item user" in warning for warning in sessions[0].warnings))
        self.assertTrue(any("task_complete" in warning for warning in sessions[0].warnings))

    def test_last_assistant_fallback_keeps_incomplete_turn(self):
        rows = [
            session_meta(ID_A),
            event("task_started"),
            event("user_message", message="question"),
            response_message("assistant", "only assistant message", "commentary"),
        ]
        write_rollout(self.home, ID_A, rows)
        sessions, _ = self.read([ID_A])
        self.assertEqual(sessions[0].messages[-1].text, "only assistant message")
        self.assertTrue(any("last assistant" in warning for warning in sessions[0].warnings))

    def test_last_and_message_limit_keep_complete_turns(self):
        self.create_session(turns=[("q1", "a1"), ("q2", "a2"), ("q3", "a3")])
        sessions, _ = self.read([ID_A], max_messages=2, max_chars=100, last=2)
        self.assertEqual([m.text for m in sessions[0].messages], ["q3", "a3"])
        self.assertTrue(sessions[0].truncated)
        self.assertIn("2 of 6 message", " ".join(sessions[0].warnings))

    def test_oversized_latest_turn_is_retained_intact(self):
        self.create_session(turns=[("old", "answer"), ("x" * 20, "y" * 20)])
        sessions, _ = self.read([ID_A], max_messages=1, max_chars=10)
        self.assertEqual(len(sessions[0].messages), 2)
        self.assertEqual(sessions[0].messages[0].text, "x" * 20)
        self.assertTrue(any("retained it intact" in warning for warning in sessions[0].warnings))

    def test_text_output_preserves_provenance_and_reports_failure(self):
        self.create_session(ID_A)
        sessions, failures = self.read([ID_A, ID_C])
        output = reader.render_text(sessions, failures)
        self.assertIn(f"===== CODEX SESSION: {ID_A} =====", output)
        self.assertIn("[user]\nquestion", output)
        self.assertIn(f"- {ID_C}: session not found", output)

    def test_json_output_has_stable_counts_and_failures(self):
        self.create_session(ID_A)
        sessions, failures = self.read([ID_A, ID_C])
        payload = json.loads(reader.render_json(sessions, failures))
        self.assertEqual(payload["sessions"][0]["counts"]["available_user"], 1)
        self.assertEqual(payload["sessions"][0]["counts"]["available_assistant"], 1)
        self.assertEqual(payload["failures"][0]["session_id"], ID_C)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        write_rollout(
            self.home,
            ID_A,
            [session_meta(ID_A), *modern_turn("question", "answer")],
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def invoke(self, argv, stdin_text=""):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = reader.main(argv, io.StringIO(stdin_text), stdout, stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_reads_ids_from_stdin(self):
        code, output, error = self.invoke(
            ["--codex-home", str(self.home)], f"sessions:\n- {ID_A}"
        )
        self.assertEqual(code, 0)
        self.assertIn(ID_A, output)
        self.assertEqual(error, "")

    def test_partial_success_exits_zero(self):
        code, output, _ = self.invoke(
            ["--codex-home", str(self.home), ID_A, ID_C]
        )
        self.assertEqual(code, 0)
        self.assertIn("Failed:", output)

    def test_all_failures_exit_one(self):
        code, output, _ = self.invoke(["--codex-home", str(self.home), ID_C])
        self.assertEqual(code, 1)
        self.assertIn("session not found", output)

    def test_no_valid_id_exits_two(self):
        code, _, error = self.invoke(["--codex-home", str(self.home)], "no id")
        self.assertEqual(code, 2)
        self.assertIn("no complete", error)

    def test_full_is_explicitly_rejected(self):
        code, _, error = self.invoke(["--full", ID_A])
        self.assertEqual(code, 2)
        self.assertIn("not supported", error)

    def test_json_fatal_error_is_machine_readable(self):
        code, output, error = self.invoke(["--format", "json"], "no id")
        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertIn("error", json.loads(output))


if __name__ == "__main__":
    unittest.main()
