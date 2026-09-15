# M-PULSE-4: frozen Grafx vector-parity contract

Status: pre-code contract, frozen on 2026-08-27.

Baselines:

- Pulse Community: `237f3bf7fa2a65a108db4f558932d429ec6696ce`
- Pulse Core: `ab61b9a785f2018312fc91541a580877fd068bbb`
- Grafx: `caa439b9847229ffbdde2f4c2a7046cd0f66b318`

This document fixes the complete acceptance surface of M-PULSE-4. Later findings
may correct a reproducible defect in this surface, but may not add exploratory
requirements to this milestone. M-PULSE-5 through M-PULSE-7, provider activation,
routing and cutover remain separate gates.

## 1. Existing ports remain unchanged

M-PULSE-4 does not add a Core port or DTO.

Board search remains:

```python
SemanticGraphStore.vector_search(
    board_id,
    node_type,
    query_vec,
    top_k,
    min_similarity,
    *,
    include_superseded=False,
    graph_layer="all",
) -> list[dict]
```

Its result dictionaries contain exactly `node_id`, `node_type`, `title`,
`source_artifact_ref`, `content`, `context`, `justification`, `kind_of` and
`similarity`.

Global Discovery search remains:

```python
GlobalDiscoveryRuntime.search_decision_digests(
    query_vector,
    *,
    board_ids,
    graph_layer,
    top_k,
    min_similarity,
    exhaustive=False,
) -> list[dict]
```

Its result dictionaries contain exactly `board_id`, `digest_id`, `id`, `title`,
`summary`, `node_type`, `graph_layer` and `similarity`. `exhaustive=True` means a
complete eligible scan, never an arbitrary first 500 rows.

No Grafx `Database`, `Transaction`, `RecordId`, vector result, regime, error or
configuration object may cross either adapter boundary.

## 2. Closed space map

Every space in this milestone has dimension 384, cosine metric,
`normalized=false` and `storage_dtype=float64`.

| Scope | Table and column | Grafx space |
|---|---|---|
| Board | `Decision.embedding` | `decision_embedding_idx` |
| Board | `Criterion.embedding` | `criterion_embedding_idx` |
| Board | `Constraint.embedding` | `constraint_embedding_idx` |
| Board | `Requirement.embedding` | `requirement_embedding_idx` |
| Board | `Entity.embedding` | `entity_embedding_idx` |
| Board | `APIContract.embedding` | `apicontract_embedding_idx` |
| Board | `TestScenario.embedding` | `testscenario_embedding_idx` |
| Board | `Bug.embedding` | `bug_embedding_idx` |
| Board | `Learning.embedding` | `learning_embedding_idx` |
| Global | `Board.summary_embedding` | `board_summary_idx` |
| Global | `Topic.centroid_embedding` | `topic_centroid_idx` |
| Global | `Entity.embedding` | `entity_embedding_idx` |
| Global | `DecisionDigest.embedding` | `digest_embedding_idx` |

The board manifest still has eleven physical spaces. `Alternative` and
`Assumption` remain physically indexed so M-PULSE-4 does not change the schema
fingerprint, but they are not searchable through `SemanticGraphStore`. The board
and Global namespaces are separate; the duplicate name `entity_embedding_idx`
must never resolve across bindings.

Only `DecisionDigest` has an existing Global Discovery search operation. The
other three Global spaces must be created, maintained and reported, but this
milestone does not invent public searches for them.

## 3. Score, filters and deterministic ordering

The public score is:

```text
clamp(cosine_similarity, 0.0, 1.0)
```

It is not `(cosine + 1) / 2`. `min_similarity` is inclusive and is applied to
the normalized score. This matters at zero: negative raw cosine scores normalize
to zero and remain eligible when `min_similarity == 0`.

Exact results use the following backend-independent total orders:

- board: `similarity DESC, node_id ASC`;
- Global Discovery: `similarity DESC, board_id ASC, digest_id ASC`.

Score comparisons use `abs_tol=1e-9` and `rel_tol=1e-9`. The exact oracle is a
standalone cosine calculation over the complete eligible fixture; Kuzu is not
an exact oracle because its current fallback is limited to 500 rows.

Board eligibility is fixed before final top-k:

- the requested type is one of the nine public vector types;
- the row belongs to the database selected once for `board_id`;
- `embedding` is non-null;
- `graph_layer` is `canonical`, `working` or `all`, with invalid values refused;
- superseded rows are excluded unless `include_superseded=True`;
- active-read tombstone/revocation reasons are always excluded.

Global eligibility is also fixed before final top-k:

- the digest is linked to a board in `board_ids`;
- `embedding` is non-null;
- `source_revoked` is null or false;
- the requested layer is `canonical`, `working` or `all`;
- a null/legacy layer is visible only through `all` and is projected as
  `legacy_unknown`.

An exact result is short only when fewer than `top_k` eligible rows satisfy the
threshold. An ANN page that underfills, or a normalized tie that crosses the
bounded cutoff, falls back to the complete exact path. The exact path reads the
eligible embeddings and computes cosine directly; it does not ask HNSW for every
candidate and call that exhaustive.

ANN is not required to return the exact IDs. It must meet the existing frozen
Grafx recall gate (mean recall@10 at least 0.90 for the calibrated 8,192 by 384,
256-query fixture) and return zero ineligible items. `vector_ef_search` and the
existing calibration remain unchanged in this milestone.

## 4. Index creation, status and rebuild

Creation uses existing DDL: a vector space is created before a table declares a
`VECTOR(space)` column, and Grafx attaches the corresponding index. Board
bootstrap remains the M-PULSE-3C implementation. M-PULSE-4 adds the closed Global
manifest and an idempotent, fail-before-write bootstrap for its four spaces and
the schema needed to maintain them.

Status uses existing `Database.vectors` and index views. A certifying adapter
must validate the exact name, dimension, metric, normalization, dtype, stale
state, stale reason and built-through position, correlate the vector and generic
index views, and use cold `verify("all")`. A process-local missing-target count is
diagnostic only and cannot certify a reopened index.

The one missing Grafx surface is a safe public rebuild. It is frozen as the
standalone operator door:

```python
database.maintenance.rebuild_vector_index(space: str) -> VectorIndexView
```

The operation owns a fresh write transaction. It resolves the vector index by
space, claims a durable stale generation, stages RESET plus the complete
authoritative heap-derived contents through its fixed target LSN, commits, and
returns only after the same generation is durably healthy. The existing rebuild
generation token must reject a concurrent superseding commit. A pre-barrier
failure or rollback leaves the index stale; an ambiguous/post-barrier failure is
recovered and cold-verified before readiness may be claimed. Read-only, closed,
unknown, non-vector and unattached targets fail with public typed errors.

The Pulse adapter must never call private `IndexManager` APIs. `GraphLifecycle.rebuild`
is not reused because that method recycles a graph handle and does not repair an
index.

## 5. Inactive M-PULSE-4 deliverables

The implementation consists only of:

1. a board vector-search adapter over a `(board_id -> Database)` resolver;
2. a Global vector schema/bootstrap and vector-search helper over its separate
   database binding;
3. the Grafx public vector-index rebuild door;
4. deterministic tie and complete-underfill corrections in the Kuzu reference
   adapters, without changing their ports;
5. the finite test matrix below.

These helpers remain inactive in `kg.py` and `composition.py`. The coherent
provider bundle and every other Global runtime method belong to M-PULSE-6.

Global writes covered here must update the four vector columns when the port
supplies a replacement value. The new implementation must not copy the current
Ladybug limitation that leaves `Board.summary_embedding` and
`DecisionDigest.embedding` stale after an upsert.

## 6. Finite acceptance matrix

| Case | Coverage | States | Acceptance |
|---|---|---|---|
| V1 | all 13 mapped spaces | cold, repeat, reopen | exact create; repeat is zero-write; status ready; only nine board types searchable |
| V2 | all 13 mappings | cold exact | independent oracle IDs, total order and scores within the frozen tolerances |
| V3 | `Decision` | warm exact | layer, nullable vector, supersedence, every active-read tombstone and inclusive threshold |
| V4 | `DecisionDigest` | warm exact | multi-board ACL, layer/null, revoked, empty board list and complete exhaustive fallback |
| V5 | `Decision` and `DecisionDigest` | cold, warm, reopen ANN | mean recall@10 >= 0.90 and no ineligible result |
| V6 | same two representatives | churn, reopen | insert, update, vector-to-null, null-to-vector, delete and filter changes reflected after commit |
| V7 | same two representatives | stale, rebuild, reopen | stale refuses; failed rebuild never certifies; committed rebuild plus `verify("all")` is ready cold |

There is deliberately no 13-by-filters-by-lifecycle Cartesian product. V1 and
V2 prove every mapping; V3 through V7 exercise one board and one Global
representative. Existing generic Grafx vector tests remain regression evidence
for concurrency, generation fencing and HNSW publication.

The two non-public board indexes are measured once for build time and persisted
size and are retained for `0.0.1`. M-PULSE-4 adds no latency or size SLO and does
not add a physical-index opt-out.

The completed one-time measurement, exact command, source pins, physical sizes,
and no-SLO boundary are recorded in
`docs/evidence/M4_VECTOR_RECALL_EVIDENCE.md` and
`docs/evidence/m4-non-public-vector-indexes.json`.

## 7. Explicit non-goals

- no Core port or DTO change;
- no provider activation, routing, cutover or dual write;
- no public Board, Topic or Entity Global vector query;
- no removal of `Alternative` or `Assumption` indexes;
- no float32 migration, metric change or physical format change;
- no new performance SLO or `ef_search` tuning;
- no vector read-your-own-writes; Pulse vector reads occur outside the staged
  graph transaction and a direct dirty-table search remains a typed refusal;
- no export/import, backup or portable recovery work from M-PULSE-5.
