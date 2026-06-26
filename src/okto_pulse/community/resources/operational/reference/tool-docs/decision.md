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
    title: Decision title (e.g. "Use LadybugDB embedded over Neo4j")
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

## `okto_pulse_migrate_spec_decisions`

One-shot migrator: extract "## Decisions" markdown bullets from spec.context
into structured spec.decisions[] entries, then remove the block from context.

Idempotent — running twice on a migrated spec is a no-op. Existing
decisions are preserved; only the markdown-sourced ones are added, and
duplicates (same title) are skipped.

Args:
    board_id: Board ID
    spec_id: Spec ID

Returns:
    JSON with migration summary (decisions_added, context_modified)

## `okto_pulse_remove_decision`

Remove a Decision (soft-delete: status becomes "revoked").

Preserves history so the KG still surfaces the decision with its
revocation reason. Use okto_pulse_update_decision with status=active to
restore.

Args:
    board_id: Board ID
    spec_id: Spec ID
    decision_id: Decision ID ("dec_...")

Returns:
    JSON confirmation

## `okto_pulse_update_decision`

Update an existing Decision. Only non-empty fields are changed; pass "CLEAR"
to wipe optional string/list fields.

Args:
    board_id: Board ID
    spec_id: Spec ID
    decision_id: Decision ID ("dec_...")
    title: New title (optional)
    rationale: New rationale (optional)
    context: New context (optional, "CLEAR" to remove)
    alternatives_considered: Pipe-separated list (optional, "CLEAR" to remove)
    supersedes_decision_id: New target Decision id, or "CLEAR" to unset
    linked_requirements: Pipe-separated requirement refs. Accepted forms:
        FR index/fr_id/text and structured TR id/text. Pass "CLEAR" to empty.
    notes: Notes (optional, "CLEAR" to remove)
    status: One of "active", "superseded", "revoked" (optional)

Returns:
    JSON with updated decision
