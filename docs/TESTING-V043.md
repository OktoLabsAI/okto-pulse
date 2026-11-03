# Acceptance execution for v0.4.3

The native Pulse Test Cards use the scoped methods in
`tests/acceptance_v043.py::TaskRequirementAcceptance` and
`tests/acceptance_v043.py::DeliveryAcceptance`. The registered primary
source is Community; Core is a distinct companion source pinned to its complete
Git revision. The installation observes both immutable trees and signs its own
execution receipt. A developer's local pytest or browser report does not replace
that receipt or complete a Card.

The observer's Python process may still belong to the previous application
release. The selectors therefore copy the observed trees into disposable sibling
directories and run `uv sync --frozen --extra dev` against their committed lock.
This creates an isolated environment with the correct package versions without
changing the running installation, package metadata or version checks. UV and,
for React scenarios, Node/npm must be available to the locally authorized runner.
Dependency setup or timeout failures fail the execution; they are not skipped.

React suites run after `npm ci --ignore-scripts` in that disposable source tree.
The acceptance methods require successful nonempty pytest/JUnit and Vitest/JSON
reports, with no skipped, pending or todo tests. They exercise the production
components with isolated API fixtures; they do not claim authenticated browser
interaction. Browser layout, responsive Help and keyboard QA are separately
recorded in the initiative's Pulse history.

| Scenario | Explicit unittest method | Exercised behavior |
| --- | --- | --- |
| Shared requirement | `test_shared_requirement_keeps_other_owner_required` | Core allocation and canonical gate; SQL applicability and independent Spec verification |
| Human mode | `test_human_mode_separates_requirement_from_proof` | Authenticated REST actor boundary; multi-task React form; separate proof and requirement forms |
| Empty note | `test_blank_note_keeps_authenticated_audit` | Real SQL decisions, authenticated author/time, null note, append-only storage |
| Atomic command | `test_atomic_replay_and_failure` | Replay/conflict, frozen selection, injected event rollback, retained UI draft and exact retry |
| Restore | `test_restore_never_recovers_revoked_proof` | Real accepted implementation, human revocation, waive/restore, continued absence of proof credit |
| Help | `test_central_and_contextual_help` | Central topic, contextual destination, accessible controls and lazy decision history |
| Technical safeguards | `test_authorization_storage_upgrade_and_rollback` | Human permissions, closed inputs, storage invariants, governed erasure, verified copy upgrade and rollback |

Delivery selectors are separate methods on `DeliveryAcceptance`; both classes
share only the isolated toolchain and report checks in `PairedAcceptance`.

| Delivery scenario | Explicit unittest method | Exercised behavior |
| --- | --- | --- |
| Shared denominator | `test_shared_obligation_has_one_global_denominator` | One global obligation count, stable task pages and real persisted SQL read without proof scans |
| Validation pending | `test_validation_stays_pending_and_default_view_is_simple` | Lifecycle matrix, three groups, lazy exceptions/Details, pagination, retained form and access failures |
| Historical Done | `test_historical_done_preserves_history_without_current_delivery_credit` | Canonical current-proof requirements, real native proof loss, preserved historical status and denied access |
| Optional/Waived and ACL | `test_optional_waived_and_denied_reads_never_become_delivered_or_empty_success` | Exact requirement history/generation restore, no delivery credit, revocation and unknown capacity |
| Help | `test_delivery_help_is_central_contextual_and_keeps_focus` | Central Delivery status topic, contextual destination, retained drafts and opening/accepted-save focus |
| Technical safeguards | `test_delivery_contract_authority_cursor_and_legacy_copy_upgrade` | Closed query, HMAC actor/scope/population binding, actual same-generation title changes and reauthorization, strict historical405 plus explicit copy406 and worker restart/backfill |

Native profiles select one explicit method for one registered scenario. The
selectors do not make every other scenario pass automatically and must be
executed before their results are associated with the current implementation.
The browser screenshots use illustrative fixtures and remain engineering QA;
they do not establish permission, performance or human comprehension outcomes.

Every affected feature has a separate Help task. Acceptance requires the rules
in the central application Help **and at the point of access**, with the same
choices, effects, authority, edition boundaries and next steps. Agent resources
are complementary and do not replace either human Help surface.

Only the local runner profile authorizes execution. It must bind the Board,
Spec, scenario, exact method, both source identities and immutable revisions.
The selected tests do not grant lifecycle transitions or excuse missing gates.
