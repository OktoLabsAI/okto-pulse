# Replay fixtures

The MCP replay tool and trace fixtures used by `release.yml` live in their own
private repo:

- **Repo:** [OktoLabsAI/mcp-replay-test](https://github.com/OktoLabsAI/mcp-replay-test)
- **Tool:**  `tools/mcp_replay.py`
- **Trace:** `mcp-traces/session_3893d86c53d34716b9f53d358936f5db_20260428T012243.jsonl` (~2.6 MB, 862 events)

The `release.yml` workflow checks out that repo into `./mcp-replay-test/` and
runs the replay against the live container in `--behavioral --no-strict` mode.
The replay tool's exit code is advisory — the regression gate is the container
log scan + health check that runs after it.

## Why this lives elsewhere now

Audit Phase 4d: keeping the 2.6 MB trace file in `okto-pulse` bloated every
clone. Moving it to a sibling repo lets okto-pulse stay tight and lets the
fixture evolve on its own cadence. Cross-repo checkout uses
`secrets.GITHUB_TOKEN` (works for any private repo within the OktoLabsAI org
when the workflow has the right permissions).

## Recording a new trace

See the README in the [mcp-replay-test repo](https://github.com/OktoLabsAI/mcp-replay-test).
Set `MCP_TRACE_ENABLED=1` on the running container to capture sessions; copy
the resulting JSONL into the fixture repo and update the path in
`okto-pulse/.github/workflows/release.yml`.
