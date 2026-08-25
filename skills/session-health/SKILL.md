---
name: session-health
description: Read-only Codex session diagnostics and continuation decisions. Use when assessing whether the current or a recent Codex session should continue, finish a milestone before switching, or move immediately to a new session; inspect context pressure, compactions, token growth, tool churn, failures, and task drift.
---

# Session Health

Use the bundled analyzer to inspect the current Codex rollout without changing session files:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/session-health/scripts/session_health.py"
```

The analyzer first uses `CODEX_THREAD_ID`, then an explicit `--session`, then the newest
`~/.codex/sessions/**/rollout-*.jsonl`. It accepts both `function_call` and
`custom_tool_call` records and tolerates malformed or forward-compatible JSONL records.

## Output contract

The default report is concise and in Chinese. It always includes:

- `Health Score` from 0 to 100 and `GOOD`, `WATCH`, or `SWITCH`.
- Current context/headroom, recent and lifetime compaction metrics, token/cache metrics,
  tool counts/failures/repetition, output metadata, and data-quality warnings.
- Main risks and exactly one recommendation: continue, switch after the current milestone,
  or open a new session immediately.

Use `--json` when another tool needs the structured metrics. The analyzer never prints user
messages, complete command arguments, or tool-output bodies; output summaries contain only
tool/category names, counts, character totals, and ratios.

## Interpretation rules

Treat compaction count as a weak signal: 0–2 is normally fine, 3–5 merits observation, 6+
raises concern, and 8+ is strongly weighted but never decides by itself. Give more weight to
compaction density, short intervals, rapid post-compaction refill, repeated read/search/test
work, tool failures, and task drift. Recent data (the last 30 minutes or the latest compact
window) drives the score; lifetime data is context.

The script de-duplicates compaction records by window identity and records raw versus unique
counts. It separates active time from long idle gaps, reports cache hit rate and 80%/90%
context forecasts, and flags model/workspace changes, restored history, incomplete events, and
unknown record shapes as data-quality or lifecycle context rather than silently treating them
as healthy work.

## Options

- `--session ID_OR_PATH`: select a rollout by session ID or exact file path.
- `--sessions-dir PATH`: override the sessions directory (useful for fixtures).
- `--json`: emit the same report as structured JSON.

Keep the skill read-only. Use temporary fixture files for parser tests; never edit or rewrite
files under the sessions directory.
