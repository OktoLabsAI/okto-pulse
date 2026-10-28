# Card lifecycle in Pulse 0.4.2

Cards created through the UI, REST API, or MCP start in `not_started`. Starting
work is an explicit `not_started` → `in_progress` transition, subject to the
current Spec, dependency, resource, and permission gates. Tasks and bugs then
follow the governed Validation and Done flow; Test Cards require passing
scenario evidence before Done. A direct create in `in_progress` is rejected.

The Kanban columns, card status selector, filters, pagination, and typed Card
status contract use `not_started`, `in_progress`, `validation`, `rejected`,
`on_hold`, `done`, and `cancelled`. `rejected` is a consequence of failed task
validation, not a manual destination. A rejected drag returns the card to its
origin and refreshes server state; the server remains the source of truth.

On startup, persisted Card rows with the former `started` status are normalized
to `in_progress` before Card readers and workers run. The migration is
idempotent and records an Activity Log/event for each changed Card. Historical
activity keeps its original `started` value so the timeline explains the
transition. Normalization does not itself satisfy current delivery or
verification gates. A migrated Card must pass the current gates before it
can enter Validation or Done. Existing return permissions from the old
`started` route are reconciled for eligible migrated Cards only.

The REST and MCP Card status contracts reject new `started` creation and
transition requests. Other uses of “started” (such as run timestamps and
Spec-edition tracking) are separate concepts and remain valid.
