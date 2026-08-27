"""M-PULSE-3C contract for the current Pulse schema over Grafx."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import okto_grafx
import pytest
from okto_grafx import Timestamp
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
)
from okto_pulse.core.kg.schema_contract import (
    MULTI_REL_TYPES,
    NODE_TYPES,
    REL_TYPES,
    SCHEMA_VERSION,
    VECTOR_INDEX_TYPES,
    vector_index_name,
)

from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
)
from okto_pulse.community.adapters.grafx_schema_bootstrap import (
    ensure_current_grafx_board_schema,
    validate_current_grafx_schema,
)
from okto_pulse.community.adapters.grafx_schema_manifest import (
    PULSE_GRAFX_SCHEMA_MANIFEST,
)
from okto_pulse.community.adapters.graph_ddl import (
    COMMON_NODE_ATTRIBUTES,
    COMMON_NODE_COLUMNS,
    COMMON_REL_COLUMNS,
    build_multi_rel_ddl,
    build_node_ddl,
    build_rel_ddl,
)

_BOARD_ID = "board-schema-bootstrap"
_STAMP = Timestamp(micros=1_788_000_000_123_456)
_FINGERPRINT = "4a7b425bf4b8c4864be633c1a87f034e5f7f641019dc029015b7d3ca786deb81"
_KUZU_DDL_DIGEST = "18a8b1a1b9459d92d61670d734087a4212af29fa4039b0825d6e966ffa181e0e"


def _meta_row(database) -> tuple:
    result = database.execute(
        "MATCH (m:BoardMeta) "
        "RETURN m.board_id, m.schema_version, m.bootstrapped_at, "
        "m.embedding_model, m.embedding_dimension"
    )
    assert len(result.rows) == 1
    return result.rows[0]


def _wal_bytes(database_path: Path, wal) -> tuple[tuple[str, bytes], ...]:
    root = Path(wal.directory)
    if not root.is_absolute():
        root = database_path / root
    return tuple(
        (entry.relative_to(root).as_posix(), entry.read_bytes())
        for entry in sorted(root.rglob("*"))
        if entry.is_file()
    )


def _logical_column(
    name: str,
    pulse_type: str,
    *,
    nullable: bool,
    space: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "type": "VECTOR" if pulse_type == "DOUBLE[384]" else pulse_type,
        "nullable": nullable,
        "space": space,
    }


def _expected_logical_descriptor() -> dict[str, object]:
    relationship_columns = [
        _logical_column(name, pulse_type, nullable=True)
        for name, pulse_type in COMMON_REL_COLUMNS
    ]
    return {
        "contract": "okto-pulse-board-schema",
        "schema_version": SCHEMA_VERSION,
        "nodes": [
            {
                "name": node_type,
                "primary_key": "id",
                "columns": [
                    _logical_column(
                        name,
                        pulse_type,
                        nullable=name != "id",
                        space=vector_index_name(node_type)
                        if pulse_type == "DOUBLE[384]"
                        else None,
                    )
                    for name, pulse_type in COMMON_NODE_COLUMNS
                ],
            }
            for node_type in NODE_TYPES
        ],
        "board_meta": {
            "name": "BoardMeta",
            "primary_key": "board_id",
            "columns": [
                _logical_column("board_id", "STRING", nullable=False),
                _logical_column("schema_version", "STRING", nullable=True),
                _logical_column("bootstrapped_at", "TIMESTAMP", nullable=True),
                _logical_column("embedding_model", "STRING", nullable=True),
                _logical_column("embedding_dimension", "INT64", nullable=True),
            ],
        },
        "relationships": [
            {
                "name": definition.name,
                "endpoint_pairs": [list(pair) for pair in definition.endpoint_pairs],
                "columns": relationship_columns,
            }
            for definition in PULSE_RELATIONSHIP_LAYOUT.logical_definitions
        ],
        "spaces": [
            {
                "node_type": node_type,
                "name": vector_index_name(node_type),
                "dimension": 384,
                "metric": "cosine",
                "normalized": False,
                "storage_dtype": "float64",
                "searchable": node_type in VECTOR_INDEX_TYPES,
            }
            for node_type in NODE_TYPES
        ],
    }


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(
            *(_nested_keys(item) for item in value.values()),
        )
    if isinstance(value, list):
        return set().union(*(_nested_keys(item) for item in value))
    return set()


class _BeginCountingDatabase:
    def __init__(self, database) -> None:
        self._database = database
        self.begin_calls = 0

    def __getattr__(self, name: str):
        return getattr(self._database, name)

    def begin(self, mode: str):
        self.begin_calls += 1
        return self._database.begin(mode)


def test_manifest_is_the_closed_current_pulse_authority() -> None:
    manifest = PULSE_GRAFX_SCHEMA_MANIFEST

    assert manifest.schema_version == "0.5.0"
    assert tuple(table.name for table in manifest.nodes) == NODE_TYPES
    assert len(manifest.nodes) == 11
    assert len(manifest.board_meta.columns) == 5
    assert len(manifest.relationships) == 69
    assert len(manifest.tables) == 81
    assert len(manifest.spaces) == 11
    assert all(len(table.columns) == 44 for table in manifest.nodes)
    assert all(len(table.columns) == 9 for table in manifest.relationships)
    assert sum(space.node_type in VECTOR_INDEX_TYPES for space in manifest.spaces) == 9
    assert manifest.logical_fingerprint == _FINGERPRINT

    descriptor = manifest.logical_descriptor
    assert descriptor == _expected_logical_descriptor()
    assert manifest.logical_descriptor_json == json.dumps(
        descriptor,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert hashlib.sha256(
        manifest.logical_descriptor_json.encode("utf-8")
    ).hexdigest() == (_FINGERPRINT)
    assert _nested_keys(descriptor).isdisjoint(
        {
            "table_id",
            "space_id",
            "lsn",
            "csn",
            "path",
            "created_at_wall",
            "index_name",
        }
    )
    assert all(
        table.name not in manifest.logical_descriptor_json
        for table in manifest.relationships
    )
    assert len(descriptor["relationships"]) == 16
    assert "supersedes__Decision__Decision" not in repr(descriptor)
    descriptor["schema_version"] = "hostile"
    assert manifest.logical_descriptor["schema_version"] == "0.5.0"
    assert manifest.logical_fingerprint == _FINGERPRINT


def test_structured_ddl_authority_preserves_the_existing_kuzu_rendering() -> None:
    assert len(COMMON_NODE_COLUMNS) == 44
    assert tuple(name for name, _type in COMMON_REL_COLUMNS) == (
        "confidence",
        "created_by_session_id",
        "created_at",
        "layer",
        "rule_id",
        "created_by",
        "fallback_reason",
    )
    assert COMMON_NODE_ATTRIBUTES.startswith("id STRING PRIMARY KEY,\n    title STRING")
    assert COMMON_NODE_ATTRIBUTES.endswith(
        "selector_fingerprint STRING,\n    resolution_state STRING,\n    embedding DOUBLE[384]"
    )
    assert build_node_ddl("Decision") == (
        f"CREATE NODE TABLE IF NOT EXISTS Decision ({COMMON_NODE_ATTRIBUTES})"
    )
    assert build_rel_ddl("edge", "Decision", "Entity") == (
        "CREATE REL TABLE IF NOT EXISTS edge (FROM Decision TO Entity, "
        "confidence DOUBLE, created_by_session_id STRING, created_at TIMESTAMP, "
        "layer STRING, rule_id STRING, created_by STRING, fallback_reason STRING)"
    )
    assert build_multi_rel_ddl("edge", (("Decision", "Entity"),)) == (
        "CREATE REL TABLE IF NOT EXISTS edge (FROM Decision TO Entity, "
        "confidence DOUBLE, created_by_session_id STRING, created_at TIMESTAMP, "
        "layer STRING, rule_id STRING, created_by STRING, fallback_reason STRING)"
    )

    all_rendered_ddl = (
        *(build_node_ddl(node_type) for node_type in NODE_TYPES),
        *(build_rel_ddl(*definition) for definition in REL_TYPES),
        *(build_multi_rel_ddl(rel_name, pairs) for rel_name, pairs in MULTI_REL_TYPES),
    )
    encoded = json.dumps(
        all_rendered_ddl,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == _KUZU_DDL_DIGEST


def test_empty_bootstrap_is_exact_second_call_is_noop_and_reopen_is_stable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "current-schema"
    database = okto_grafx.connect(database_path)
    try:
        first = ensure_current_grafx_board_schema(
            database,
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
        )
        assert first.changed is True
        assert first.schema_version == "0.5.0"
        assert first.logical_fingerprint == _FINGERPRINT
        assert len(database.catalog.catalog.tables()) == 81
        assert len(database.catalog.catalog.spaces()) == 11
        assert _meta_row(database) == (_BOARD_ID, "0.5.0", _STAMP, None, None)
        assert database.verify("all").findings == ()

        catalog_before = database.catalog.catalog
        transactions_before = database.transactions
        wal_before = database.wal
        wal_bytes_before = _wal_bytes(database_path, wal_before)
        assert wal_bytes_before
        row_before = _meta_row(database)
        counted = _BeginCountingDatabase(database)
        second = ensure_current_grafx_board_schema(
            counted,  # type: ignore[arg-type]
            board_id=_BOARD_ID,
            bootstrapped_at=Timestamp(micros=_STAMP.micros + 999),
        )
        assert second.changed is False
        assert second.logical_fingerprint == first.logical_fingerprint
        assert counted.begin_calls == 0
        assert database.catalog.catalog == catalog_before
        assert database.transactions == transactions_before
        assert database.wal == wal_before
        assert _wal_bytes(database_path, wal_before) == wal_bytes_before
        assert _meta_row(database) == row_before
    finally:
        database.close()

    reopened = okto_grafx.connect(database_path)
    try:
        assert validate_current_grafx_schema(reopened) == _FINGERPRINT
        assert (
            ensure_current_grafx_board_schema(
                reopened,
                board_id=_BOARD_ID,
                bootstrapped_at=Timestamp(micros=1),
            ).changed
            is False
        )
        assert _meta_row(reopened) == (_BOARD_ID, "0.5.0", _STAMP, None, None)
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


def test_partial_exact_catalog_creates_only_missing_objects(tmp_path: Path) -> None:
    manifest = PULSE_GRAFX_SCHEMA_MANIFEST
    database = okto_grafx.connect(tmp_path / "partial")
    try:
        first_relationship = manifest.relationships[0]
        seeded_node_names = {
            first_relationship.from_table,
            first_relationship.to_table,
        }
        seeded_nodes = tuple(
            table for table in manifest.nodes if table.name in seeded_node_names
        )
        seeded_space_names = {
            column.vector_space
            for table in seeded_nodes
            for column in table.columns
            if column.vector_space is not None
        }
        with database.begin("write") as schema:
            for space in manifest.spaces:
                if space.name not in seeded_space_names:
                    continue
                schema.execute(space.ddl())
            for table in (manifest.board_meta, *seeded_nodes, first_relationship):
                schema.execute(table.ddl())

        expected_table_names = {table.name for table in manifest.tables}
        expected_space_names = {space.name for space in manifest.spaces}
        tables_before = {table.name for table in database.catalog.catalog.tables()}
        spaces_before = {space.name for space in database.catalog.catalog.spaces()}
        stable_ids = {
            table.name: table.table_id for table in database.catalog.catalog.tables()
        }
        result = ensure_current_grafx_board_schema(
            database,
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
            embedding_model="all-MiniLM-L6-v2",
            embedding_dimension=384,
        )

        assert result.changed is True
        assert result.logical_fingerprint == _FINGERPRINT
        assert len(database.catalog.catalog.tables()) == 81
        assert len(database.catalog.catalog.spaces()) == 11
        tables_after = {table.name for table in database.catalog.catalog.tables()}
        spaces_after = {space.name for space in database.catalog.catalog.spaces()}
        assert tables_after - tables_before == expected_table_names - tables_before
        assert spaces_after - spaces_before == expected_space_names - spaces_before
        assert {
            name: database.catalog.catalog.table(name).table_id for name in stable_ids
        } == stable_ids
        assert _meta_row(database) == (
            _BOARD_ID,
            "0.5.0",
            _STAMP,
            "all-MiniLM-L6-v2",
            384,
        )
    finally:
        database.close()


def test_missing_metadata_is_enriched_once_and_never_overwritten(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "metadata"
    database = okto_grafx.connect(database_path)
    try:
        ensure_current_grafx_board_schema(
            database,
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
        )
        enriched = ensure_current_grafx_board_schema(
            database,
            board_id=_BOARD_ID,
            bootstrapped_at=Timestamp(micros=2),
            embedding_model="all-MiniLM-L6-v2",
            embedding_dimension=384,
        )
        assert enriched.changed is True
        assert _meta_row(database) == (
            _BOARD_ID,
            "0.5.0",
            _STAMP,
            "all-MiniLM-L6-v2",
            384,
        )

        def assert_success_is_inert(
            *,
            embedding_model: str | None = None,
            embedding_dimension: int | None = None,
        ) -> None:
            catalog_before = database.catalog.catalog
            transactions_before = database.transactions
            wal_before = database.wal
            wal_bytes_before = _wal_bytes(database_path, wal_before)
            row_before = _meta_row(database)
            result = ensure_current_grafx_board_schema(
                database,
                board_id=_BOARD_ID,
                bootstrapped_at=Timestamp(micros=2),
                embedding_model=embedding_model,
                embedding_dimension=embedding_dimension,
            )
            assert result.changed is False
            assert database.catalog.catalog == catalog_before
            assert database.transactions == transactions_before
            assert database.wal == wal_before
            assert _wal_bytes(database_path, wal_before) == wal_bytes_before
            assert _meta_row(database) == row_before

        assert_success_is_inert(
            embedding_model="all-MiniLM-L6-v2",
            embedding_dimension=384,
        )
        assert_success_is_inert()

        def assert_refusal_is_inert(
            *,
            reason: str,
            embedding_model: str | None = None,
            embedding_dimension: int | None = None,
        ) -> None:
            catalog_before = database.catalog.catalog
            transactions_before = database.transactions
            wal_before = database.wal
            wal_bytes_before = _wal_bytes(database_path, wal_before)
            row_before = _meta_row(database)
            with pytest.raises(GraphCapabilityUnavailable) as mismatch:
                ensure_current_grafx_board_schema(
                    database,
                    board_id=_BOARD_ID,
                    bootstrapped_at=Timestamp(micros=3),
                    embedding_model=embedding_model,
                    embedding_dimension=embedding_dimension,
                )
            assert mismatch.value.details["reason"] == reason
            assert database.catalog.catalog == catalog_before
            assert database.transactions == transactions_before
            assert database.wal == wal_before
            assert _wal_bytes(database_path, wal_before) == wal_bytes_before
            assert _meta_row(database) == row_before

        assert_refusal_is_inert(
            reason="board_meta_embedding_mismatch",
            embedding_model="different-model",
            embedding_dimension=384,
        )

        with database.begin("write") as mutation:
            mutation.execute(
                "MATCH (m:BoardMeta {board_id: $board_id}) "
                "SET m.schema_version = $value RETURN m.board_id",
                {"board_id": _BOARD_ID, "value": "0.4.0"},
            )
        assert_refusal_is_inert(reason="board_meta_version_mismatch")

        with database.begin("write") as mutation:
            mutation.execute(
                "MATCH (m:BoardMeta {board_id: $board_id}) "
                "SET m.schema_version = $value, m.embedding_dimension = $dimension "
                "RETURN m.board_id",
                {"board_id": _BOARD_ID, "value": "0.5.0", "dimension": 383},
            )
        assert_refusal_is_inert(reason="board_meta_embedding_mismatch")
    finally:
        database.close()


class _NoWriteDatabase:
    def __init__(self, *, tables: tuple = (), spaces: tuple = ()) -> None:
        catalog = SimpleNamespace(tables=lambda: tables, spaces=lambda: spaces)
        self._catalog = SimpleNamespace(catalog=catalog)
        self.catalog_calls = 0
        self.begin_calls = 0

    @property
    def catalog(self):
        self.catalog_calls += 1
        return self._catalog

    def begin(self, _mode: str):
        self.begin_calls += 1
        raise AssertionError("preflight divergence must not open a write transaction")


def _observed_table(table) -> SimpleNamespace:
    return SimpleNamespace(
        name=table.name,
        kind=table.kind,
        primary_key=table.primary_key,
        from_table=table.from_table,
        to_table=table.to_table,
        columns=tuple(
            SimpleNamespace(
                name=column.name,
                type=SimpleNamespace(name=column.grafx_value_type),
                nullable=column.nullable,
                vector_space=column.vector_space,
            )
            for column in table.columns
        ),
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "kind",
        "primary_key",
        "column_removed",
        "column_extra",
        "column_order",
        "column_name",
        "column_type",
        "column_nullable",
        "column_space",
    ),
)
def test_every_table_shape_divergence_fails_before_write(mutation: str) -> None:
    expected = PULSE_GRAFX_SCHEMA_MANIFEST.nodes[0]
    observed = _observed_table(expected)
    columns = list(observed.columns)
    if mutation == "kind":
        observed.kind = "rel"
    elif mutation == "primary_key":
        observed.primary_key = None
    elif mutation == "column_removed":
        observed.columns = tuple(columns[:-1])
    elif mutation == "column_extra":
        observed.columns = (*columns, columns[-1])
    elif mutation == "column_order":
        columns[0], columns[1] = columns[1], columns[0]
        observed.columns = tuple(columns)
    elif mutation == "column_name":
        columns[1].name = "wrong"
    elif mutation == "column_type":
        columns[1].type = SimpleNamespace(name="INT64")
    elif mutation == "column_nullable":
        columns[1].nullable = False
    elif mutation == "column_space":
        columns[-1].vector_space = "wrong_space"

    database = _NoWriteDatabase(tables=(observed,))
    with pytest.raises(GraphCapabilityUnavailable) as captured:
        ensure_current_grafx_board_schema(
            database,  # type: ignore[arg-type]
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
        )
    assert captured.value.details["reason"] == "table_shape_mismatch"
    assert database.catalog_calls == 1
    assert database.begin_calls == 0


@pytest.mark.parametrize("endpoint", ("from_table", "to_table"))
def test_each_relationship_endpoint_divergence_fails_before_write(
    endpoint: str,
) -> None:
    expected = PULSE_GRAFX_SCHEMA_MANIFEST.relationships[0]
    observed = _observed_table(expected)
    setattr(observed, endpoint, "Entity")
    database = _NoWriteDatabase(tables=(observed,))

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        ensure_current_grafx_board_schema(
            database,  # type: ignore[arg-type]
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
        )

    assert captured.value.details["reason"] == "table_shape_mismatch"
    assert database.catalog_calls == 1
    assert database.begin_calls == 0


@pytest.mark.parametrize("table_kind", ("board_meta", "relationship"))
def test_each_non_node_table_shape_is_validated_before_write(table_kind: str) -> None:
    manifest = PULSE_GRAFX_SCHEMA_MANIFEST
    expected = (
        manifest.board_meta if table_kind == "board_meta" else manifest.relationships[0]
    )
    observed = _observed_table(expected)
    columns = list(observed.columns)
    columns[-1].type = SimpleNamespace(name="BOOL")
    database = _NoWriteDatabase(tables=(observed,))

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        ensure_current_grafx_board_schema(
            database,  # type: ignore[arg-type]
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
        )

    assert captured.value.details["reason"] == "table_shape_mismatch"
    assert database.catalog_calls == 1
    assert database.begin_calls == 0


@pytest.mark.parametrize(
    "mutation",
    ("dimension", "metric", "normalized", "storage_dtype", "state"),
)
def test_every_space_shape_divergence_fails_before_write(mutation: str) -> None:
    expected = PULSE_GRAFX_SCHEMA_MANIFEST.spaces[0]
    observed = SimpleNamespace(
        name=expected.name,
        dimension=expected.dimension,
        metric=SimpleNamespace(value=expected.metric),
        normalized=expected.normalized,
        storage_dtype=expected.storage_dtype,
        state="active",
    )
    if mutation == "metric":
        observed.metric = SimpleNamespace(value="euclidean")
    elif mutation == "normalized":
        observed.normalized = True
    elif mutation == "dimension":
        observed.dimension = 383
    elif mutation == "storage_dtype":
        observed.storage_dtype = "float32"
    elif mutation == "state":
        observed.state = "retired"

    database = _NoWriteDatabase(spaces=(observed,))
    with pytest.raises(GraphCapabilityUnavailable) as captured:
        ensure_current_grafx_board_schema(
            database,  # type: ignore[arg-type]
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
        )
    assert captured.value.details["reason"] == "space_shape_mismatch"
    assert database.catalog_calls == 1
    assert database.begin_calls == 0


@pytest.mark.parametrize("kind", ("table", "space"))
def test_an_unexpected_schema_object_fails_before_write(kind: str) -> None:
    unexpected = SimpleNamespace(name="CustomObject")
    database = _NoWriteDatabase(
        tables=(unexpected,) if kind == "table" else (),
        spaces=(unexpected,) if kind == "space" else (),
    )
    with pytest.raises(GraphCapabilityUnavailable) as captured:
        ensure_current_grafx_board_schema(
            database,  # type: ignore[arg-type]
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
        )
    assert captured.value.details["reason"] == "unexpected_schema_object"
    assert database.catalog_calls == 1
    assert database.begin_calls == 0


def test_real_preflight_refusal_is_catalog_and_wal_byte_inert(tmp_path: Path) -> None:
    database_path = tmp_path / "unexpected"
    database = okto_grafx.connect(database_path)
    try:
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE CustomObject(id STRING, PRIMARY KEY(id))"
            )

        catalog_before = database.catalog.catalog
        transactions_before = database.transactions
        wal_before = database.wal
        wal_bytes_before = _wal_bytes(database_path, wal_before)
        assert wal_bytes_before

        with pytest.raises(GraphCapabilityUnavailable) as captured:
            ensure_current_grafx_board_schema(
                database,
                board_id=_BOARD_ID,
                bootstrapped_at=_STAMP,
            )

        assert captured.value.details["reason"] == "unexpected_schema_object"
        assert not database.catalog.catalog.has_table("BoardMeta")
        assert database.catalog.catalog == catalog_before
        assert database.transactions == transactions_before
        assert database.wal == wal_before
        assert _wal_bytes(database_path, wal_before) == wal_bytes_before
    finally:
        database.close()


def test_direct_schema_validation_reports_its_own_operation() -> None:
    database = _NoWriteDatabase()

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        validate_current_grafx_schema(database)  # type: ignore[arg-type]

    assert captured.value.details["reason"] == "schema_incomplete"
    assert captured.value.details["operation"] == "validate_current_grafx_schema"
    assert database.catalog_calls == 1
    assert database.begin_calls == 0


class _FailingTransaction:
    def __init__(self, transaction, *, fail_at: int) -> None:
        self._transaction = transaction
        self._fail_at = fail_at
        self._calls = 0

    @property
    def active(self) -> bool:
        return self._transaction.active

    def execute(self, text: str, parameters: dict):
        self._calls += 1
        if self._calls == self._fail_at:
            raise RuntimeError("injected schema bootstrap failure")
        return self._transaction.execute(text, parameters)

    def commit(self):
        return self._transaction.commit()

    def rollback(self) -> None:
        self._transaction.rollback()


class _FailingDatabase:
    def __init__(self, database, *, fail_at: int) -> None:
        self._database = database
        self._fail_at = fail_at

    @property
    def catalog(self):
        return self._database.catalog

    def execute(self, text: str, parameters=None):
        return self._database.execute(text, parameters)

    def begin(self, mode: str):
        return _FailingTransaction(self._database.begin(mode), fail_at=self._fail_at)


class _FailingPostValidationDatabase:
    def __init__(self, database) -> None:
        self._database = database
        self.catalog_calls = 0

    @property
    def catalog(self):
        self.catalog_calls += 1
        if self.catalog_calls == 2:
            raise RuntimeError("injected post-commit schema validation failure")
        return self._database.catalog

    def execute(self, text: str, parameters=None):
        return self._database.execute(text, parameters)

    def begin(self, mode: str):
        return self._database.begin(mode)


def test_a_schema_failure_rolls_back_every_prefix_and_never_stamps_version(
    tmp_path: Path,
) -> None:
    database = okto_grafx.connect(tmp_path / "failure")
    try:
        # Fail after every space, BoardMeta and all vector-bearing nodes have been staged,
        # just as the first relationship would be created.
        failing = _FailingDatabase(database, fail_at=24)
        with pytest.raises(GraphError):
            ensure_current_grafx_board_schema(
                failing,  # type: ignore[arg-type]
                board_id=_BOARD_ID,
                bootstrapped_at=_STAMP,
            )
        assert database.catalog.catalog.is_empty()
        assert database.vectors.indexes() == ()
        assert database.verify("all").findings == ()

        retry = ensure_current_grafx_board_schema(
            database,
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
        )
        assert retry.changed is True
        assert retry.logical_fingerprint == _FINGERPRINT
        assert _meta_row(database) == (_BOARD_ID, "0.5.0", _STAMP, None, None)
    finally:
        database.close()


def test_postvalidation_and_metadata_failures_leave_version_absent_and_retry(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "postvalidation-failure"
    database = okto_grafx.connect(database_path)
    try:
        postvalidation_failure = _FailingPostValidationDatabase(database)
        with pytest.raises(GraphError):
            ensure_current_grafx_board_schema(
                postvalidation_failure,  # type: ignore[arg-type]
                board_id=_BOARD_ID,
                bootstrapped_at=_STAMP,
            )

        assert postvalidation_failure.catalog_calls == 2
        assert len(database.catalog.catalog.tables()) == 81
        assert len(database.catalog.catalog.spaces()) == 11
        assert database.catalog.catalog.has_table("BoardMeta")
        assert database.execute("MATCH (m:BoardMeta) RETURN m.board_id").rows == ()
        assert database.verify("all").findings == ()

        catalog_before = database.catalog.catalog
        transactions_before = database.transactions
        wal_before = database.wal
        wal_bytes_before = _wal_bytes(database_path, wal_before)
        metadata_failure = _FailingDatabase(database, fail_at=1)
        with pytest.raises(GraphError):
            ensure_current_grafx_board_schema(
                metadata_failure,  # type: ignore[arg-type]
                board_id=_BOARD_ID,
                bootstrapped_at=_STAMP,
            )

        assert database.execute("MATCH (m:BoardMeta) RETURN m.board_id").rows == ()
        assert database.catalog.catalog == catalog_before
        assert database.transactions == transactions_before
        assert database.wal == wal_before
        assert _wal_bytes(database_path, wal_before) == wal_bytes_before

        retry = ensure_current_grafx_board_schema(
            database,
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
        )
        assert retry.changed is True
        assert retry.logical_fingerprint == _FINGERPRINT
        assert _meta_row(database) == (_BOARD_ID, "0.5.0", _STAMP, None, None)
    finally:
        database.close()


@pytest.mark.parametrize(
    ("model", "dimension"),
    ((None, 384), ("model", None), ("model", 383), ("", 384)),
)
def test_invalid_embedding_metadata_is_refused_without_backend_access(
    model: str | None,
    dimension: int | None,
) -> None:
    class BackendMustNotBeRead:
        @property
        def catalog(self):
            raise AssertionError("invalid arguments must fail before backend access")

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        ensure_current_grafx_board_schema(
            BackendMustNotBeRead(),  # type: ignore[arg-type]
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
            embedding_model=model,
            embedding_dimension=dimension,
        )
    assert captured.value.details["reason"] == "invalid_bootstrap_argument"
