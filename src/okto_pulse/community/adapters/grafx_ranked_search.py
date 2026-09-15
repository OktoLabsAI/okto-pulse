"""Opt-in native lexical/hybrid retrieval with same-snapshot Pulse visibility."""

from contextlib import nullcontext
import math
from time import monotonic

from okto_grafx import HybridSearchOptions, TextIndexOptions, TextSearchLimits
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.errors import GrafxQueryBudgetExceeded, GrafxQueryDeadlineExceeded
from okto_pulse.core.kg import cypher_templates as tpl
from okto_pulse.core.domain.code_traceability_kg import CODE_TRACEABILITY_KG_SUBTYPES
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.interfaces.ranked_graph_search import RankedGraphQuery
from okto_pulse.core.kg.schema_contract import NODE_TYPES

from okto_pulse.community.adapters.grafx_board_vector_search import _SPACE_BY_NODE_TYPE
from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_observations import mapped

_COLUMNS = (
    "id",
    "title",
    "source_artifact_ref",
    "source_confidence",
    "graph_layer",
    "superseded_by",
    "revocation_reason",
    "kind_of",
)
_FIELDS = ("title", "content")
_INDEX_OPTIONS = TextIndexOptions(field_weights=(2.0, 1.0), statistics_mode="durable")
_FILTER_PAYLOAD_BYTES = 16 * 1024 * 1024


def _node_type(value):
    if type(value) is not str or value not in NODE_TYPES:
        raise ValueError("invalid_node_type")
    return value


def text_index_name(node_type):
    return f"pulse_text_v1_{_node_type(node_type)}"


def _missing(node_type):
    return GraphCapabilityUnavailable(
        "Ranked graph search requires explicit index preparation.",
        details={"reason": "ranked_search_not_ready", "node_type": node_type},
    )


def _request(request):
    if type(request) is not RankedGraphQuery:
        raise ValueError("ranked_query_required")
    _node_type(request.node_type)
    if request.mode not in {"text", "hybrid"} or request.graph_layer not in {
        "all",
        "canonical",
        "working",
    }:
        raise ValueError("invalid_ranked_search_mode_or_layer")
    if (
        type(request.query) is not str
        or not request.query.strip()
        or len(request.query) > 4096
    ):
        raise ValueError("ranked_query_requires_1_to_4096_characters")
    for key, maximum in (
        ("limit", 200),
        ("candidate_limit", 1000),
        ("max_filter_rows", 100_000),
    ):
        if (
            type(getattr(request, key)) is not int
            or not 1 <= getattr(request, key) <= maximum
        ):
            raise ValueError(f"invalid_{key}")
    if request.limit > request.candidate_limit:
        raise ValueError("limit_exceeds_candidate_limit")
    for key in ("include_superseded", "include_code_traceability", "phrase"):
        if type(getattr(request, key)) is not bool:
            raise ValueError(f"invalid_{key}")
    for key, lower, upper in (("min_confidence", 0, 1), ("timeout_seconds", 0.001, 30)):
        value = getattr(request, key)
        if (
            type(value) not in (float, int)
            or not math.isfinite(value)
            or not lower <= value <= upper
        ):
            raise ValueError(f"invalid_{key}")
    if request.mode == "hybrid":
        if request.phrase:
            raise ValueError("phrase_requires_text_mode")
        if (
            type(request.vector) is not tuple
            or len(request.vector) != 384
            or any(
                type(x) not in (int, float) or not math.isfinite(x)
                for x in request.vector
            )
        ):
            raise ValueError("hybrid_requires_finite_384_vector")
        if request.node_type not in _SPACE_BY_NODE_TYPE:
            raise GraphCapabilityUnavailable(
                "This node type does not support vector retrieval."
            )
    elif request.vector:
        raise ValueError("text_mode_does_not_consume_a_vector")
    return request


class CommunityGrafxRankedSearch:
    def __init__(
        self, database_resolver, revalidate_fence, *, read_database_scope=None
    ):
        self._resolve = database_resolver
        self._fence = revalidate_fence
        self._read_scope = read_database_scope or (
            lambda board: nullcontext(database_resolver(board))
        )

    @staticmethod
    def _ready(database, node_type):
        indexes = {index.name: index for index in database.indexes.indexes()}
        name = text_index_name(node_type)
        if name not in indexes:
            return False
        # Do not accept a same-name foreign/manual index with different semantics.
        entry = indexes[name]
        if entry.stale:
            raise GraphCapabilityUnavailable(
                "Ranked search index requires explicit maintenance."
            )
        definition = database.catalog.catalog.table(node_type, kind="node")
        if (
            entry.table_id != definition.table_id
            or tuple(entry.columns) != _FIELDS
            or entry.key_derivation != _INDEX_OPTIONS.derivation()
        ):
            raise GraphCapabilityUnavailable(
                "Ranked search index definition differs from its contract."
            )
        return True

    @mapped
    def readiness(self, board_id, node_type):
        _node_type(node_type)
        with self._read_scope(board_id) as db:
            return {
                "supported": True,
                "ready": self._ready(db, node_type),
                "node_type": node_type,
                "modes": ["text", "hybrid"]
                if node_type in _SPACE_BY_NODE_TYPE
                else ["text"],
            }

    def prepare(self, board_id, node_type, *, reason):
        _node_type(node_type)
        if type(reason) is not str or not reason.strip() or len(reason) > 1024:
            raise ValueError("preparation_requires_a_bounded_audit_reason")
        self._fence(board_id, "ranked_search_prepare")
        db = self._resolve(board_id)
        try:
            if not self._ready(db, node_type):
                db.create_text_index(
                    text_index_name(node_type),
                    node_type,
                    _FIELDS,
                    options=_INDEX_OPTIONS,
                )
            self._fence(board_id, "ranked_search_prepare_complete")
            return {
                "supported": True,
                "ready": True,
                "node_type": node_type,
                "reason": reason,
            }
        except Exception as exc:
            error = map_grafx_error(exc, operation="ranked_search_prepare")
            if error is exc:
                raise
            raise error from exc

    def search(self, board_id, request):
        request = _request(request)
        deadline = monotonic() + request.timeout_seconds

        def remaining():
            left = deadline - monotonic()
            if left <= 0:
                raise GrafxQueryDeadlineExceeded("Ranked search deadline exceeded.")
            return left

        try:
            with self._read_scope(board_id) as db, db.begin("read") as reader:
                if not self._ready(db, request.node_type):
                    raise _missing(request.node_type)
                # Only projected policy/payload fields, never retained embeddings.
                # Native filters require physical IDs; bound the materialization
                # explicitly rather than post-filtering an already truncated top-k.
                allowed, payloads, scanned, cursor = [], {}, 0, None
                retained_bytes = 0
                while True:
                    page = reader.scan_rows_v1(
                        request.node_type,
                        kind="node",
                        columns=_COLUMNS,
                        limit=min(256, request.max_filter_rows + 1 - scanned),
                        cursor=cursor,
                        max_batch_bytes=4 * 1024 * 1024,
                        timeout_seconds=remaining(),
                    )
                    for row in page.rows:
                        scanned += 1
                        if scanned > request.max_filter_rows:
                            raise GrafxQueryBudgetExceeded(
                                "Ranked filter materialization budget exceeded.",
                                resource="pulse_ranked_filter",
                            )
                        values = dict(zip(_COLUMNS, row.values, strict=True))
                        if not tpl.is_visible_in_active_reads(
                            values["revocation_reason"]
                        ):
                            continue
                        if (
                            not request.include_superseded
                            and values["superseded_by"] is not None
                        ):
                            continue
                        if (
                            request.graph_layer != "all"
                            and values["graph_layer"] != request.graph_layer
                        ):
                            continue
                        confidence = values["source_confidence"]
                        if confidence is None or confidence < request.min_confidence:
                            continue
                        if (
                            not request.include_code_traceability
                            and str(values["kind_of"] or "")
                            in CODE_TRACEABILITY_KG_SUBTYPES
                        ):
                            continue
                        retained_bytes += 128 + sum(
                            64 + 4 * len(value) if isinstance(value, str) else 32
                            for value in values.values()
                        )
                        if retained_bytes > _FILTER_PAYLOAD_BYTES:
                            raise GrafxQueryBudgetExceeded(
                                "Ranked visibility payload bound exceeded.",
                                resource="pulse_ranked_filter_bytes",
                            )
                        allowed.append(row.record_id)
                        payloads[row.record_id] = values
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                permitted = RecordIdFilter.of(allowed)
                limits = TextSearchLimits(max_candidates=request.max_filter_rows)
                if request.mode == "text":
                    result = db.search_text(
                        reader,
                        index=text_index_name(request.node_type),
                        query=request.query,
                        k=request.limit,
                        filter=permitted,
                        limits=limits,
                        timeout_seconds=remaining(),
                        phrase=request.phrase,
                    )
                    scores = [
                        (hit.record_id, hit.score, hit.score, None)
                        for hit in result.hits
                    ]
                    lexical, vector, ranking = result.regime, "disabled", "bm25"
                else:
                    result = db.search_hybrid(
                        reader,
                        table=request.node_type,
                        index=text_index_name(request.node_type),
                        query=request.query,
                        space=_SPACE_BY_NODE_TYPE[request.node_type],
                        vector=request.vector,
                        k=request.limit,
                        filter=permitted,
                        options=HybridSearchOptions(
                            candidate_k=request.candidate_limit
                        ),
                        text_limits=limits,
                        timeout_seconds=remaining(),
                    )
                    scores = [
                        (hit.record_id, hit.score, hit.lexical_score, hit.vector_score)
                        for hit in result.hits
                    ]
                    lexical, vector, ranking = (
                        result.lexical_regime,
                        result.vector_regime,
                        result.fusion,
                    )
                remaining()
                return {
                    "hits": [
                        dict(
                            node_id=payloads[rid]["id"],
                            node_type=request.node_type,
                            title=payloads[rid]["title"],
                            score=score,
                            lexical_score=text,
                            vector_score=vec,
                        )
                        for rid, score, text, vec in scores
                    ],
                    "mode": request.mode,
                    "ranking": ranking,
                    "snapshot": str(result.snapshot_commit),
                    "lexical_regime": lexical,
                    "vector_regime": vector,
                    "filter_rows_scanned": scanned,
                    "complete": True,
                }
        except Exception as exc:
            error = map_grafx_error(exc, operation="ranked_graph_search")
            if error is exc:
                raise
            raise error from exc
