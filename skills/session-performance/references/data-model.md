# Observed Codex performance data model

This is a compatibility reference for the parser. It documents fields observed in the local
Codex 0.144.x files; discover tables and tolerate absent/new fields at runtime.

## Rollout JSONL

Each line is an object with a top-level ISO `timestamp`, `type`, and usually `payload`.

| Record | Fields used | Meaning |
|---|---|---|
| `session_meta` | `payload.id`, `payload.session_id`, `payload.cwd`, `payload.model_provider`, `payload.cli_version` | Session identity and lifecycle anchors. A resumed file can contain a replayed prefix from another id. |
| `event_msg` / `task_started` | `turn_id`, `started_at`, `model_context_window` | Turn start. The top-level timestamp has finer precision than `started_at`. |
| `event_msg` / `task_complete` | `turn_id`, `completed_at`, `duration_ms`, `time_to_first_token_ms` | Exact runtime measurements for a completed turn. |
| `event_msg` / `token_count` | `info.total_token_usage`, `info.last_token_usage`, `info.model_context_window` | Cumulative and latest request usage: input, cached input, output, reasoning output, total. No `turn_id` is present. |
| `event_msg` / `context_compacted` | `type` only | Marker accompanying a `compacted` record; do not count it as another compaction. |
| `response_item` | `type`, `call_id`, `name`, `input`/`arguments`, `status`, `internal_chat_message_metadata_passthrough.turn_id` | Model/tool timeline. Call output records reuse `call_id`. |
| `compacted` | `window_id`, `previous_window_id`, `replacement_history`, `window_number` | Context replacement. Deduplicate by `window_id`. |

`response_item` types observed include `reasoning`, `message`, `custom_tool_call`,
`custom_tool_call_output`, `function_call`, and `function_call_output`.

## State DB

Discover `state_*.sqlite` rather than hard-coding a number. The `threads` table currently includes:

```text
id, rollout_path, created_at_ms, updated_at_ms, recency_at_ms,
tokens_used, model, reasoning_effort, cwd, archived, cli_version,
git_sha, git_branch, has_user_event, history_mode, thread_source
```

`threads.rollout_path` is the preferred path lookup. `tokens_used` is an aggregate/cross-check, not
the current context size. `thread_spawn_edges(parent_thread_id, child_thread_id, status)` can identify
agent lineage. State fields can lag the active rollout, so report divergence instead of overriding
rollout measurements.

## Logs DB

Discover `logs_*.sqlite`; the current schema is:

```text
logs(id, ts, ts_nanos, level, target, feedback_log_body, module_path,
     file, line, thread_id, process_uuid, estimated_bytes)
```

The body is Rust structured-span text, not JSON. Parse only fixed numeric fields:

- target `codex_core::tools::parallel`: `turn_id`, `tool_name`, `call_id`,
  `dispatch_duration_ms`, `handler_duration_ms`, `total_duration_ms`;
- target `codex_core::session::turn`: `estimated_token_count`,
  `auto_compact_scope_tokens`, `auto_compact_scope_limit`, `total_usage_tokens`,
  `full_context_window_limit_reached`;
- target `codex_core::responses_retry`: retry attempt and backoff milliseconds;
- target `codex_core::stream_events_utils`: event labels and emission timestamps, but no reliable
  server generation duration.

Per-thread log retention has been observed at 1000 rows. Treat a count at the configured retention
watermark as truncation and lower confidence; absence of an old row is not evidence that work did not
occur.

## Phase and token limitations

There are no persisted server `response.created`, first-token, last-token, per-token delta, or
server generation start/end timestamps. `responsesapi.websocket_timing` records event names without
numeric timing payloads. Therefore:

1. report runtime TTFT exactly when `task_complete` supplies it;
2. use rollout item timestamps and retry logs only as phase boundary hints;
3. match tool durations by `call_id`, falling back to call-to-output elapsed time;
4. attribute per-turn token deltas between cumulative samples and mark them approximate;
5. leave true model generation tok/s unavailable, while exposing explicitly named agent-throughput
   estimates.
