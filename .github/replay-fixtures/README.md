# Replay fixtures

Self-contained MCP replay tooling used by `release.yml` to smoke-test the built
container before publishing to GHCR.

## Files

- `mcp_replay.py` — vendored from `mcp-replay-test/tools/mcp_replay.py`. Replays
  recorded MCP tool calls against a running `okto-pulse` MCP server (port 8101)
  and reports diffs. Exit code is always 0; CI gates on log-grep instead.
- `session_short.jsonl` — vendored from
  `mcp-replay-test/mcp-traces/session_5df67458775f4c8b9857f382f798de53_20260428T101724.jsonl`
  (~75 KB, 4 events). Short enough for a CI smoke run, long enough to exercise
  list/create/derive flows.

## Updating

If `mcp-replay-test/` (the upstream working repo, not yet pushed to GitHub) gets
updated, copy the new versions over and commit.
