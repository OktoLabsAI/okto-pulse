"""Digest source probes remain exact without a full table scan per upsert."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from okto_grafx import connect
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphIndexUnavailable,
)

from okto_pulse.community.adapters.grafx_global_discovery import (
    PULSE_GRAFX_GLOBAL_SCHEMA,
    certify_grafx_global_vector_indexes,
    ensure_current_grafx_global_schema,
    upsert_grafx_decision_digest_vector,
)
from okto_pulse.community.adapters.grafx_global_indexes import (
    DIGEST_SOURCE_INDEX,
    ensure_grafx_global_digest_source_index,
    validate_grafx_global_digest_source_index,
)

_LOOKUP = (
    "MATCH (d:DecisionDigest) WHERE d.board_id = $board_id "
    "AND d.original_node_id = $original_node_id RETURN d.id"
)


@pytest.mark.parametrize('fault', [None, 'absent', 'ahead', 'stale', 'identity'])
def test_cold_vector_certification_reads_durable_header(tmp_path, fault):
    with connect(tmp_path / 'global') as database:
        ensure_current_grafx_global_schema(database)
        before = database.transactions.published_lsn()

        class ColdProbe:
            def __getattr__(self, name):
                return getattr(database, name)

            @property
            def vectors(self):
                view = database.vectors
                return replace(view, registered_indexes=tuple(
                    replace(index, built_through_lsn=None) for index in view.registered_indexes
                ))

            @property
            def indexes(self):
                view = database.indexes
                return replace(view, registered=tuple(
                    replace(index, built_through_lsn=None) for index in view.registered
                ))

            def read_index_status(self, name):
                index = database.read_index_status(name)
                if fault == 'absent':
                    return replace(index, built_through_lsn=None)
                if fault == 'ahead':
                    return replace(index, built_through_lsn=before + 1)
                if fault == 'stale':
                    return replace(index, stale=True)
                if fault == 'identity':
                    return replace(index, file='index/wrong.idx')
                return index

        if fault:
            with pytest.raises(GraphIndexUnavailable):
                certify_grafx_global_vector_indexes(ColdProbe())
        else:
            result = certify_grafx_global_vector_indexes(ColdProbe())
            assert len(result) == 4
            assert all(type(index.built_through_lsn) is int for index in result)
        assert database.transactions.published_lsn() == before


def _legacy_global_schema(database):
    with database.begin("write") as transaction:
        for space in PULSE_GRAFX_GLOBAL_SCHEMA.spaces:
            transaction.execute(space.ddl())
        for table in PULSE_GRAFX_GLOBAL_SCHEMA.tables:
            transaction.execute(table.ddl())


def _add_digest(database, digest_id, board="b1", source="n1"):
    with database.begin("write") as transaction:
        transaction.execute(
            "CREATE (:DecisionDigest {id:$id, board_id:$board, original_node_id:$source})",
            {"id": digest_id, "board": board, "source": source},
        )


def _assert_seek(database):
    plan = repr(database.explain(_LOOKUP))
    assert "IndexSeek(" in plan
    assert DIGEST_SOURCE_INDEX in plan
    assert "NodeScan(" not in plan


def test_fresh_global_schema_installs_index_once_and_uses_exact_seek(tmp_path: Path):
    with connect(tmp_path / "global") as database:
        phases = []
        first = ensure_current_grafx_global_schema(
            database, revalidate_fence=phases.append
        )
        assert first.changed
        assert "global_digest_source_index" in phases
        assert phases[-1] == "commit"
        assert validate_grafx_global_digest_source_index(database)
        _assert_seek(database)
        before = database.transactions.published_lsn()
        assert not ensure_current_grafx_global_schema(database).changed
        assert database.transactions.published_lsn() == before


@pytest.mark.parametrize("fault", ["ahead", "mismatch", "stale", "findings", "advance"])
def test_vector_certification_retains_coverage_and_publication_guards(
    tmp_path: Path, fault
):
    with connect(tmp_path / "global") as database:
        ensure_current_grafx_global_schema(database)
        publication = database.transactions.published_lsn()

        class Probe:
            def __getattr__(self, name):
                return getattr(database, name)

            def verify(self, scope):
                report = database.verify(scope)
                if fault == "findings":
                    return SimpleNamespace(findings=("injected missing vector entry",))
                if fault == "advance":
                    database.create_index("concurrent_unrelated", "Board", ("name",))
                return report

            @property
            def vectors(self):
                view = database.vectors
                first, *rest = view.registered_indexes
                if fault == "ahead":
                    first = replace(first, built_through_lsn=publication + 1)
                elif fault == "mismatch":
                    first = replace(
                        first, built_through_lsn=first.built_through_lsn + 1
                    )
                elif fault == "stale":
                    first = replace(first, stale=True)
                return replace(view, registered_indexes=(first, *rest))

            @property
            def indexes(self):
                view = database.indexes
                if fault != "ahead":
                    return view
                name = database.vectors.registered_indexes[0].name
                return replace(
                    view,
                    registered=tuple(
                        replace(index, built_through_lsn=publication + 1)
                        if index.name == name
                        else index
                        for index in view.registered
                    ),
                )

        expected = {
            "findings": "verification_findings",
            "advance": "verification_snapshot_changed",
        }.get(fault, "index_status_mismatch")
        with pytest.raises(GraphIndexUnavailable) as captured:
            certify_grafx_global_vector_indexes(Probe())
        assert captured.value.details["reason"] == expected


def test_cold_existing_schema_and_rows_are_backfilled_without_losing_duplicate_hits(
    tmp_path: Path,
):
    root = tmp_path / "global"
    with connect(root) as database:
        _legacy_global_schema(database)
        _add_digest(database, "d1")
        _add_digest(database, "d2")
        _add_digest(database, "foreign", board="b2")
        assert not validate_grafx_global_digest_source_index(database)
    with connect(root) as database:
        assert ensure_current_grafx_global_schema(database).changed
        _assert_seek(database)
        assert set(
            database.execute(_LOOKUP, {"board_id": "b1", "original_node_id": "n1"}).rows
        ) == {("d1",), ("d2",)}
    with connect(root) as database:
        assert not ensure_current_grafx_global_schema(database).changed
        _assert_seek(database)
        assert set(
            database.execute(_LOOKUP, {"board_id": "b1", "original_node_id": "n1"}).rows
        ) == {("d1",), ("d2",)}
        assert database.verify("all").findings == ()


@pytest.mark.parametrize("columns", [("id",), ("original_node_id", "board_id")])
def test_incompatible_existing_index_is_refused_before_any_write(
    tmp_path: Path, columns
):
    with connect(tmp_path / "global") as database:
        _legacy_global_schema(database)
        incumbent = database.create_index(
            DIGEST_SOURCE_INDEX, "DecisionDigest", columns
        )
        before = database.transactions.published_lsn()
        with pytest.raises(GraphCapabilityUnavailable) as captured:
            ensure_current_grafx_global_schema(database)
        assert captured.value.details["reason"] == "digest_source_index_mismatch"
        assert database.indexes.index(DIGEST_SOURCE_INDEX) == incumbent
        assert database.transactions.published_lsn() == before


@pytest.mark.parametrize("denied_phase", ["global_digest_source_index", "commit"])
def test_index_creation_respects_fence_and_rolls_back(tmp_path: Path, denied_phase):
    with connect(tmp_path / "global") as database:
        _legacy_global_schema(database)
        _add_digest(database, "d1")
        before = database.transactions.published_lsn()

        def fence(phase):
            if phase == denied_phase:
                raise RuntimeError("fence lost")

        with pytest.raises(RuntimeError, match="fence lost"):
            ensure_grafx_global_digest_source_index(database, revalidate_fence=fence)
        assert not validate_grafx_global_digest_source_index(database)
        assert database.transactions.published_lsn() == before
        assert database.execute("MATCH (d:DecisionDigest) RETURN d.id").rows == (
            ("d1",),
        )


@pytest.mark.parametrize("collision", ["source", "primary_key"])
def test_indexed_upsert_still_refuses_identity_collisions_without_writing(
    tmp_path: Path, collision
):
    with connect(tmp_path / "global") as database:
        ensure_current_grafx_global_schema(database)
        if collision == "source":
            _add_digest(database, "wanted")
            _add_digest(database, "duplicate")
            reason = "digest_identity_collision"
        else:
            _add_digest(database, "wanted", board="other")
            reason = "digest_primary_key_collision"
        before = database.transactions.published_lsn()
        with pytest.raises(GraphCapabilityUnavailable) as captured:
            upsert_grafx_decision_digest_vector(
                database,
                digest_id="wanted",
                board_id="b1",
                original_node_id="n1",
                title="title",
                summary="summary",
                node_type="Decision",
                graph_layer="canonical",
                embedding=[1.0, *([0.0] * 383)],
                created_at="2026-09-07T12:00:00Z",
            )
        assert captured.value.details["reason"] == reason
        assert database.transactions.published_lsn() == before
