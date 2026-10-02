---
version: "1.0"
---

# Tool docs — `decision`

Full long-form documentation (args, returns, examples, enum prose) for `okto_pulse_*` tools in this family. The `tools/list` surface carries only the compact summary; read here on demand.

## `okto_pulse_add_decision`

Add a formalized Decision to a spec.

A Decision records a contextual CHOICE — the reasoning behind picking one
path over alternatives. Different from BusinessRule (which is a NORM, a
prescriptive "DEVE" statement): use a Decision to capture design
intent, tradeoffs, or team consensus. The KG extracts Decisions into
queryable nodes, and the optional coverage gate (opt-in) can require each
Decision to have ≥1 linked task.

Args:
    board_id: Board ID
    spec_id: Spec ID
    title: Decision title (e.g. "Use Okto Grafx embedded over Neo4j")
    rationale: Why this choice was made
    context: When/where this applies (optional)
    alternatives_considered: Pipe-separated list of alternatives (e.g. "Neo4j|DuckDB")
    supersedes_decision_id: id of another Decision this one replaces; it auto-moves to status=superseded
    linked_requirements: Pipe-separated requirement refs. Accepted forms:
        FR index/fr_id/text and structured TR id/text. Persisted values are
        canonical ids when the write path resolves them.
    notes: Additional notes

Returns:
    JSON with created decision and spec coverage snapshot


## `okto_pulse_remove_spec_entity` (`target_type="decision"`)

Remove a Decision (soft-delete: status becomes "revoked").

Preserves history so the KG still surfaces the decision with its
revocation reason. Use okto_pulse_update_spec_entity with entity_type=decision and operation=restore to
restore.

Args:
    board_id: Board ID
    spec_id: Spec ID
    target_type: decision
    entity_id: Decision ID ("dec_...")

Returns:
    JSON confirmation

## `okto_pulse_update_spec_entity` — decision

Use `entity_type="decision", entity_id`, `operation="update"` and an object `payload_json` containing the fields to change. Omit unchanged fields; use JSON null to clear optional fields. Use exact same-Spec IDs for links.
Use the explicit `restore`, `revoke` or `supersede` operation for lifecycle changes. Existing granular permissions and impact checks apply.
