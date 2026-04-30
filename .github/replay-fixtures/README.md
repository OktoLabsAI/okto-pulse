# Replay fixtures

Self-contained MCP replay tooling used by `release.yml` to smoke-test the built
container before publishing to GHCR.

## Files

- `mcp_replay.py` — vendored from `mcp-replay-test/tools/mcp_replay.py`. Replays
  recorded MCP tool calls against a running `okto-pulse` MCP server (port 8101).
  Always exits 0 unless the trace file is unparseable. The release workflow
  treats its exit code as advisory and gates on the container log scan + health.
- `session_long.jsonl` — vendored from
  `mcp-replay-test/mcp-traces/session_3893d86c53d34716b9f53d358936f5db_20260428T012243.jsonl`
  (~2.6 MB, 862 events). Contains create/move/edit operations across ideation,
  refinement, spec, card phases — exercises the full MCP surface. Used in
  `--behavioral --no-strict` mode so dynamic-ID mismatches don't fail; the
  actual regression gate is whether the container survives the load.
