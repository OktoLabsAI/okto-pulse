# Contributing to Okto Pulse

Thanks for your interest in contributing. Okto Pulse is the CLI, embedded
frontend, and Docker packaging for the project; the underlying engine lives
in the sibling repo [`okto-pulse-core`](https://github.com/OktoLabsAI/okto-pulse-core).
Most feature work touches both repos — check there first if your change is
about board/spec/ideation logic rather than the CLI, API surface, or
packaging.

## Before you start

- **Search existing issues and PRs** to avoid duplicate work.
- **For anything non-trivial** (new features, breaking changes, architecture
  changes), open an issue first to discuss the approach before writing code.
- **Small fixes** (typos, docs, obvious bugs) can go straight to a PR.

## Contributor License Agreement

By submitting a pull request to this repository, you agree to the terms in
[`CLA.md`](./CLA.md). No separate signature step is required — opening the
PR indicates agreement.

## Development setup

```bash
git clone https://github.com/OktoLabsAI/okto-pulse-core.git
git clone https://github.com/OktoLabsAI/okto-pulse.git
cd okto-pulse

pip install -e ../okto-pulse-core -e ".[dev]"

okto-pulse init
okto-pulse serve     # API + frontend on :8100, MCP on :8101
```

Frontend (only needed if you're changing UI):

```bash
cd frontend
npm install
npm run dev           # local dev server
npm run build          # production build, synced into the packaged CLI
npm run lint
npm test               # vitest
npm run test:e2e       # playwright
cd ..
```

See [`CLAUDE.md`](./CLAUDE.md) for the full local dev, Docker, and release
procedures used in this repo.

## Making a change

1. Create a branch off `main` (don't commit directly to `main`).
2. Keep the change focused — one logical change per PR.
3. Match the existing code style in the file(s) you touch; don't reformat
   unrelated code.
4. Add or update tests for behavior you change.
5. Update relevant docs (`README.md`, `docs/`, `CLAUDE.md`) if the change
   affects setup, CLI flags, env vars, or the release process.

## Running tests

```bash
pytest -q
pytest tests/test_specific.py::test_name -v
```

Some tests are marked `e2e`, `slow`, or `stress` (see
`[tool.pytest.ini_options]` in `pyproject.toml`) and are excluded from the
fast default suite — run them explicitly with `-m e2e` etc. when relevant to
your change.

Note: this repo does not currently gate CI on the full pytest suite (see
"Known caveats" in `CLAUDE.md`). CI does run a Dockerfile build-verify step —
make sure your change doesn't break `docker build` if it touches
dependencies, packaging, or the Dockerfile.

## Commit messages

Write clear, descriptive commit messages explaining *why*, not just *what*.
Reference related issues where relevant (e.g. `Fixes #123`).

## Pull requests

- Describe what changed and why.
- Link any related issue.
- Make sure `pytest -q` and (if you touched the frontend) `npm run lint` /
  `npm test` pass locally before requesting review.
- A maintainer will review and may ask for changes. Please be responsive to
  feedback — PRs that go stale without activity may be closed.

## Reporting bugs

Open a GitHub issue with:

- What you expected to happen vs. what actually happened.
- Steps to reproduce.
- Version (`okto-pulse --version`), OS, and how you're running it
  (local install, Docker, etc.).

## Reporting security vulnerabilities

**Do not open a public issue for security vulnerabilities.** Follow the
process in [`SECURITY.md`](./SECURITY.md) instead.

## License

By contributing, you agree that your contributions will be licensed under
the same license as the project (see [`LICENSE`](./LICENSE)), subject to the
[CLA](./CLA.md).
