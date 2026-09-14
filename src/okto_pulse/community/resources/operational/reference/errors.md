---
version: "1.0"
---

# Common Errors and How to Fix Them

This table is the **single source of truth** for MCP-level errors. Before any ad hoc retry or workaround, consult this section and apply the canonical fix.
First interpret the outer MCP V2 envelope using
`okto-pulse://reference/projection-profiles`. The common retry/uncertainty/job
protocol is in `okto-pulse://workflows/preflight`; domain error shapes below
live inside `data` and do not replace the outer envelope.
Guideline-policy error semantics and retry actions are governed by
`okto-pulse://reference/policy-compliance`.

## Resource Gate

| Error message | Cause | Fix |
|---|---|---|
| `resource_gate_missing_resources` | A blocking Architecture or Mockup resource is missing and not marked N/A for the entity being validated or completed | Call `okto_pulse_get_resource_gate_summary`, then attach the missing blocking artifact. For cards/tasks/tests/bugs, copy inherited artifacts with `okto_pulse_copy_architecture_to_card` or `okto_pulse_copy_mockups_to_card`. If the artifact does not exist yet, create it on the source ideation/refinement/spec first. Use N/A only with a real `justification`. A Knowledge Base is advisory: it remains visible in `advisory_resources` / `advisory_missing_resources`, but its absence never causes this error. |
| `architecture_propagation_blocked` | A source Architecture Design is INELIGIBLE for propagation (active critic findings, or a missing/stale/unloadable verdict) and cannot be copied or propagated to a refinement/spec/card. This also fails the Resource Gate closed when coverage needs that inherited architecture (the gate summary surfaces `architecture_propagation_blocking=true` rather than silently marking N/A). `architecture_warning_acknowledgement` is AUDIT-ONLY and does NOT authorize the copy. The structured payload carries `code`, `source_design_id`, `source_ref`, `source_version`, `parent_source`, `critic_run_id`, `design_version`, `finding_keys`, `issues`, `warnings`, `verdict_status`, and `remediation`. | Fix the SOURCE design: resolve the active findings (update the diagram until the backend critic stops emitting them) or restore its verdict, re-run the critic, then retry the copy. Do NOT mark architecture N/A to bypass. Use `okto_pulse_list_architecture_propagation_legacy` to find already-copied legacy snapshots whose source is now ineligible (read-only diagnostic). |
| `card_resource_read_only` | Tried to create, edit, annotate, import, or delete a card Knowledge Base, Mockup, or Architecture resource directly | Edit the source ideation/refinement/spec resource, then refresh the card with `okto_pulse_copy_knowledge_to_card`, `okto_pulse_copy_mockups_to_card`, or `okto_pulse_copy_architecture_to_card`. |
| `knowledge_governance_invalid_metadata` | Explicit `governance_metadata` is partial, unknown, or violates the closed v1 contract | Read `okto-pulse://reference/knowledge-governance`; fix every sorted `issues[]` entry. Omit the whole field only for an intentionally legacy-compatible write. No row or propagation is produced on failure. |
| `invalid_entity_type` | Resource Gate was called with a non-canonical entity type such as `task`, `test`, or `bug` | Retry with the matrix above: `ideation`, `refinement`, `spec`, or `card`. Tasks, tests, and bugs must use `entity_type=card`. |

**`auto_derive_spec_resources_enabled` is Spec→Card-only — it does NOT auto-fill the ideation/refinement gate.** This board setting governs ONLY the Spec→Card resource copy (`SpecResourcePropagationService.propagate_for_card`, for `knowledge_base`/`architecture`/`mockup`) into tasks/tests/bugs. It is a separate mechanism from the gate's parent→child inheritance. The Resource Gate already inherits a parent's provided artifacts AND its N/A marks compulsorily down the chain ideation→refinement→spec→card: a single `okto_pulse_mark_resource_not_applicable` at the ideation resolves the same resource to `not_applicable` at the child levels (the summary marks it `na_mark.inherited=true` with the source entity) — no re-mark needed. So do NOT expect the setting to auto-attach resources at the ideation/refinement gate; attach or mark N/A once at the nearest level and let it inherit.

## Selective Knowledge Propagation v2

These v2 service errors have the inner domain shape
`{error, code, detail, details, retryable}`. Only
`knowledge_creation_race` is retryable at this boundary. Validation errors for
an incoherent tri-state envelope are rejected before any target or assignment
is created.

| Error code | Cause | Fix |
|---|---|---|
| `conflicting_propagation_parameters` | `okto_pulse_derive_spec_from_refinement` received legacy `kb_ids` together with the v2 `knowledge_propagation` envelope | Choose one contract. Omit the envelope to keep v1, or remove `kb_ids` and send the complete v2 envelope. |
| `knowledge_propagation_creation_expected_revision_invalid` | A spec/card creation envelope used `expected_revision` other than omitted/`0` | Omit it or pass `0`. Creation has no prior mutable scope. |
| `knowledge_propagation_revision_conflict` | Replace/drop/refresh used a stale `expected_revision`; `details.current_revision` reports the current value | Call `okto_pulse_get_card_knowledge_propagation`, reconsider the desired mutation against the returned state, then submit the new intent with that revision and a new idempotency key. Do not blindly overwrite. |
| `knowledge_propagation_idempotency_conflict` | The same idempotency key was reused with a different actor, parent, operation, selection, linkage, or semantic create payload | Generate a new key for the new intent. Reuse the old key only for an exact retry. |
| `knowledge_creation_race` | Concurrent v2 creates reached the same deterministic target before the durable receipt became visible in the current unit of work | MCP automatically retries once with a fresh unit of work. If still returned, `retryable=true`: retry the exact request with the same idempotency key so replay can recover the one durable result. Do not change the request. |
| `knowledge_creation_target_collision` / `knowledge_creation_replay_target_mismatch` | A deterministic target exists without the matching durable ledger result, or a replay target no longer belongs to the expected parent | Stop and investigate persistent state; this fails closed and is not a safe automatic retry. |
| `knowledge_relevance_invalid` / `knowledge_relevance_spec_mismatch` / `knowledge_relevance_spec_missing` | A `linkage`/`relevance_links` item is not an FR, AC, or test scenario on the card's linked spec, or the card has no valid spec | Read the linked spec, use an exact `functional_requirement`, `acceptance_criterion`, or `test_scenario` ID from it, and retry with a new key. The original attempt writes no assignment. |
| `knowledge_assignment_not_refreshable` | Refresh targeted a non-v2 scope, a non-snapshot selection, an unknown root, or an assignment that cannot be refreshed | Read the technical projection. Pass stable root Knowledge IDs for current snapshot assignments; use replace first if snapshot mode is desired. |

## Stories / Topics

| Error message | Cause | Fix |
|---|---|---|
| `topic_not_empty` | Tried to delete a Topic that still has active or archived Stories | Inspect the returned counts, then use `okto_pulse_merge_topics`, `okto_pulse_move_story`, or `okto_pulse_archive_story`. Do not retry delete until the Topic is empty. |
| `Only ready Stories can be converted` | Story is still `draft`, `triage`, `converted`, or archived | List the Story, resolve triage, then call `okto_pulse_move_story(status="ready")` before link/convert. Archived Stories must be restored first. |
| `Story can only be linked to editable Ideations` | Target Ideation is `done`, `cancelled`, archived, or otherwise frozen | Pick an editable Ideation (`draft`, `review`, `approved`, or `evaluating`) or create/restore the correct Ideation before linking. |

## Card / Move Transitions

| Error message | Cause | Fix |
|---|---|---|
| `"A conclusion is required when moving a card to Validation"` / `"A conclusion is required when moving a card to Done"` | Missing executor report: `conclusion`, `completeness`, `completeness_justification`, `drift`, `drift_justification` | Add all 5 parameters to `okto_pulse_move_card`. |
| `"Card type 'test' is not subject to validation gate"` | Called `okto_pulse_submit_task_validation` on a test card | Test cards use scenario execution, not normal task validation. Record honest terminal results (`passed`, `failed`, or `automated`) with required evidence, then request `done` with the executor report. Completion is not product approval. |
| `"N test scenario(s) still have status 'draft'"` / `"ready"` | Test card's linked scenarios not executed/reported | Execute the test and call `okto_pulse_update_test_scenario_status` with its actual result and required evidence, then retry `okto_pulse_move_card`. Never relabel a failure as passed. On a locked Spec, the scenario must already be linked to an executable test card; otherwise `status_not_mutable` still applies. |
| `"Cannot move card forward: spec must be at least 'in_progress'"` | Spec is in `approved` or `validated` | Move the spec to `in_progress` first via `okto_pulse_move_spec` (requires `okto_pulse_submit_spec_evaluation` with `recommendation=approve` on a `validated` spec). |
| `"Validation gate is active. Move card to 'validation' first"` | Tried to move a normal card directly to `done` | Move to `validation` with the executor report, then `okto_pulse_submit_task_validation`. |
| `"Card is not in 'validation' status"` | Validation was requested before the implementor successfully moved the card out of `in_progress` | The implementor must call `okto_pulse_move_card(status="validation", ...)` with the complete executor report and confirm that it succeeded. Only then can a validator submit. |
| `card_rejected_rework_handoff_required` | An implementation/content/evidence mutation was attempted while the card is Rejected | Read the Current rejection cause, move the card `rejected` → `in_progress`, then begin the new implementation attempt. |
| `task_validation_subject_version_conflict` | The card changed after the validation payload was prepared | Reload gate context and retry with the new `expected_subject_version` and a new idempotency key. |
| `task_validation_idempotency_conflict` | One idempotency key was reused with a different validation payload | Preserve the original key only for an exact retry; otherwise use a new key. |
| `reviewer_separation_required` | `reviewer_separation_mode="enforce"` and the task validator is also the card creator, current assignee, or executor-report author | This is an `action_required` outcome, not a transient retry. Follow remediation `request_independent_task_validator`: have a different authorized principal read `okto_pulse_get_task_context(..., profile="full", context_scope="gate")` and submit the validation. The blocked attempt persists neither a validation nor a status change. |

If a persisted legacy board has no `reviewer_separation_mode` field, this error is intentionally not raised: the policy decision is `mode="off"`, `source="legacy_absent_compat"`. The accepted validation still records its conflicts and source for auditability.

## Card Creation

| Error message | Cause | Fix |
|---|---|---|
| `"Every task must be linked to a spec"` | `spec_id` missing on `okto_pulse_create_card` | Always pass `spec_id`. |
| `"<Type> cards can only be created for specs in <list> status. Spec '<title>' is currently '<status>'."` | Spec status doesn't accept card creation of this `card_type` | See card type governance rules. Move the spec forward with `okto_pulse_move_spec`. |
| `"Test scenario(s) not found in spec '<title>': [...]"` | Passed `test_scenario_ids` that don't exist on that spec | List scenarios with `okto_pulse_list_test_scenarios` and use a valid id. |
| `max_scenarios_per_card_exceeded` | A test-card create/link request exceeded `board.settings.max_scenarios_per_card` | Split the scenarios across multiple `card_type="test"` cards and keep each card within the board cap. |

## Bug Cards

**The bug regression protocol (Path A / B / C)** — the rows below reference these paths instead of restating them:

- **Path A — same-spec reuse.** Run `okto_pulse_resolve_bug_regression_scenarios` first. Reuse an existing scenario ONLY when it is eligible by lineage (same spec AND linked to the bug's `origin_task_id` or an explicitly supplied affected task), then create a fresh post-bug `card_type="test"` card referencing it and link it to the bug. Leave validated spec content unchanged. Pre-existing scenarios DO count; the "after the bug" temporal applies to the test TASK (card), not the scenario.
- **Path B — formal amendment.** When no eligible scenario exists, expected behavior changed, or the evidence is cross-spec: create/associate a formal `AmendmentHotfixRevision` (`okto_pulse_create_amendment_revision` — binds to the bug's own spec, starts `draft` — or `okto_pulse_associate_amendment_revision_artifacts`), complete its lineage, register re-executable evidence, and have the validator confirm coverage via `okto_pulse_confirm_amendment_coverage` (the only writer of the non-forgeable `coverage_confirmed` signal). Refinement or spec-revision authoring alone NEVER satisfies the bug gate; there is no skip/override.
- **Path C — hotfix lane.** Post-closure bug with no executable sprint lane: create or choose a `lane_type="hotfix"` sprint, assign the bug and its regression test card, activate it. Same-spec assignment remains the default. A cross-spec Path B test card is accepted only when the exact task, scenario, revision spec and bug are bound by a non-blocking complete amendment plus persisted validator coverage confirmation; `coverage_pending` and unrelated cross-spec cards fail closed. Keep the original closed sprint unchanged.

| Error message | Cause | Fix |
|---|---|---|
| `"origin_task_id is required for bug cards"` | Missing `origin_task_id` | Pass the id of the task where the bug was found. |
| `"Bug cards can only be created with status not_started or started"` | Tried to create in a later status | Create as `not_started`, then advance via `okto_pulse_move_card`. |
| `"Bug card requires at least 1 new test task linked"` / `reason=missing_regression_test_task` | Moving a bug to `in_progress` without a linked regression test card | Preview candidates (`okto_pulse_resolve_bug_regression_scenarios` or REST). Eligible scenario exists → Path A; none → Path B (protocol above). |
| `"Linked test task has no test_scenario_ids"` | The linked card is not a proper test task | Link it to a scenario via `okto_pulse_link_task(target_type="scenario", ...)`, or recreate with `card_type="test"` + `test_scenario_ids`. |
| `"Test task belongs to a different spec"` | The linked test task is on another spec | Create the test task on the same spec as the bug. |
| `"Linked test task must be created after this bug card"` | The linked regression task predates the bug | Create a new `card_type="test"` card after the bug. |
| `"Test scenario does not exist in spec"` / `reason=scenario_not_found` | The scenario id is wrong, was deleted, or the bug reveals missing canonical coverage | Preview candidates with `okto_pulse_resolve_bug_regression_scenarios`. Eligible scenario exists → Path A; none → Path B (protocol above). |
| `reason=unrelated_scenario` | The linked scenario exists on the bug spec but is not linked to the bug origin task or affected tasks | Do not use the unrelated scenario to satisfy the gate. Run `okto_pulse_resolve_bug_regression_scenarios` to find eligible candidates; if none exist, escalate Path B as semantic gap remediation with `semantic_gap_required=true` and `next_action=escalate_semantic_gap`. |
| `reason=cross_spec_scenario` | The linked test card references a scenario from another spec | Use a lineage-eligible scenario on the bug spec. If cross-spec evidence is genuinely required → Path B (protocol above) — do NOT cross-link the bug directly. |
| `reason=missing_amendment_revision` | Cross-spec regression evidence with no formal `AmendmentHotfixRevision` backing this bug (a hotfix lane, label, or manual association does NOT satisfy Path B) | Start Path B step 1 (create/associate the revision — protocol above), then complete lineage + evidence + validator coverage. |
| `reason=coverage_pending` | The amendment lineage is eligible but the **validator has not confirmed coverage** yet — re-executable evidence is necessary but NOT sufficient | The bug stays blocked (lineage eligible ≠ closure-ready). Finish Path B: register re-executable evidence on the regression scenario, then the validator runs `okto_pulse_confirm_amendment_coverage`. Do not attempt to skip. |
| `coverage_not_gate_consumable` | `okto_pulse_confirm_amendment_coverage` was called with a syntactically valid tuple (binding + validator authorization + re-executable evidence all OK) but the **bug regression gate would not consume it**, so the writer fails closed BEFORE persisting — **nothing is written**. Distinct from `coverage_pending`, which is the move-gate block AFTER a confirmation is simply missing. `facts` carry `amendment_id`, `bug_id`, `original_spec_id`, `regression_test_task_id`, `regression_scenario_id`, `scenario_spec_id`, `routed_path`, `resolver_reason`, `coverage_state`, `missing_links` | Fix the reason, not the writer. `routed_path=path_a` (same-spec): the scenario is `unrelated_scenario` — link/create a regression scenario tied to the bug's origin/affected task, or treat it as a semantic gap; do NOT try to confirm Path B on the same spec. `routed_path=path_b` (cross-spec): complete the amendment lineage/evidence until the tuple would reach `path_b_ready`, then re-confirm. No `skip`/`override`/`force`/`bypass`, and `unrelated_scenario` is never relaxed. |
| `gate_bypass_not_allowed` | A `skip_gate`/`override_gate`/`bypass`/`force` (or equivalent) field was sent to an amendment surface | Remove it. MCP/API/UI only REMEDIATE the bug regression gate; they never skip or override it. Follow the Path B sequence instead. |
| `amendment_bug_mismatch` | The amendment revision does not belong to this bug/board (no free reparenting) | Use a revision created for THIS bug; list them with `okto_pulse_list_amendment_revisions`. `origin_bug_id`/`original_spec_id` are fixed at create and cannot be re-pointed. |
| `original_spec_not_done_or_locked` | Tried to create a Path B amendment for a spec that is NOT content-locked (`draft`, or `in_progress` without an active passed validation) | Path B amendments attach to a `done`/`validated` spec, OR an `in_progress` spec that is still **content-locked** — its `current_validation_id` points to a validation with `outcome=success` (a validated spec moved to in_progress for execution, which cannot be edited directly). If the spec is `in_progress` but NOT content-locked it is still editable, so edit it directly. A `failed`/`stale`/`superseded` validation is not a lock. |
| `bug_spec_mismatch` | `original_spec_id` passed to create does not match the bug's own spec | The amendment binds to the bug's spec. Omit `original_spec_id` (it defaults to the bug's spec) or pass the bug's spec id. |
| `invalid_initial_status` | Tried to create an amendment directly in a non-`draft` status (e.g. `approved`/`done`) | A new amendment MUST start as `draft`; advance it through the lifecycle (status changes never skip the gate). |
| `invalid_amendment_status` | `okto_pulse_transition_amendment_revision` got an unknown `status` | Use a real `AmendmentRevisionStatus` (`draft`/`review`/`approved`/`done`/`cancelled`/`superseded`). Unknown values are rejected fail-closed. |
| `invalid_lineage_state` | `okto_pulse_transition_amendment_revision` got an unknown `lineage_state` | Use `incomplete` or `complete`. Unknown values are rejected fail-closed. |
| `incomplete_lineage_artifacts` | Tried to set `lineage_state=complete` before the amendment has enough lineage | Declare at least one `regression_scenario_id`, one `regression_test_task_id` (via `okto_pulse_associate_amendment_revision_artifacts`) and the bug's authoritative origin task in `origin_task_ids`/`affected_task_ids`, then retry. |
| `cannot_promote_incomplete_lineage` | Tried to set `status=approved`/`done` while `lineage_state` is not `complete` | Complete the lineage first (`okto_pulse_transition_amendment_revision` `lineage_state=complete`), then promote the status. Promotion never confirms coverage — the bug stays `coverage_pending` until the validator runs `okto_pulse_confirm_amendment_coverage`. |
| `terminal_amendment_revision` | Tried to mutate a `cancelled`/`superseded` revision (status, lineage, coverage, or artifact associations) | Terminality is monotonic: the revision is permanently immutable. Only an exact retry of its current terminal status is accepted as an effect-free idempotent operation. Create a NEW `okto_pulse_create_amendment_revision` for the bug instead. |
| `SpecLockedError` / `"spec is locked"` | Tried to edit a `validated`/`in_progress` spec to add regression coverage for a post-lock bug | Do not unlock the spec. Leave the current validated spec content unchanged for simple Path A reuse. Eligible existing scenario → Path A (protocol above); no eligible scenario or expected behavior changed → Path B. |
| `status_not_mutable` while updating a test scenario on a `validated`/`done` spec | The scenario is not linked to an executable test card, so the platform treats the update as arbitrary locked-spec mutation | For Path A/reconciliation, create or use a `card_type="test"` card on the same spec, link the scenario, move that card into `started`, `in_progress`, or `validation`, then call `okto_pulse_update_test_scenario_status` with structured evidence. Do not unlock the spec just to record operational test evidence. If there is no eligible existing scenario, use Path B/C instead. |
| `reason=sprint_required` / `reason=sprint_not_active` with `next_action=assign_hotfix_lane` or `activate_hotfix_lane` | Post-closure bug lacks an executable sprint lane | Use Path C (protocol above), then retry. |

## Spec Coverage / Validation

| Error message | Cause | Fix |
|---|---|---|
| `"Cannot start this card: N test scenario(s) have no linked task cards"` | Scenarios have no test cards linked | For each uncovered scenario, create a test card (`card_type="test"` + `test_scenario_ids`) and/or call `okto_pulse_link_task(target_type="scenario", ...)`. |
| `"Cannot start this card: N functional requirement(s) have no linked business rules"` | FR→BR coverage incomplete | Call `okto_pulse_add_business_rule` with `linked_requirements` referencing the uncovered FR indices. |
| `"Cannot start this card: N business rule(s) have no linked task cards"` | BR→Task coverage incomplete | Call `okto_pulse_link_task(target_type="rule", ...)` for each unlinked BR. |
| `"Cannot validate spec: N business rule(s) have no linked task cards"` | Same, at validation time | Same fix — link implementation tasks to every BR. |
| `"Cannot validate spec: N test scenario(s) have no linked test cards"` | Scenario side | Create/link test cards for every scenario. |
| `"Cannot move spec to 'done': N acceptance criteria lack test scenarios"` | AC→Scenario coverage incomplete | Create a scenario for every uncovered AC (use `linked_criteria` with the 0-based index). |
| `"Cannot move spec to 'done': N linked task(s) are not yet done or cancelled"` | Open task cards still attached | Complete or cancel the pending task cards (bugs are excluded from this check). |

## Multi-Value Parameters (`parse_multi_value`)

A multi-value parse failure returns the uniform envelope `{"error": "invalid_multi_value_input", "detail": "<message>"}` — fix: send a **native `list[str]`** (comma-only strings are rejected).
Accepted shapes, the `detail` messages, and the structured `*_json` field rules (which keep the `{"error": "Invalid <param>: <exc>"}` shape): `okto-pulse://reference/multivalue`.

## List / Projection

| Error code | Cause | Fix |
|---|---|---|
| `missing_required_filter` | `okto_pulse_list_by_board` called with `entity_type="refinement"` without `filters.ideation_id`, or `entity_type="sprint"` without `filters.spec_id` | Pass the required filter: refinements require `filters={"ideation_id": "..."}`; sprints require `filters={"spec_id": "..."}`. |
| `invalid_filter` | An unknown filter key for that `entity_type`, or a malformed `filters` JSON string, was passed to `okto_pulse_list_by_board`/`list_qa`/`list_knowledge` | Use only the keys in the response's `supported` field (`invalid_keys` echoes the rejected ones) — the per-`entity_type` filter table lives in `okto-pulse://reference/list_tools`. |
| `unsupported_projection` | The requested `profile` is not supported by this tool (e.g. `detail` on the `copy_*_to_card` family) | Pick a profile from the returned `supported_profiles` list. Profile semantics and per-family variance: `okto-pulse://reference/projection-profiles`. |
| `resource_lineage_resolution_failed` | The Resource Gate could not resolve the entity's resource lineage (broken/missing parent chain) — fail-closed, not a coverage verdict | Inspect `lineage_error_code`/`lineage_error_details` in the payload and repair the lineage (e.g. the card's spec or the spec's parent is missing/deleted); do not mark resources N/A to bypass. |

## SK-A Quality, Research Decisions, and Checklist

REST and MCP preserve one semantic code/envelope. Transport status may differ,
but a client must not invent a surface-specific remediation.

| Error code | Cause | Fix |
|---|---|---|
| `forbidden` | One side of the composed domain + SK-A leaf authority is absent | Refresh permissions; obtain both authorities. A leaf alone never grants a domain action. |
| `version_conflict` | Subject version, head revision, execution revision, or another CAS fence changed | Re-read full context/current head and rebuild the request; keep the idempotency key only for an exact retry. |
| `validation_failed` | Closed payload/taxonomy/anchor/state validation failed | Correct the reported safe reason. Do not retry unchanged. |
| `question_budget_exceeded` | An assessment proposed more than five Q&A items | Split the work into another assessment run; each run permits at most five. |
| `resolved_evidence_required` | A resolved RDL entry has neither evidence nor an explicit evidence-absence justification | Attach versioned evidence or state the bounded justification. |
| `checklist_incomplete` | The checklist submission omitted, duplicated, or added an item | Submit every immutable template item exactly once. |
| `checklist_binding_off` | Execution was attempted while the effective binding mode is `off` | Do not create an execution; a human may govern the binding in Board Config. There is no MCP binding mutator. |
| `human_actor_required` | An agent attempted a human-only Refinement skip or checklist-binding governance action | Ask an authenticated human to perform the governed action; do not seek an agent permission alias. |
| `invalid_pagination` | Offset/limit/cursor is primitive-, range-, menu-, or combination-invalid | REST uses non-negative offset and limit `25|50|100`; MCP uses limit `1..200` and an opaque cursor that cannot be combined with a non-zero offset. |

Full operational protocol:
`okto-pulse://reference/quality-assessments`.

## KG Graph Availability Errors

These structured error keys appear in KG query responses when the embedded graph is in a degraded state. See the **Degraded-KG Fallback Rule** in `okto-pulse://workflows/kg` for the full protocol.

| Error key | Cause | Fix |
|---|---|---|
| `graph_unavailable` | The embedded graph database is in a hard-reject state (`graph_state` is `recovery_needed` or `quarantined`). Returned by KG query tools (e.g. `okto_pulse_kg_get_learning_from_bugs`) when queries cannot be served. On a degraded board, `graph_unavailable` is the **expected** signal — do not retry in a loop. | Call `okto_pulse_kg_health(board_id)` to confirm `graph_state`. If degraded, follow the KG Health recovery flow (`okto-pulse://reference/kg-health`). This is an operator-driven path; the Degraded-KG Fallback Rule lets you proceed past the Stage 1 triad while the graph recovers. |
| `cognitive_status_unavailable` | The cognitive closeout gate could not confirm the cognitive consolidation status for a `done` transition because `graph_state` is `None` and no generation exists (the unconfirmed shape). This is a fail-closed signal: the gate cannot read the ledger and will not silently allow the transition. | Confirm board health via `okto_pulse_kg_health`. If the board's cognitive consolidation setting needs to be bypassed temporarily, enable `skip_cognitive_consolidation` in board settings. For full recovery, follow the KG Health recovery flow (`okto-pulse://reference/kg-health`). |

## Agent-mediated Code Traceability

These codes govern receipts submitted after an authenticated external agent has
performed its capability/access check and deterministic investigation. Pulse
Core and Pulse Community never remediate an error by opening or searching the
repository themselves.

| Error code | Cause | Remediation |
|---|---|---|
| `code_investigation_request_not_found` | The request does not exist or is outside the board. | Re-read the board-scoped subject and start a new investigation request. |
| `code_investigation_request_not_open` | The request was consumed, revoked, or expired. | Start a new request; only an identical idempotent replay may return the prior result. |
| `code_investigation_challenge_invalid` | The challenge does not match its server-bound digest. | Discard it and start a new request. |
| `code_investigation_receipt_expired` | The receipt or challenge exceeded its TTL. | Start a fresh preflight and submit its result within the advertised TTL. |
| `code_investigation_receipt_revoked` | An append-only revocation applies to the receipt. | Run a new external preflight and submit a replacement receipt. |
| `code_investigation_receipt_conflicted` | The source head has conflicting attestations. | Re-read the head and resolve the attestation conflict according to board policy. |
| `code_investigation_challenge_consumed` | The one-time challenge was already consumed. | Use the exact idempotent replay or start a new request. |
| `code_investigation_actor_kind_required` | The submission principal is not an authenticated agent. | Submit through an authenticated agent principal; a human UI action cannot impersonate one. |
| `code_investigation_capability_missing` | A required capability was not attested. | Have the external agent perform the missing check or follow the explicit waiver path. |
| `code_investigation_attestor_mismatch` | The authenticated actor differs from the request or receipt attestor. | Continue with the bound agent or start a new request for the current agent. |
| `code_investigation_source_scope_mismatch` | The logical source reference is outside the server-bound board/request lineage. | Re-read context and start a correctly scoped request. |
| `code_investigation_subject_version_conflict` | The live subject changed after the request was created. | Re-read full context and run a new preflight for the current version. |
| `code_investigation_selector_scope_mismatch` | A parent, target, or selector is outside the request snapshot. | Rebuild the submission from the current request scope. |
| `code_investigation_unavailable` | The agent declared insufficient access. | Resolve external agent access or use the board's explicit human-waiver flow; Pulse will not inspect the source. |
| `code_investigation_head_conflict` | Source generation or predecessor advanced before commit. | Re-read the head and run a new external preflight. |
| `code_investigation_idempotency_conflict` | The same idempotency key was reused with a different digest. | Use a new key for changed input; retain the key only for an exact retry. |
| `code_investigation_profile_mismatch` | Canonicalization, limits, or fingerprint profile is incompatible. | Use the profiles returned by the current request. |
| `code_investigation_trust_insufficient` | Trust or corroboration does not meet board policy. | Submit acceptable agent attestation or follow the explicit waiver path. |
| `code_investigation_currentness_unknown` | No accepted, current, conflict-free source head exists. | Run a fresh external preflight and re-read currentness. |
| `code_investigation_payload_digest_mismatch` | The recomputed payload or excerpt digest differs. | Re-canonicalize the exact bounded submission and retry with a new key. |
| `code_investigation_submission_limit_exceeded` | Payload, item, candidate, or excerpt caps were exceeded. | Reduce the bounded submission to the advertised hard limits. |
| `code_path_invalid` | A submitted path is not relative and normalized. | Submit a safe logical relative path with no absolute root or `..`. |
| `code_path_denied` | A submitted value violates path or sensitive-content policy. | Remove or redact the denied value; Pulse did not open the file. |
| `code_evidence_submission_failed` | Evidence failed closed validation. | Correct the safe validation details and submit again. |
| `code_evidence_receipt_mismatch` | Evidence does not match the accepted receipt, head, or current subject. | Run a current preflight and bind Evidence to its exact receipt. |
| `code_evidence_immutable` | Historical Evidence was edited in place. | Create a new Evidence item that supersedes the original. |
| `code_evidence_attestation_required` | Policy requires Evidence linked to an accepted receipt. | Submit a current external-agent receipt, then the Evidence. |
| `code_evidence_disposition_required` | Inherited Evidence remains pending, so Code Evidence Matrix coverage is below 100%. | Link it to applicable Spec entities or record an explicit disposition. An authorized human can auditably skip only this coverage obligation in the Code Evidence Matrix tab or for every Spec in Menu → Board. |
| `code_evidence_link_invalid` | The Spec entity or lineage is invalid. | Re-read full Spec context and link to a current supported entity. |
| `implementation_target_invalid` | The semantic selector is incoherent. | Correct the selector and preserve its logical source scope. |
| `implementation_target_resolution_required` | A required Target has no resolution. | Have the agent externally re-evaluate it and submit a resolution receipt. |
| `implementation_target_resolution_outdated` | A newer receipt advanced the source head. | Re-evaluate the Target against the current head and submit a new resolution. |
| `implementation_target_stale` | The Target changed semantically. | Update or supersede the Target after external agent investigation. |
| `implementation_target_ambiguous` | Multiple bounded candidates remain. | Refine the selector or explicitly report ambiguity; never choose silently. |
| `implementation_target_missing` | No candidate was reported. | Confirm source access/state externally, then update or waive the Target. |
| `implementation_overlap_blocking` | Another active Task operates on the same resolved target. | Create a dependency or a policy-compliant acknowledgement. |
| `implementation_overlap_ack_stale` | Resolutions changed after acknowledgement. | Re-read overlaps and create a new decision against current resolutions. |
| `target_execution_disposition_required` | An active required Target lacks an execution record. | Submit its agent execution receipt before completion. |
| `code_traceability_waiver_required` | A not-applicable decision lacks an explicit waiver. | Ask an authorized human to record the bounded waiver. |
| `code_traceability_locked` | A mutation targeted a locked entity/version. | Reopen through an advertised SDLC transition or create a new version. |

Every Code Traceability error has the inner closed semantic shape
`{code, message, details, remediation[]}`. Each remediation entry contains an
`action` and, when an MCP action exists, its exact `tool`; clients must not
replace typed blockers with a generic repository fallback.

Canonical protocol: `okto-pulse://reference/code-traceability`.

## Community operational notes

The embedded graph provider in Community is Okto Grafx. Common gate and retry
rules above remain identical to Core; provider-specific recovery is documented
in `okto-pulse://reference/kg-health` and `okto-pulse://workflows/kg`.

For **Normal and Bug** cards, an admitted task-validation submission evaluates
both the assessment and the completion gates. A failed assessment, or a passed
assessment followed by a blocking completion gate, atomically moves the card
from `validation` to `rejected` and publishes the sealed Current rejection
cause. `rejected` means **rework is required**, not "waiting for another
validator": do not resubmit validation from that status. The only public rework
edge is `rejected` → `in_progress`; after the implementation changes, submit a
new executor report when moving back to `validation`, then create a new
validation attempt. Historical validations and rejection records remain
append-only.

| Error message | Cause | Fix |
|---|---|---|
| `current_rejection_cause_missing` | A legacy or damaged `rejected` card has no sealed Current cause, so Pulse cannot safely explain the requested rework | Do not bypass or resubmit validation. Repair or migrate the causal record through the operator path, then retry `rejected` → `in_progress`. |
| `card_initial_status_invalid` / schema validation for `status` | A card create attempted to start directly in `rejected` (or another non-initial state) | Create only in `not_started` or `started`. `rejected` is consequence-only and can be assigned only by an admitted validation/completion decision. |
| `card_transition_not_allowed` / schema validation for `status` | `okto_pulse_move_card` used `rejected` as a manual target, or attempted any edge other than the public lifecycle contract | Never move a card manually into `rejected`; it is an internal gate consequence. From `rejected`, the sole public target is `in_progress`. |
| `task_validation_history_append_only` | Tried to delete an admitted task validation, including one referenced by a Current rejection cause | Keep the immutable causal history. After rework, record a new validation attempt; do not edit or delete the earlier result. |
| `"multi-value input must be a JSON array ... or pipe-separated ... — comma-separated input is rejected by REJECT policy"` | A comma-only string (e.g. multi-line prose with commas) was sent to a strict multi-value field | Send a **native list** `["a", "b"]`, a JSON-array string `'["a", "b"]'`, or pipe-separated `"a|b"`. Comma-only is ambiguous and rejected. |
| `"malformed JSON for multi-value param: ... (at pos N)"` | Input started with `[` so the JSON path was taken, but the JSON was invalid | Fix the JSON syntax (quoting, brackets). |
| `"malformed multi-value: expected list, got <type>"` | JSON decoded to a non-list (e.g. an object) | Send an array, not an object. |
| `"malformed multi-value: expected string items, got <type> at index N"` | JSON array had a non-string item | Every item must be a string. |
