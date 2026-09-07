# Grafx ordered pagination integration (0.0.4)

The Pulse Core keeps the knowledge-graph page query backend-neutral. The Community adapter
installs the Grafx physical capability required to execute the stable cursor order
`created_at DESC, id DESC` without sorting every node table on each page.

## Durable policy

- Every Pulse node table owns one exact ordered index named `pulse_page_<lowercase-table>`.
- The indexed key is `(created_at, id)` with Grafx `ordered` layout, exact visibility and an
  ACTIVE durable generation.
- Bootstrap validates an existing definition and fails closed on any mismatch. It never drops,
  replaces or silently repairs a conflicting named authority.
- Creation is fenced. A concurrent winner is accepted only after a fresh registry snapshot
  proves that its complete durable definition is identical.
- The schema is stamped ready only after every ordered index has been validated or created.
- Reopening an already valid graph performs no write transaction and appends no WAL record.

## Query contract

Grafx 0.0.4 recognizes the exact Pulse predicate, including its fail-closed
`NOT (coalesce(...) IN [...])` exclusions, prunes node tables that cannot match, and lazily
merges the remaining ordered streams. The returned rows and cursor boundaries remain identical
to the canonical full-scan/sort plan.

## Regression checkpoint

`test_grafx_ordered_indexes.py`, `test_grafx_schema_bootstrap.py` and
`test_grafx_board_operational_providers.py` jointly cover exact activation, idempotent reopen,
conflict refusal, schema readiness, the real Pulse query template and operational composition.
The grouped checkpoint on 2026-09-07 completed with 62 passing tests.
