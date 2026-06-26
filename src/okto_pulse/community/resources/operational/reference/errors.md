---
version: "1.0"
---

# Common Errors and How to Fix Them

This table is the **single source of truth** for MCP-level errors. Before any ad hoc retry or workaround, consult this section and apply the canonical fix.

## Resource Gate

| Error message | Cause | Fix |
|---|---|---|
| `resource_gate_missing_resources` | Architecture, Mockup, or Knowledge Base is missing and not marked N/A for the entity being validated, started, or completed | Call `okto_pulse_get_resource_gate_summary`, then attach the missing artifact. For cards/tasks/tests/bugs, copy inherited artifacts with `okto_pulse_copy_architecture_to_card`, `okto_pulse_copy_mockups_to_card`, or `okto_pulse_copy_knowledge_to_card`. If the artifact does not exist yet, create it on the source ideation/refinement/spec first. Use N/A only with a real `justification`. |
| `architecture_propagation_blocked` | A source Architecture Design is INELIGIBLE for propagation (active critic findings, or a missing/stale/unloadable verdict) and cannot be copied or propagated to a refinement/spec/card. This also fails the Resource Gate closed when coverage needs that inherited architecture (the gate summary surfaces `architecture_propagation_blocking=true` rather than silently marking N/A). `architecture_warning_acknowledgement` is AUDIT-ONLY and does NOT authorize the copy. The structured payload carries `code`, `source_design_id`, `source_ref`, `source_version`, `parent_source`, `critic_run_id`, `design_version`, `finding_keys`, `issues`, `warnings`, `verdict_status`, and `remediation`. | Fix the SOURCE design: resolve the active findings (update the diagram until the backend critic stops emitting them) or restore its verdict, re-run the critic, then retry the copy. Do NOT mark architecture N/A to bypass. Use `okto_pulse_list_architecture_propagation_legacy` to find already-copied legacy snapshots whose source is now ineligible (read-only diagnostic). |
| `card_resource_read_only` | Tried to create, edit, annotate, import, or delete a card Knowledge Base, Mockup, or Architecture resource directly | Edit the source ideation/refinement/spec resource, then refresh the card with `okto_pulse_copy_knowledge_to_card`, `okto_pulse_copy_mockups_to_card`, or `okto_pulse_copy_architecture_to_card`. |
| `invalid_entity_type` | Resource Gate was called with a non-canonical entity type such as `task`, `test`, or `bug` | Retry with the matrix above: `ideation`, `refinement`, `spec`, or `card`. Tasks, tests, and bugs must use `entity_type=card`. |

**`auto_derive_spec_resources_enabled` is Spec→Card-only — it does NOT auto-fill the ideation/refinement gate.** This board setting governs ONLY the Spec→Card resource copy (`SpecResourcePropagationService.propagate_for_card`, for `knowledge_base`/`architecture`/`mockup`) into tasks/tests/bugs. It is a separate mechanism from the gate's parent→child inheritance. The Resource Gate already inherits a parent's provided artifacts AND its N/A marks compulsorily down the chain ideation→refinement→spec→card: a single `okto_pulse_mark_resource_not_applicable` at the ideation resolves the same resource to `not_applicable` at the child levels (the summary marks it `na_mark.inherited=true` with the source entity) — no re-mark needed. So do NOT expect the setting to auto-attach resources at the ideation/refinement gate; attach or mark N/A once at the nearest level and let it inherit.

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
| `"Card type 'test' is not subject to validation gate"` | Called `okto_pulse_submit_task_validation` on a test card | Test cards skip the validation gate — move directly to `done` after scenarios are `passed`. |
| `"N test scenario(s) still have status 'draft'"` / `"ready"` | Test card's linked scenarios not updated | Call `okto_pulse_update_test_scenario_status(status="passed")` for each linked scenario, then retry `okto_pulse_move_card`. If the spec is `validated` or `done`, make sure the scenario is already linked to this executable test card (`started`, `in_progress`, `validation`, or `done`); otherwise the scenario status call remains blocked by `status_not_mutable`. |
| `"Cannot move card forward: spec must be at least 'in_progress'"` | Spec is in `approved` or `validated` | Move the spec to `in_progress` first via `okto_pulse_move_spec` (requires `okto_pulse_submit_spec_evaluation` with `recommendation=approve` on a `validated` spec). |
| `"Validation gate is active. Move card to 'validation' first"` | Tried to move a normal card directly to `done` | Move to `validation` with the executor report, then `okto_pulse_submit_task_validation`. |

## Card Creation

| Error message | Cause | Fix |
|---|---|---|
| `"Every task must be linked to a spec"` | `spec_id` missing on `okto_pulse_create_card` | Always pass `spec_id`. |
| `"<Type> cards can only be created for specs in <list> status. Spec '<title>' is currently '<status>'."` | Spec status doesn't accept card creation of this `card_type` | See card type governance rules. Move the spec forward with `okto_pulse_move_spec`. |
| `"Test scenario(s) not found in spec '<title>': [...]"` | Passed `test_scenario_ids` that don't exist on that spec | List scenarios with `okto_pulse_list_test_scenarios` and use a valid id. |
| `max_scenarios_per_card_exceeded` | A test-card create/link request exceeded `board.settings.max_scenarios_per_card` | Split the scenarios across multiple `card_type="test"` cards and keep each card within the board cap. |

## Bug Cards

| Error message | Cause | Fix |
|---|---|---|
| `"origin_task_id is required for bug cards"` | Missing `origin_task_id` | Pass the id of the task where the bug was found. |
| `"Bug cards can only be created with status not_started or started"` | Tried to create in a later status | Create as `not_started`, then advance via `okto_pulse_move_card`. |
| `"Bug card requires at least 1 new test task linked"` / `reason=missing_regression_test_task` | Moving a bug to `in_progress` without a linked regression test card | First run `okto_pulse_resolve_bug_regression_scenarios` or the REST candidate preview. If an eligible existing scenario exists, use Path A: create a fresh post-bug `card_type="test"` card that references that scenario and link it to the bug. If none exists, use Path B. |
| `"Linked test task has no test_scenario_ids"` | The linked card is not a proper test task | Link it to a scenario via `okto_pulse_link_task(target_type="scenario", ...)`, or recreate with `card_type="test"` + `test_scenario_ids`. |
| `"Test task belongs to a different spec"` | The linked test task is on another spec | Create the test task on the same spec as the bug. |
| `"Linked test task must be created after this bug card"` | The linked regression task predates the bug | Create a new `card_type="test"` card after the bug. |
| `"Test scenario does not exist in spec"` / `reason=scenario_not_found` | The scenario id is wrong, was deleted, or the bug reveals missing canonical coverage | First list/preview candidates with `okto_pulse_resolve_bug_regression_scenarios`. If an eligible existing scenario exists, create a fresh post-bug test card referencing it. If no eligible scenario exists, treat this as Path B: create/associate a formal `AmendmentHotfixRevision`, complete its lineage, register re-executable evidence, and have the validator confirm coverage (`okto_pulse_confirm_amendment_coverage`). Refinement or spec-revision authoring may produce the revisional artifact but does not satisfy the bug gate without amendment lineage + confirmed coverage. Leave the current validated spec content unchanged for simple Path A reuse. |
| `reason=unrelated_scenario` | The linked scenario exists on the bug spec but is not linked to the bug origin task or affected tasks | Do not use the unrelated scenario to satisfy the gate. Run `okto_pulse_resolve_bug_regression_scenarios` to find eligible candidates; if none exist, escalate Path B as semantic gap remediation with `semantic_gap_required=true` and `next_action=escalate_semantic_gap`. |
| `reason=cross_spec_scenario` | The linked test card references a scenario from another spec | Use a scenario on the bug spec that is eligible by origin/affected-task lineage. If cross-spec evidence is genuinely required, use Path B: back it with a formal amendment revision (see `missing_amendment_revision` below) — do NOT cross-link the bug directly. |
| `reason=missing_amendment_revision` | Cross-spec regression evidence with no formal `AmendmentHotfixRevision` backing this bug (a hotfix lane, label, or manual association does NOT satisfy Path B) | Path B step 1: `okto_pulse_create_amendment_revision` for the bug (binds to the bug's own `done`/`validated` spec, starts `draft`), or `okto_pulse_associate_amendment_revision_artifacts` onto an existing revision. Then complete lineage + evidence + validator coverage. There is no skip/override. |
| `reason=coverage_pending` | The amendment lineage is eligible but the **validator has not confirmed coverage** yet — re-executable evidence is necessary but NOT sufficient | The bug stays blocked (lineage eligible ≠ closure-ready). Register re-executable evidence on the regression test scenario, then the validator runs `okto_pulse_confirm_amendment_coverage` (the only writer of the non-forgeable `coverage_confirmed` signal). Do not attempt to skip. |
| `gate_bypass_not_allowed` | A `skip_gate`/`override_gate`/`bypass`/`force` (or equivalent) field was sent to an amendment surface | Remove it. MCP/API/UI only REMEDIATE the bug regression gate; they never skip or override it. Follow the Path B sequence instead. |
| `amendment_bug_mismatch` | The amendment revision does not belong to this bug/board (no free reparenting) | Use a revision created for THIS bug; list them with `okto_pulse_list_amendment_revisions`. `origin_bug_id`/`original_spec_id` are fixed at create and cannot be re-pointed. |
| `original_spec_not_done_or_locked` | Tried to create a Path B amendment for a spec that is NOT content-locked (`draft`, or `in_progress` without an active passed validation) | Path B amendments attach to a `done`/`validated` spec, OR an `in_progress` spec that is still **content-locked** — its `current_validation_id` points to a validation with `outcome=success` (a validated spec moved to in_progress for execution, which cannot be edited directly). If the spec is `in_progress` but NOT content-locked it is still editable, so edit it directly. A `failed`/`stale`/`superseded` validation is not a lock. |
| `bug_spec_mismatch` | `original_spec_id` passed to create does not match the bug's own spec | The amendment binds to the bug's spec. Omit `original_spec_id` (it defaults to the bug's spec) or pass the bug's spec id. |
| `invalid_initial_status` | Tried to create an amendment directly in a non-`draft` status (e.g. `approved`/`done`) | A new amendment MUST start as `draft`; advance it through the lifecycle (status changes never skip the gate). |
| `invalid_amendment_status` | `okto_pulse_transition_amendment_revision` got an unknown `status` | Use a real `AmendmentRevisionStatus` (`draft`/`review`/`approved`/`done`/`cancelled`/`superseded`). Unknown values are rejected fail-closed. |
| `invalid_lineage_state` | `okto_pulse_transition_amendment_revision` got an unknown `lineage_state` | Use `incomplete` or `complete`. Unknown values are rejected fail-closed. |
| `incomplete_lineage_artifacts` | Tried to set `lineage_state=complete` before the amendment has enough lineage | Declare at least one `regression_scenario_id`, one `regression_test_task_id` (via `okto_pulse_associate_amendment_revision_artifacts`) and the bug's authoritative origin task in `origin_task_ids`/`affected_task_ids`, then retry. |
| `cannot_promote_incomplete_lineage` | Tried to set `status=approved`/`done` while `lineage_state` is not `complete` | Complete the lineage first (`okto_pulse_transition_amendment_revision` `lineage_state=complete`), then promote the status. Promotion never confirms coverage — the bug stays `coverage_pending` until the validator runs `okto_pulse_confirm_amendment_coverage`. |
| `terminal_amendment_revision` | Tried to promote a `cancelled`/`superseded` revision back to `approved`/`done` | Terminal revisions cannot be resurrected. Create a NEW `okto_pulse_create_amendment_revision` for the bug instead. |
| `SpecLockedError` / `"spec is locked"` | Tried to edit a `validated`/`in_progress` spec to add regression coverage for a post-lock bug | For Path A, leave validated spec content unchanged. Reuse an existing scenario only when it is eligible by lineage: same spec and linked to the bug `origin_task_id` or an explicitly supplied affected task. Then create a post-bug `card_type="test"` task that references it. If no eligible scenario exists or expected behavior changed, remediate via a formal Path B `AmendmentHotfixRevision` (create/associate, complete lineage, register re-executable evidence, validator confirms coverage); refinement/spec-revision authoring alone does not satisfy the bug gate. The "after the bug" temporal applies to the test TASK (card), not the scenario. |
| `status_not_mutable` while updating a test scenario on a `validated`/`done` spec | The scenario is not linked to an executable test card, so the platform treats the update as arbitrary locked-spec mutation | For Path A/reconciliation, create or use a `card_type="test"` card on the same spec, link the scenario, move that card into `started`, `in_progress`, or `validation`, then call `okto_pulse_update_test_scenario_status` with structured evidence. Do not unlock the spec just to record operational test evidence. If there is no eligible existing scenario, use Path B/C instead. |
| `reason=sprint_required` / `reason=sprint_not_active` with `next_action=assign_hotfix_lane` or `activate_hotfix_lane` | Post-closure bug lacks an executable sprint lane | Use Path C: create or choose a `lane_type="hotfix"` sprint, assign the bug and regression test card to it, activate it, then retry. Keep the original closed sprint unchanged. |

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

Since spec d41c7209 (R3a), the migrated multi-value cluster returns a **uniform JSON envelope** `{"error": "invalid_multi_value_input", "detail": "<message>"}` instead of leaking a raw `ValueError` to the MCP transport (this closes the NC-3/G-2 leak). The `detail` field carries the messages below. Prefer a **native `list[str]`** to avoid all of these; the tool schema declares `anyOf [array-of-string, string]`.

| `detail` message | Cause | Fix |
|---|---|---|
| `"multi-value input must be a JSON array ... or pipe-separated ... — comma-separated input is rejected by REJECT policy"` | A comma-only string (e.g. multi-line prose with commas) was sent to a strict multi-value field | Send a **native list** `["a", "b"]`, a JSON-array string `'["a", "b"]'`, or pipe-separated `"a|b"`. Comma-only is ambiguous and rejected. |
| `"malformed JSON for multi-value param: ... (at pos N)"` | Input started with `[` so the JSON path was taken, but the JSON was invalid | Fix the JSON syntax (quoting, brackets). |
| `"malformed multi-value: expected list, got <type>"` | JSON decoded to a non-list (e.g. an object) | Send an array, not an object. |
| `"malformed multi-value: expected string items, got <type> at index N"` | JSON array had a non-string item | Every item must be a string. |

**Structured JSON fields** (`request_body_json`/`response_success_json`/`data_contract_json`/`payload_json` = `dict | str`; `response_errors_json` = `list[dict] | str`) accept a native `dict`/`list` or a legacy JSON-string and keep the `{"error": "Invalid <param>: <exc>"}` shape on a parse failure — see `okto-pulse://reference/multivalue`.

## KG Graph Availability Errors

These structured error keys appear in KG query responses when the embedded graph is in a degraded state. See the **Degraded-KG Fallback Rule** in `okto-pulse://workflows/kg` for the full protocol.

| Error key | Cause | Fix |
|---|---|---|
| `graph_unavailable` | The embedded LadybugDB graph is in a hard-reject state (`graph_state` is `recovery_needed` or `quarantined`). Returned by KG query tools (e.g. `okto_pulse_kg_get_learning_from_bugs`) when queries cannot be served. On a degraded board, `graph_unavailable` is the **expected** signal — do not retry in a loop. | Call `okto_pulse_kg_health(board_id)` to confirm `graph_state`. If degraded, follow the KG Health recovery flow (`okto-pulse://reference/kg-health`). This is an operator-driven path; the Degraded-KG Fallback Rule lets you proceed past the Stage 1 triad while the graph recovers. |
| `cognitive_status_unavailable` | The cognitive closeout gate could not confirm the cognitive consolidation status for a `done` transition because `graph_state` is `None` and no generation exists (the unconfirmed shape). This is a fail-closed signal: the gate cannot read the ledger and will not silently allow the transition. | Confirm board health via `okto_pulse_kg_health`. If the board's cognitive consolidation setting needs to be bypassed temporarily, enable `skip_cognitive_consolidation` in board settings. For full recovery, follow the KG Health recovery flow (`okto-pulse://reference/kg-health`). |
