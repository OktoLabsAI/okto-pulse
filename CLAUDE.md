# CLAUDE.md — okto-pulse

This file gives Claude (and humans) what they need to operate this repo.
Project overview lives in `README.md`; this file is for procedures.

This repo is the deliverable: CLI, embedded frontend, Dockerfile, and the
release pipeline. It depends on `okto-pulse-core` (a sibling repo) for the
actual engine.

---

## Releasing a new version

The pipeline is **tag-driven**. Pushing a `vX.Y.Z` git tag to this repo
triggers `.github/workflows/release.yml`, which runs gates and publishes a
Docker image to GitHub Container Registry.

- Image: `ghcr.io/oktolabsai/okto-pulse` (public, no auth needed to pull)
- Pipeline runtime: ~9 min on first run, faster afterwards (Buildx cache)
- Branches are working space; the **tag is the release event**

### Release procedure (in order)

1. **Bump version in BOTH repos**. Three files total — `okto-pulse-core/pyproject.toml`,
   `okto-pulse/pyproject.toml`, and `okto-pulse/Dockerfile` (the `ARG OKTO_PULSE_VERSION`
   line). The pin in `okto-pulse/pyproject.toml` (`okto-pulse-core>=X.Y.Z,<1.0.0`)
   does not need to be touched unless you want to raise the floor.

2. **Commit and push to your working branch in both repos.** Both must be at
   their bumped commit on the remote before tagging.

3. **Tag and push okto-pulse-core FIRST** — required by the release workflow's
   preflight:

   ```bash
   cd okto-pulse-core
   git tag -a vX.Y.Z -m "Release X.Y.Z"
   git push origin vX.Y.Z
   ```

4. **Tag and push okto-pulse SECOND** — this triggers the pipeline:

   ```bash
   cd okto-pulse
   git tag -a vX.Y.Z -m "Release X.Y.Z"
   git push origin vX.Y.Z
   ```

5. **Watch the run**:

   ```bash
   gh run watch -R OktoLabsAI/okto-pulse
   ```

If you tag pulse first, the preflight fails fast with a copy-pasteable fix
command. Don't try to "fix forward" by tagging core after — the workflow
won't re-run on a non-tag event. Bump and re-tag (see Recovery below).

### What the pipeline does

Triggered by `push: tags: ['v*.*.*']`. Single job, single runner (`ubuntu-latest`,
Python 3.12). All gates must pass before any image is pushed:

1. Extract version from tag (`vX.Y.Z` → `X.Y.Z`)
2. Preflight: confirm `okto-pulse-core` has the matching `vX.Y.Z` tag
3. Sibling-checkout both repos at the tag (workspace root holds both as siblings)
4. Verify `pyproject.toml` versions in both repos match the git tag
5. Install `okto-pulse-core` in editable mode + replay/test deps
6. Set up Buildx + log in to GHCR (uses built-in `GITHUB_TOKEN`, no PAT)
7. Compute image tags via `docker/metadata-action`
8. Build image (`target=local-runtime`, `load: true` — into local daemon, not pushed)
9. Spin up container, poll `docker inspect ... .State.Health.Status` until `healthy` (max 120s)
10. Extract bootstrap API key (`dash_<hex>`) from container logs
11. Run vendored `mcp_replay.py` against the long trace fixture (862 events,
    `--behavioral --no-strict`)
12. **Replay gate**: scan container logs for `sigbus|sigsegv|segmentation|bus error|ResourceWarning|RuntimeError.*Failed to open`,
    confirm container is still `healthy`
13. Push to GHCR with semver tags: `:X.Y.Z`, `:X.Y`, `:latest`, `:sha-<short>`
14. Cleanup container (always, even on failure)
15. Job summary with pull command

### Image tags published per release

For tag `v0.1.11`:

| Tag | Mutability | Use |
|-----|-----------|-----|
| `:0.1.11` | immutable | exact pin (recommended for prod) |
| `:0.1` | mutable | minor track — picks up future `0.1.x` patches |
| `:latest` | mutable | floating |
| `:sha-b74923a` | immutable | hard SHA pin (for forensic / debugging) |

### Verifying a release

```bash
docker pull ghcr.io/oktolabsai/okto-pulse:X.Y.Z
docker run -d --name pulse-verify -p 8100:8100 -p 8101:8101 \
    ghcr.io/oktolabsai/okto-pulse:X.Y.Z
sleep 30
curl -fsS http://localhost:8100/api/v1/kg/settings  # should 200
docker rm -f pulse-verify
```

If `docker pull` returns 401, the package was created with default-private
visibility on its first push. Flip it at
https://github.com/orgs/OktoLabsAI/packages/container/okto-pulse/settings
("Danger Zone" → Change package visibility → Public).

### Recovery — when the pipeline fails

This repo has a tag-protection ruleset (`Protect release tags v*`) that
blocks tag deletion, force-update, and non-fast-forward. **Tags are
immutable.** Do not try to delete or move them — the API will 422.

If the pipeline fails at any gate:

1. Diagnose with `gh run view --log-failed --job=<id> -R OktoLabsAI/okto-pulse`
2. Fix in code or workflow
3. **Bump the patch version** (e.g. `0.1.11` → `0.1.12`) in both repos
4. Re-run the procedure above

The protection is correct hygiene — failed tags stay in the history as a
record of what didn't ship. Don't try to bypass the ruleset for a clean log;
bump and move forward.

### Known caveats

- **No pytest gate in the workflow.** The `okto-pulse-core` test suite has
  pre-existing post-Ladybug failures, notably `test_close_releases_handles`
  which reproducibly leaks `graph.kuzu` + `graph.kuzu.wal` file handles on
  Linux (real bug in `BoardConnection.close()`, not a CI flake). The smoke
  test + MCP replay gates cover deployability today. Reinstate pytest in
  `release.yml` once the kg layer's `close()` is fixed for Linux.

- **Trace fixture must contain `create_ideation` events.** The replay
  tool's `clean_golden_path` skips everything before the first
  `create_ideation`. The vendored `session_long.jsonl` (~2.6 MB, 862 events)
  has 42 of them. A trimmed-down trace without one will filter to zero and
  fail the smoke step with `No entries to replay after filtering`.

- **Replay tool's exit code is advisory.** The tool exits 0 unless the
  trace is unparseable. The actual gate is the log scan + health check
  that runs after the replay step.

- **First push of a new image to GHCR creates a private package.** Flip
  visibility manually once (see "Verifying a release" above). Subsequent
  pushes inherit the visibility.

- **Cross-repo checkout uses `secrets.GITHUB_TOKEN`.** Works because both
  repos are public. If either goes private, add a fine-grained PAT and
  swap the `token:` parameter in `release.yml` checkout steps.

---

## Local development

```bash
pip install -e ".[dev]"

okto-pulse init
okto-pulse serve     # API+frontend :8100, MCP :8101
okto-pulse status
okto-pulse reset

pytest -q
pytest tests/test_specific.py::test_name -v
```

`okto-pulse serve` requires no LLM keys. The image bundles
`all-MiniLM-L6-v2` (sentence-transformers) for kg embeddings; downloads at
build time so the runtime is offline-capable.

---

## CI workflows in this repo

- `.github/workflows/release.yml` — tag-driven release (above)
- `.github/workflows/ci.yml` — PR + main: pytest gate is currently dropped
  (same caveat as release); runs build verify on the Dockerfile to catch
  Dockerfile/dep regressions before tagging
- `.github/workflows/cla.yml` — CLA signing, do not touch

## Out of scope (deliberately)

- Auto-deploy to Portainer or any production host (deploy stays manual)
- Multi-arch builds (amd64 only; add arm64 only when there's a real consumer)
- Cross-run buildx cache (`type=gha`) — current build is fast enough
- Auto-tag on green main commits — defer until trunk-based discipline exists
