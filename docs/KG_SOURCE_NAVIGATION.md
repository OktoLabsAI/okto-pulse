# Open a graph node's source entity

KG node previews, node details and Global Discovery share **Source entity**.
The panel shows the owning artifact's type and title, with **Open source**;
it is available before **Show more**. The raw provenance remains expandable
under **Original source reference**.

Opening the source uses the existing Spec, Refinement, Ideation, Story, Sprint
or Card modal (including task, test and bug cards). **Back** returns to the node;
closing the modal stack leaves the graph selection or Discovery search intact.
Discovery uses the result's board, not the board currently selected in the app.
Nested artifact navigation inherits that board unless explicitly overridden.

## Provenance resolution

| Recorded source | Opened entity |
| --- | --- |
| `spec:<id>` or `spec:<id>:<child-type>:<child-id>` | Owning Spec, not a URL made from the full composite reference |
| `refinement`, `ideation`, `story`, `sprint` with an ID | Corresponding artifact |
| `card`, `task`, `test`, `bug` with an ID; governed `card:bug:<uuid>` alias | Card modal, with the actual card kind |
| `code_investigation_receipt:<id>` | Receipt's subject artifact |
| `code_evidence:<id>` | Evidence's parent artifact |
| `implementation_target:<id>` | Target's card |

Technical references without a supported owner are not guessed from titles,
edges or opaque code `source_ref` values. Missing, deleted, inaccessible and
out-of-board entities have no navigation button. For indirect records with a
version, the panel shows the recorded version and explicitly opens the **current**
entity; it does not claim to render an immutable historical revision.

## Read-only API contract

`GET /api/v1/kg/boards/{board_id}/nodes/{node_id}/source`

```json
{
  "status": "resolved",
  "source_artifact_ref": "spec:spec-id:decision:decision-id",
  "target": {
    "board_id": "board-id",
    "entity_type": "spec",
    "entity_kind": "spec",
    "entity_id": "spec-id",
    "title": "Owning specification",
    "source_version": null
  }
}
```

- `status`: `resolved`, `missing_source`, `unsupported` or `unavailable`.
- `target` is null except on successful resolution. Removed, forbidden and
  wrong-board owners share `unavailable`; their titles/IDs are not returned.
- The route authorizes the board's graph read and uses the node-detail Code
  Traceability visibility gate. Hidden/missing nodes return 404. It accepts no
  caller-supplied source reference.
- Resolution separately checks board/realm access, the owning entity's read
  permission and, for indirect Code Traceability sources, its read permissions.
  Indirect records and owners must belong to the requested board.
- Graph/backend failures use existing error responses, not an invented empty
  result. The UI exposes a manual retry, without an unbounded retry loop.

The Core use case `ResolveKGNodeSourceUseCase` uses existing relational ports;
the Community route provides graph access. No Grafx-specific import, policy or
configuration is added to the Core. No migration, setting, consolidation or
rebuild is required.

Resolution happens only for an opened node panel, not for every node in a graph
page. Reads run through the existing blocking-I/O boundary. Old requests are
aborted/ignored when changing node or board, so a late response cannot open an
artifact from a previous scope.
While Code Traceability permissions load, the node modal waits without displaying
protected content. A pending authorization lookup is not treated as a denial;
an actual denial still closes the protected detail.

## Regression coverage

- Core: direct/composite aliases, indirect owners, versions, missing/unsupported
  references, board/realm and permission boundaries, and propagation of runtime
  failures (`tests/test_kg_node_source.py`).
- Community: REST/OpenAPI envelope, hidden nodes, indirect resolution,
  cross-board denial and off-event-loop graph reads
  (`tests/test_kg_node_source_route.py`).
- Frontend: all owner modal kinds, indirect card kinds, stale-response abortion,
  retries, Escape/Back, result-board routing and nested modal board inheritance
  (`NodeSourceLink`, `GlobalSearchView`, `ModalStackRenderer` tests).

### Local validation — 2026-09-14

The scoped regression passed: 116 Core source/authorization tests, 6 Core import
boundary tests, 24 Community source/visibility/read-dispatch tests and 279 frontend
KG/modal tests. The production build, packaged frontend verification and KG Health
browser regression also passed. This is the affected-surface regression, not a
claim that every Pulse test was rerun.

Real-data browser checks (read-only navigation):

- Global Discovery opened an Okto Pulse Spec from another active board, with the
  Spec modal's requests scoped to Okto Pulse. Back/Escape preserved the query.
- A `code_evidence` result resolved to its Refinement and displayed its recorded
  version; a test-card result opened the existing Card modal.
- An E2E receipt opened its Refinement directly from the graph preview without
  Show more. Back/Escape retained the selected node and preview. The graph check
  used the accessible non-WebGL renderer; Discovery used the regular UI.
- Cold Code Traceability authority exposed a premature-close race. The detail
  now waits without showing protected fields; loading-to-allow and loading-to-deny
  regressions cover both outcomes.
  The final installed production bundle was also checked with a deliberately
  delayed real permissions response: the modal stayed open without source content,
  then resolved the Refinement after authorization completed.

No test triggered consolidation, rebuild or source edits. Grafx remained 0.0.6;
this change is Pulse navigation and adapter-neutral provenance resolution.
