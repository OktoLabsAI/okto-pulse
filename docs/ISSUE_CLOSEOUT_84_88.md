# Issues #84–#88: CLI, configuration and cognitive badge refresh

Historical qualification: the v0.4.0 simplification removes the public `kg` CLI
group, including migration/export/subtype commands described below. These results
record the earlier version; see the README for current commands.

Reviewed against the Community 0.3.3 development source on 2026-09-14.
All five reports were applicable; none was closed as obsolete.

| Issue | Correction | Regression evidence |
| --- | --- | --- |
| [#84](https://github.com/OktoLabsAI/okto-pulse/issues/84) | `okto-pulse kg subtype` without `declare` prints group help and exits 1, without dispatching an unset handler. The guard follows the contribution in [PR #89](https://github.com/OktoLabsAI/okto-pulse/pull/89), by Rayan-and-beyond. | Missing-child help for kg, kg subtype, metrics and code-traceability. |
| [#85](https://github.com/OktoLabsAI/okto-pulse/issues/85) | Path derivation no longer overwrites `cors_origins`. Constructor, environment and dotenv values are preserved; the default remains `*`. | Configuration-source precedence and real CORS middleware preflight accepting the allowed origin and rejecting another. |
| [#86](https://github.com/OktoLabsAI/okto-pulse/issues/86) | Badge `refresh()` uses React state and a stable callback; superseded requests cannot overwrite newer results after cancellation. | Refresh alone, deduped batch, error/retry, delayed obsolete response, and permission/empty-input guards. |
| [#87](https://github.com/OktoLabsAI/okto-pulse/issues/87) | Terms acceptance uses `CommunitySettings().data_dir`, including its DATA_DIR, legacy-home and dotenv precedence. | Write/read roundtrip under each supported data-home source, including both variables and whitespace-only DATA_DIR. |
| [#88](https://github.com/OktoLabsAI/okto-pulse/issues/88) | Export checks its destination parent before database setup and handles temporary-file, write and replacement OS errors as `ERRO export_output_error: ...`, exit 2. | Missing parent, denied temporary creation/open/write/replace, cancellation cleanup, preservation of an existing output, successful complete JSON replacement. |

## Operator-facing contracts

- `CORS_ORIGINS=https://one.example,https://two.example` restricts browser origins.
  It is not authentication or a network firewall. The default `*` is unchanged.
- Terms acceptance is stored at `<data_dir>/.terms-accepted.json`. Setting
  `DATA_DIR` does not import consent from another installation's legacy home.
  A user who accepted terms only at that old location may need to accept again.
  The old record is neither deleted nor silently migrated.
- `kg export --output <file>` requires an existing destination directory. It
  does not create missing parents. Successful output still uses same-directory
  temporary-file replacement; this patch does not add an fsync durability claim.
- Badge refresh remains read-only and permission-checked; no REST payload or
  public method signature changed.

## Validation

The added regressions were run before correction: 14 backend cases and 3 hook
cases failed, reproducing all five reports. After correction:

- 85 Python tests passed: new issue regressions plus existing CLI, acceptance,
  data-home identity and settings tests.
- 69 frontend tests passed: hook, badges, cognitive-pending panel and Cognitive
  Action Center regressions.
- Both suites passed again in the isolated publication checkout. TypeScript,
  scoped Python/React lint and the production build passed. The packaged build
  contains 78 files, tree SHA-256
  `4857ad808700068f49a7abfb4881ddb046722a59e80cedac48cf1d9e55a48246`.
  Existing Browserslist-age and large-chunk build warnings remain advisory.

Tests use temporary data and mocked graph composition. No user graph, SQLite
database or running Pulse instance is changed. Publication to the development
branch is distinct from installation or release on main.
