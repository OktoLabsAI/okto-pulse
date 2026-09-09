"""M6-B: the Grafx read-only executor envelope and the scope's execute.

Focused on what this layer actually decides: the Pulse envelope, one snapshot
for the paired read, the `_NODES`/`_RELS` conversion and nothing beyond it, and
that a statement Core cannot prove read-only revalidates the fence before it
runs. The query grammar belongs to Core and is not re-tested here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import okto_grafx
import pytest
from okto_grafx.errors import GrafxLeaseTimeout
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult

from okto_pulse.community.adapters.grafx_cypher_executor import (
    CommunityGrafxCypherExecutor,
    project_path_sequences,
    pulse_value,
    statement_is_write,
    statement_kind,
)

BOARD_ID = "grafx-executor-board"
PATH_QUERY = "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN path"


def test_scalar_page_parameter_conversion_keeps_nested_datetime_and_detachment():
    from okto_pulse.community.adapters.grafx_graph_transaction import _grafx_query_parameters

    ids = [f"id-{i}" for i in range(500)]
    source = {"ids": ids, "nested": [{"at": datetime(2026, 9, 1, tzinfo=UTC)}],
              "scalars": (None, True, 1, 1.5)}
    converted = _grafx_query_parameters(source)
    assert converted["ids"] == ids
    assert converted["ids"] is not ids
    assert isinstance(converted["nested"][0]["at"], okto_grafx.Timestamp)
    assert converted["scalars"] == source["scalars"]
    ids.append("later")
    assert len(converted["ids"]) == 500


class _RecordingFence:
    """Records every fence revalidation so a test can name the phase."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, board_id: str, phase: str) -> None:
        self.calls.append((board_id, phase))


@pytest.fixture
def grafx_database(tmp_path: Path) -> Any:
    database = okto_grafx.connect(tmp_path / "grafx-board", page_size=8192)
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Decision("
            "id STRING, title STRING, created_at TIMESTAMP, PRIMARY KEY(id))"
        )
        schema.execute(
            "CREATE REL TABLE supersedes__Decision__Decision(FROM Decision TO Decision)"
        )
    with database.begin("write") as writer:
        writer.execute(
            "CREATE (:Decision {id: 'd1', title: 'first', "
            "created_at: timestamp('2026-08-28 01:02:03.456789')})"
        )
        writer.execute("CREATE (:Decision {id: 'd2', title: 'second'})")
        writer.execute(
            "MATCH (a:Decision {id:'d1'}), (b:Decision {id:'d2'}) "
            "CREATE (a)-[:supersedes__Decision__Decision]->(b)"
        )
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def executor(grafx_database: Any) -> CommunityGrafxCypherExecutor:
    def resolve(board_id: str) -> Any:
        if board_id != BOARD_ID:
            raise KeyError(board_id)
        return grafx_database

    return CommunityGrafxCypherExecutor(resolve)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a:Decision)-[:supersedes]->(b:Decision) RETURN a.id, b.id",
        "MATCH (b:Decision)<-[:supersedes]-(a:Decision) RETURN a.id, b.id",
        "MATCH (a:Decision) OPTIONAL MATCH (a)-[:supersedes]->(b) "
        "RETURN a.id, b.id ORDER BY a.id",
    ],
)
def test_logical_relationship_read_reaches_the_physical_table(executor, query):
    rows = executor.execute_read_only(BOARD_ID, query)["rows"]
    assert ["d1", "d2"] in rows
    if "OPTIONAL" in query:
        assert rows == [["d1", "d2"], ["d2", None]]
    else:
        assert rows == [["d1", "d2"]]


def test_paired_logical_relationship_reads_are_translated(executor):
    query = "MATCH (a:Decision)-[:supersedes]->(b:Decision) RETURN a.id, b.id"
    pair = executor.execute_read_only_pair(BOARD_ID, query, query)
    assert pair["primary"]["rows"] == [["d1", "d2"]]
    assert pair["comparison"]["rows"] == [["d1", "d2"]]


def test_optional_logical_single_table_with_other_node_type_keeps_zero_result(
    executor, grafx_database
):
    with grafx_database.begin("write") as writer:
        writer.execute("CREATE NODE TABLE Entity(id STRING, PRIMARY KEY(id))")
        writer.execute(
            "CREATE REL TABLE contradicts__Decision__Decision(FROM Decision TO Decision)"
        )
        writer.execute("CREATE (:Entity {id:'e1'})")
    query = (
        "MATCH (n:Entity) OPTIONAL MATCH (n)<-[c:contradicts]-() RETURN n.id, COUNT(c)"
    )
    assert executor.execute_read_only(BOARD_ID, query)["rows"] == [["e1", 0]]


def test_transient_reader_lane_timeout_retries_on_the_next_participant() -> None:
    resolutions: list[str] = []

    class BusyLane:
        def execute(self, _query: str, _params: object) -> object:
            raise GrafxLeaseTimeout("reader lane is busy")

    class HealthyLane:
        def execute(self, _query: str, _params: object) -> object:
            return type("Result", (), {"columns": ("value",), "rows": ((7,),)})()

    lanes = iter((BusyLane(), HealthyLane()))

    def resolve(board_id: str) -> Any:
        resolutions.append(board_id)
        return next(lanes)

    envelope = CommunityGrafxCypherExecutor(resolve).execute_read_only(
        BOARD_ID,
        "RETURN 7 AS value",
    )

    assert envelope["rows"] == [[7]]
    assert resolutions == [BOARD_ID, BOARD_ID]


class TestThePulseEnvelope:
    def test_a_read_returns_every_declared_field(self, executor) -> None:
        envelope = executor.execute_read_only(
            BOARD_ID, "MATCH (d:Decision) RETURN d.id"
        )

        assert set(envelope) == {
            "rows",
            "columns",
            "row_count",
            "truncated",
            "execution_time_ms",
        }
        assert isinstance(envelope["rows"], list)
        assert all(isinstance(row, list) for row in envelope["rows"])
        assert isinstance(envelope["columns"], list)
        assert envelope["row_count"] == len(envelope["rows"])
        assert envelope["truncated"] is False
        assert isinstance(envelope["execution_time_ms"], float)
        assert {row[0] for row in envelope["rows"]} == {"d1", "d2"}

    def test_the_row_bound_is_respected(self, executor) -> None:
        envelope = executor.execute_read_only(
            BOARD_ID, "MATCH (d:Decision) RETURN d.id", max_rows=1
        )

        # Core injects the terminal LIMIT, so the engine never returns more
        # than the bound and truncation stays the defensive path below.
        assert envelope["row_count"] == 1
        assert envelope["truncated"] is False

    def test_truncation_reports_itself_when_a_backend_overruns(self) -> None:
        class _Overrun:
            columns = ("value",)
            rows = (("a",), ("b",), ("c",))

        envelope = CommunityGrafxCypherExecutor._envelope(
            _Overrun(), max_rows=2, started=0.0
        )

        assert envelope["truncated"] is True
        assert envelope["row_count"] == 2

    def test_a_timestamp_is_normalized_for_pulse(self, executor) -> None:
        envelope = executor.execute_read_only(
            BOARD_ID, "MATCH (d:Decision {id: 'd1'}) RETURN d.created_at"
        )

        rendered = envelope["rows"][0][0]
        assert isinstance(rendered, str)
        assert rendered.endswith("Z")
        assert rendered.startswith("2026-08-28T01:02:03")

    def test_a_datetime_parameter_is_normalized_for_grafx(self, executor) -> None:
        envelope = executor.execute_read_only(
            BOARD_ID,
            "MATCH (d:Decision) WHERE d.created_at < $cursor_ts RETURN d.id",
            {"cursor_ts": datetime(2026, 9, 1, tzinfo=UTC)},
        )

        assert envelope["rows"] == [["d1"]]

    def test_is_supported(self, executor) -> None:
        assert executor.is_supported() is True


class TestTheTwoOPathConversion:
    def test_the_path_sequences_come_back_as_lists(self, executor) -> None:
        envelope = executor.execute_read_only(BOARD_ID, PATH_QUERY)

        assert envelope["columns"] == ["path"]
        path = envelope["rows"][0][0]
        assert isinstance(path, dict)
        assert isinstance(path["_NODES"], list)
        assert isinstance(path["_RELS"], list)
        assert [node["id"] for node in path["_NODES"]] == ["d1", "d2"]
        assert path["_RELS"][0]["_LABEL"] == "supersedes__Decision__Decision"
        # The opaque identities inside stay exactly as the engine gave them.
        assert set(path["_NODES"][0]["_ID"]) == {"offset", "table"}

    def test_the_injected_limit_stays_engine_side(self, tmp_path: Path) -> None:
        database = okto_grafx.connect(
            tmp_path / "bounded-path",
            page_size=8192,
            max_result_rows=1,
        )
        try:
            with database.begin("write") as schema:
                schema.execute("CREATE NODE TABLE Decision(id STRING, PRIMARY KEY(id))")
                schema.execute(
                    "CREATE REL TABLE supersedes__Decision__Decision(FROM Decision TO Decision)"
                )
            with database.begin("write") as writer:
                for value in ("d1", "d2", "d3"):
                    writer.execute(f"CREATE (:Decision {{id: '{value}'}})")
                for source, target in (("d1", "d2"), ("d2", "d3")):
                    writer.execute(
                        f"MATCH (a:Decision {{id: '{source}'}}), "
                        f"(b:Decision {{id: '{target}'}}) "
                        "CREATE (a)-[:supersedes__Decision__Decision]->(b)"
                    )

            bounded = CommunityGrafxCypherExecutor(
                lambda _board_id: database
            ).execute_read_only(BOARD_ID, PATH_QUERY, max_rows=1)

            assert bounded["row_count"] == 1
            assert bounded["truncated"] is False
        finally:
            database.close()

    def test_only_those_two_keys_change_shape(self) -> None:
        value = {
            "_NODES": (1, 2),
            "_RELS": (3,),
            "other": (4, 5),
            "nested": {"_NODES": (6,), "keep": (7,)},
        }

        projected = project_path_sequences(value)

        assert projected["_NODES"] == [1, 2]
        assert projected["_RELS"] == [3]
        assert projected["nested"]["_NODES"] == [6]
        # Every other tuple is a tuple in the contract and stays one.
        assert projected["other"] == (4, 5)
        assert isinstance(projected["other"], tuple)
        assert isinstance(projected["nested"]["keep"], tuple)

    def test_a_bare_tuple_value_is_untouched(self) -> None:
        assert isinstance(pulse_value((1, 2)), tuple)
        assert pulse_value((1, 2)) == (1, 2)


class TestThePairedReadSharesOneSnapshot:
    def test_both_windows_are_returned(self, executor) -> None:
        paired = executor.execute_read_only_pair(
            BOARD_ID,
            "MATCH (d:Decision) WHERE d.id = 'd1' RETURN d.id",
            "MATCH (d:Decision) RETURN d.id",
        )

        assert set(paired) == {"primary", "comparison"}
        assert paired["primary"]["row_count"] == 1
        assert paired["comparison"]["row_count"] == 2
        assert paired["primary"]["columns"] == ["d.id"]

    def test_the_pair_opens_exactly_one_snapshot(self, grafx_database) -> None:
        # The comparison exists to measure what the canonical filter hides, so
        # the two windows must differ by layer and never by time. One snapshot
        # is what makes that true, and a second begin would break it.
        opened: list[str] = []

        class _CountingDatabase:
            def __init__(self, delegate: Any) -> None:
                self._delegate = delegate

            def transaction(self, mode: str, *args: Any, **kwargs: Any) -> Any:
                opened.append(mode)
                return self._delegate.transaction(mode, *args, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._delegate, name)

        executor = CommunityGrafxCypherExecutor(
            lambda _board: _CountingDatabase(grafx_database)
        )
        paired = executor.execute_read_only_pair(
            BOARD_ID,
            "MATCH (d:Decision) WHERE d.id = 'd1' RETURN d.id",
            "MATCH (d:Decision) RETURN d.id",
        )

        assert opened == ["read"]
        assert paired["primary"]["row_count"] == 1
        assert paired["comparison"]["row_count"] == 2


class TestTheBatchedReadSharesOneSnapshot:
    def test_all_statements_use_one_snapshot(self, grafx_database) -> None:
        opened: list[str] = []

        class _CountingDatabase:
            def transaction(self, mode: str, *args: Any, **kwargs: Any) -> Any:
                opened.append(mode)
                return grafx_database.transaction(mode, *args, **kwargs)

        executor = CommunityGrafxCypherExecutor(lambda _board: _CountingDatabase())

        results = executor.execute_read_only_batch(
            BOARD_ID,
            [
                ("MATCH (d:Decision) WHERE d.id = 'd1' RETURN d.id", None, 10),
                ("MATCH (d:Decision) RETURN d.id", None, 10),
            ],
        )

        assert opened == ["read"]
        assert results[0]["row_count"] == 1
        assert results[1]["row_count"] == 2

    def test_every_statement_is_validated_before_resolving_the_database(self) -> None:
        resolved = False

        def resolve(_board_id: str) -> Any:
            nonlocal resolved
            resolved = True
            raise AssertionError("the database must not be resolved")

        executor = CommunityGrafxCypherExecutor(resolve)

        with pytest.raises(Exception):
            executor.execute_read_only_batch(
                BOARD_ID,
                [
                    ("MATCH (d:Decision) RETURN d.id", None, 10),
                    ("CREATE (:Decision {id: 'forbidden'})", None, 10),
                ],
            )

        assert resolved is False


class TestWriteClassificationFailsClosed:
    @pytest.mark.parametrize(
        "statement",
        [
            "CREATE (:Decision {id: 'x'})",
            "MATCH (d:Decision) DELETE d",
            "MATCH (d:Decision) SET d.title = 'x'",
            "/* comment */ CREATE (:Decision {id: 'y'})",
        ],
    )
    def test_a_mutation_is_classified_as_a_write(self, statement: str) -> None:
        assert statement_is_write(statement) is True

    def test_a_plain_read_is_not(self) -> None:
        assert statement_is_write("MATCH (d:Decision) RETURN d.id") is False

    def test_the_read_only_endpoint_refuses_a_write(self, executor) -> None:
        with pytest.raises(Exception) as caught:
            executor.execute_read_only(BOARD_ID, "CREATE (:Decision {id: 'x'})")
        assert "CREATE" in str(caught.value) or "read" in str(caught.value).lower()

    def test_statement_kind_never_echoes_the_query(self) -> None:
        assert statement_kind("MATCH (d) RETURN d") == "MATCH"
        assert statement_kind("  create (:X)") == "CREATE"


class TestTheScopeExecute:
    async def _scope(self, database: Any, fence: _RecordingFence) -> Any:
        from okto_pulse.community.adapters.grafx_graph_transaction import (
            CommunityGrafxGraphTransaction,
        )

        def resolve(board_id: str) -> Any:
            if board_id != BOARD_ID:
                raise KeyError(board_id)
            return database

        provider = CommunityGrafxGraphTransaction(
            database_resolver=resolve,
            revalidate_fence=fence,
            node_types=("Decision",),
            relationship_pairs=(("supersedes", "Decision", "Decision"),),
            relationship_table_resolver=lambda edge, source, target: (
                f"{edge}__{source}__{target}"
            ),
        )
        return await provider.begin(BOARD_ID)

    async def test_a_read_returns_a_graph_statement_result(
        self, grafx_database
    ) -> None:
        fence = _RecordingFence()
        scope = await self._scope(grafx_database, fence)
        try:
            result = scope.execute("MATCH (d:Decision) RETURN d.id")
        finally:
            await scope.rollback()

        assert isinstance(result, GraphStatementResult)
        assert result.columns == ("d.id",)
        assert {row[0] for row in result.rows} == {"d1", "d2"}
        assert all(isinstance(row, tuple) for row in result.rows)
        # A read is not a mutation, so it spends no fence beyond the one the
        # provider already took when the scope opened.
        assert fence.calls == [(BOARD_ID, "begin")]

    async def test_the_path_projection_is_converted_here_too(
        self, grafx_database
    ) -> None:
        fence = _RecordingFence()
        scope = await self._scope(grafx_database, fence)
        try:
            result = scope.execute(PATH_QUERY)
        finally:
            await scope.rollback()

        path = result.rows[0][0]
        assert isinstance(path["_NODES"], list)
        assert isinstance(path["_RELS"], list)

    async def test_a_write_revalidates_the_fence_before_running(
        self, grafx_database
    ) -> None:
        fence = _RecordingFence()
        scope = await self._scope(grafx_database, fence)
        try:
            scope.execute("CREATE (:Decision {id: 'd9', title: 'ninth'})")
            assert fence.calls == [
                (BOARD_ID, "begin"),
                (BOARD_ID, "graph_statement_precommit"),
            ]

            # Read-your-own-writes: the scope reads through its own
            # transaction, so its uncommitted row is visible to it.
            seen = scope.execute("MATCH (d:Decision {id: 'd9'}) RETURN d.id")
            assert [row[0] for row in seen.rows] == ["d9"]
        finally:
            await scope.rollback()

        # Rolled back, so nothing escaped the unit of work.
        with grafx_database.begin("read") as reader:
            after = reader.execute("MATCH (d:Decision {id: 'd9'}) RETURN d.id")
        assert list(after.rows) == []
