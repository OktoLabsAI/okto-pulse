# Contributing to Okto Pulse

Thank you for contributing to the Community edition of Okto Pulse.

## Development Setup

Python 3.11 or newer and Node.js are required. Clone the Core and Community
repositories as siblings:

```bash
git clone https://github.com/OktoLabsAI/okto-pulse-core.git
git clone https://github.com/OktoLabsAI/okto-pulse.git
cd okto-pulse
pip install -e ../okto-pulse-core -e ".[dev]"
```

The Community CI prefers an `okto-pulse-core` branch with the same name as the
Community branch and falls back to Core `main` when none exists. Create matching
branches in both repositories when a change spans the package boundary.
The fallback does not validate a coordinated Core/Community change. Record and
test the exact pair of commits; release builds check out both repositories at
the same tag.

Before behavioral tests against installed wheels, compare all Python files under
`site-packages/okto_pulse/` byte for byte with both local `src/` trees. Use fresh
disposable test processes after reinstalling. Direct source runs must include
both repositories in `PYTHONPATH` (`core/src;community/src` on Windows, using the
actual checkout paths).

## Architecture

`src/okto_pulse/community/adapters/` owns SQLAlchemy, Okto Grafx, filesystem,
scheduler, telemetry, and HTTP client mechanics. REST routers live in `api/`;
the compiled SPA lives in `frontend_dist/`. Core owns domain rules and public
Protocols. Adapters consume those ports instead of private Core modules; add a
missing port in Core instead of duplicating its rules or granting an exception.
Validate both repositories and wheels with `okto-pulse-saas-closure`; every
transitional budget must remain zero.

## Tests

Run the standard Community suite:

```bash
pytest -q -m "not e2e and not stress" tests
```

Run a focused test while iterating:

```bash
pytest -q tests/test_specific.py::test_name
```

The `e2e` and `stress` suites are opt-in because they require disposable runtime
state and take longer. Documentation-only changes can run the focused contributor
contract plus the frontend checks that cover edited files.

## Frontend

```bash
cd frontend
npm install
npm run lint
npm run test
npm run build
```

The build command synchronizes compiled frontend assets into the Python package;
include those generated changes when frontend behavior changes.

## Pull Requests

1. Branch from `main` and keep the change focused.
2. Add or update tests for behavior changes.
3. Run the relevant Python and frontend checks.
4. Link the issue in the pull request description.
5. Resolve all required checks and review discussions.

By submitting a contribution you agree to the [Contributor License Agreement](./CLA.md).
The CLA check is provided by the repository's external GitHub integration.

Please report security issues through the process in [SECURITY.md](./SECURITY.md),
not through a public issue.
