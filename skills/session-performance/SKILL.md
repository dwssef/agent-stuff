---
name: session-performance
description: Read-only Codex session performance diagnostics. Use when a Codex session feels slow or when deciding whether to continue, finish a milestone, or switch sessions; inspect rollout JSONL, Codex state/log SQLite databases, TTFT, turn duration, token usage, context growth, compaction, tool latency, retries, repeated calls, and recent performance trends.
---

# Session Performance Monitor

Use the bundled CLI before deciding whether a session is becoming a liability:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/session-performance/scripts/session_performance.py"
```

It selects `CODEX_THREAD_ID` first, then the state DB's most recent thread, then the newest
`~/.codex/sessions/**/rollout-*.jsonl`. Select another session with `--thread ID_OR_PATH` and
request machine-readable output with `--json`.

Use `--last N` to change the trend window and `--log-cap N` when a deployment uses a different
per-thread log retention watermark. A live first turn has no completed-turn baseline; the analyzer
reports a neutral score with low confidence until a turn completes.
When the current turn is still in flight, the text dashboard falls back to recent completed-turn
agent-throughput and tool-execution aggregates while marking current-turn values unavailable.

## Interpretation

- `task_complete.time_to_first_token_ms` is the best available completed-turn TTFT.
- `task_complete.duration_ms` is full turn wall time, not model generation time.
- `token_count.total_token_usage` is cumulative; per-turn token attribution is approximate because
  token-count events do not carry `turn_id`.
- `codex_core::tools::parallel` logs provide Codex-measured `dispatch_duration_ms`,
  `handler_duration_ms`, and `total_duration_ms` keyed by `call_id`/`turn_id`.
- `Model generation tok/s` is normally `N/A`: the persisted data has no server generation start/end
  or per-token timestamps. The CLI labels output/turn-second and residual rates as agent throughput
  or approximate estimates, never as true model tok/s.
- If the current turn is incomplete, the text report shows recent completed-turn agent throughput
  and tool-execution totals; a residual model-rate estimate is shown only as low-confidence context.
- Use recent completed turns (default 10), robust medians, p95, and first-half versus second-half
  trends. Do not let one outlier or compaction count alone decide.
- Treat compaction as a weak signal: 0–2 is usually normal, 3–5 merits observation, 6+ raises
  concern, and 8+ is a switch candidate only when density, rapid refill, context pressure, retries,
  or repeated work agree.

The report emits `GOOD`, `SLOW`, `DEGRADED`, or `SWITCH SESSION`, a 0–100 score, bottleneck risks,
and one explicit recommendation. Missing observations reduce confidence; they are not silently
treated as healthy or as a reason to switch.

## Read-only and privacy rules

The analyzer opens SQLite with `mode=ro` and `PRAGMA query_only=ON`, never checkpoints or rewrites
the WAL, and only reads complete JSONL lines. It must not modify files beneath `sessions/`, state DBs,
or logs DBs. Reports contain counts, timings, categories, sizes, and hashed normalized call
signatures only; never print user messages, command arguments, tool-output bodies, or reasoning text.

For field details and known limitations, see [references/data-model.md](references/data-model.md).
