"""Final frozen M-PULSE-5 matrix: eight functions and thirty-two cases."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from okto_grafx import Transaction
from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    ArtifactIntegrityError,
    LogicalNode,
    LogicalSchemaError,
    LogicalTimestamp,
    LogicalVector,
    PhasedTransferError,
    fingerprint_graph,
    transfer_logical_graph,
)

from logical_transfer_matrix_support import (
    BACKENDS,
    Corpus,
    MaterializedSource,
    ObservedSink,
    ObservedSnapshot,
    ObservedSource,
    canonical_corpus,
    close_database,
    complete_node,
    corrupt_artifact_payload,
    dense_global_corpus,
    endpoint_database,
    endpoint_schema,
    expected_ladybug_filename,
    export_database,
    export_generation,
    failing_file_nodes,
    insert_omitted_and_null_rows,
    make_physical_sink,
    make_physical_source,
    native_insert_node,
    one_node_corpus,
    open_generation_database,
    portable_file_corpus,
    schema_for,
    seed_generation,
    sink_retains_logical_records,
    tree_digest,
)
from okto_pulse.community.adapters import logical_graph_file
from okto_pulse.community.adapters.logical_graph_transfer import (
    restore_logical_graph_file,
)
from okto_pulse.community.adapters.logical_transfer_grafx import (
    CommunityGrafxLogicalSnapshotSource,
    _EndpointMap,
)
from okto_pulse.community.adapters.logical_transfer_schema import (
    searchable_indexes,
)


@pytest.mark.parametrize("backend", BACKENDS)
def test_physical_null_and_unrepresentable_absent(
    tmp_path: Path,
    backend: str,
) -> None:
    """A1[2]: physical NULL projection, absent import refusal and cleanup."""

    scope = "global_discovery"
    schema = schema_for(scope)
    temporary_parent = tmp_path / "endpoint-maps"
    temporary_parent.mkdir()

    origin = tmp_path / "null-origin"
    seed_generation(
        backend,
        origin,
        Corpus(schema, (), ()),
        temporary_parent=temporary_parent,
    )
    database = open_generation_database(backend, origin, scope, read_only=False)
    try:
        type_name, _key_name, nullable_name = insert_omitted_and_null_rows(
            backend, database, schema
        )
        exported = export_database(
            backend,
            database,
            scope=scope,
            batch_size=1,
            temporary_parent=temporary_parent,
        )
    finally:
        close_database(backend, database)

    rows = {node.key: node for node in exported.nodes if node.type_name == type_name}
    declared_names = schema.node_type(type_name).property_names()
    assert rows["omitted"].properties[nullable_name] is LOGICAL_NULL
    assert rows["explicit"].properties[nullable_name] is LOGICAL_NULL
    assert set(rows["omitted"].properties) == declared_names
    assert set(rows["explicit"].properties) == declared_names

    previous = tmp_path / "previous"
    seed_generation(
        backend,
        previous,
        one_node_corpus(scope),
        temporary_parent=temporary_parent,
    )
    previous_digest = tree_digest(previous)
    node_type = schema.node_type("Topic")
    complete = complete_node(schema, node_type.name, "incomplete", 71)
    missing_name = next(
        prop.name
        for prop in node_type.properties
        if prop.nullable and prop.name != node_type.key
    )
    incomplete_properties = dict(complete.properties)
    del incomplete_properties[missing_name]
    incomplete = LogicalNode(
        complete.type_name,
        complete.key,
        incomplete_properties,
    )
    source = MaterializedSource(Corpus(schema, (incomplete,), ()))
    candidate = tmp_path / "absent-candidate"
    sink = ObservedSink(
        make_physical_sink(
            backend,
            candidate,
            scope=scope,
            max_batch_size=1,
            temporary_parent=temporary_parent,
        )
    )

    with pytest.raises(PhasedTransferError) as caught:
        transfer_logical_graph(source, sink, batch_size=1)

    assert caught.value.phase == "import"
    assert "absent" in str(caught.value.__cause__).lower()
    assert source.snapshot.close_calls == 1
    assert sink.abort_calls == 1
    assert sink.finalize_calls == 0
    assert not candidate.exists()
    assert tree_digest(previous) == previous_digest
    assert list(temporary_parent.iterdir()) == []


@pytest.mark.parametrize("case", ["success", "scan_failure", "dangling_endpoint"])
def test_grafx_endpoint_map_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """A2[3]: accepted disk-backed Grafx endpoint-map contract."""

    temporary_parent = tmp_path / "endpoint-maps"
    temporary_parent.mkdir()
    database = endpoint_database(tmp_path / "graph")
    source = CommunityGrafxLogicalSnapshotSource(
        database,
        schema=endpoint_schema(),
        relationship_tables={("links", "A", "B"): "A_links_B"},
        scan_batch_size=1,
        temporary_parent=temporary_parent,
    )
    inserted_batch_sizes: list[int] = []
    lookup_keys: list[tuple[str, int]] = []
    original_add = _EndpointMap.add_batch
    original_resolve = _EndpointMap.resolve

    def observed_add(endpoint_map, entries):
        inserted_batch_sizes.append(len(entries))
        return original_add(endpoint_map, entries)

    def observed_resolve(endpoint_map, node_table, record_id):
        lookup_keys.append((node_table, record_id))
        return original_resolve(endpoint_map, node_table, record_id)

    monkeypatch.setattr(_EndpointMap, "add_batch", observed_add)
    monkeypatch.setattr(_EndpointMap, "resolve", observed_resolve)

    if case == "scan_failure":
        original_scan = Transaction.scan_rows_v1

        def failing_scan(transaction, table, *, limit, cursor=None):
            if table == "A_links_B":
                raise OSError("injected scan failure")
            return original_scan(transaction, table, limit=limit, cursor=cursor)

        monkeypatch.setattr(Transaction, "scan_rows_v1", failing_scan)
    elif case == "dangling_endpoint":
        with database.begin("write") as writer:
            writer.execute("MATCH (a:A {id: 'a1'}) DELETE a")

    if case == "success":
        snapshot = source.open_snapshot()
        assert snapshot._endpoints is not None
        endpoint_path = snapshot._endpoints.path
        nodes = tuple(
            node for batch in snapshot.iter_nodes(batch_size=1) for node in batch
        )
        relations = tuple(
            relation
            for batch in snapshot.iter_relations(batch_size=1)
            for relation in batch
        )
        assert tuple((node.type_name, node.key) for node in nodes) == (
            ("A", "a1"),
            ("B", "b1"),
        )
        assert nodes[0].properties["note"] is LOGICAL_NULL
        assert len(relations) == 1
        assert relations[0].source_key == "a1"
        assert relations[0].target_key == "b1"
        assert relations[0].properties["note"] == ""
        assert snapshot.counts().as_mapping() == {
            "nodes": 2,
            "relations": 1,
            "properties": 4,
            "vectors": 0,
        }
        assert lookup_keys == [("A", 1), ("B", 1), ("A", 1), ("B", 1)]
        assert endpoint_path.exists()

        rollback_attempts = 0
        original_rollback = Transaction.rollback

        def transient_rollback(transaction):
            nonlocal rollback_attempts
            rollback_attempts += 1
            if rollback_attempts == 1:
                raise OSError("injected transient rollback failure")
            return original_rollback(transaction)

        monkeypatch.setattr(Transaction, "rollback", transient_rollback)
        with pytest.raises(OSError, match="transient rollback failure"):
            snapshot.close()
        assert snapshot._transaction.active
        with pytest.raises(LogicalSchemaError, match="snapshot is closed"):
            snapshot.schema()
        snapshot.close()
        snapshot.close()
        assert rollback_attempts == 2
        assert not snapshot._transaction.active
        assert not endpoint_path.exists()
    elif case == "scan_failure":
        with pytest.raises(OSError, match="injected scan failure"):
            source.open_snapshot()
    else:
        with pytest.raises(LogicalSchemaError, match="missing physical endpoint"):
            source.open_snapshot()

    assert inserted_batch_sizes
    assert max(inserted_batch_sizes) <= 1
    assert list(temporary_parent.iterdir()) == []
    database.close()


@pytest.mark.parametrize(
    ("scope", "origin_backend", "destination_backend"),
    [
        ("board", "ladybug", "grafx"),
        ("board", "grafx", "ladybug"),
        ("global_discovery", "ladybug", "grafx"),
        ("global_discovery", "grafx", "ladybug"),
    ],
    ids=("board-l2g", "board-g2l", "global-l2g", "global-g2l"),
)
def test_canonical_roundtrip(
    tmp_path: Path,
    scope: str,
    origin_backend: str,
    destination_backend: str,
) -> None:
    """B1[4]: the complete canonical corpus survives both physical directions."""

    corpus = canonical_corpus(scope)
    temporary_parent = tmp_path / "endpoint-maps"
    temporary_parent.mkdir()
    origin = tmp_path / "origin"
    origin_sink = seed_generation(
        origin_backend,
        origin,
        corpus,
        batch_size=5,
        temporary_parent=temporary_parent,
    )
    if origin_backend == "ladybug":
        assert origin_sink.candidate_path == origin
        assert origin_sink.database_path == origin / expected_ladybug_filename(scope)

    database = open_generation_database(origin_backend, origin, scope, read_only=True)
    candidate = tmp_path / "candidate"
    destination = ObservedSink(
        make_physical_sink(
            destination_backend,
            candidate,
            scope=scope,
            max_batch_size=5,
            temporary_parent=temporary_parent,
        )
    )
    try:
        source = make_physical_source(
            origin_backend,
            database,
            scope=scope,
            scan_batch_size=5,
            temporary_parent=temporary_parent,
        )
        report = transfer_logical_graph(source, destination, batch_size=5)
    finally:
        close_database(origin_backend, database)

    assert report.counts == corpus.counts
    assert report.fingerprint == corpus.fingerprint
    assert destination.abort_calls == 0
    assert destination.finalize_calls == 1
    if destination_backend == "ladybug":
        assert destination.candidate_path == candidate
        assert destination.database_path == (
            candidate / expected_ladybug_filename(scope)
        )

    restored = export_generation(
        destination_backend,
        candidate,
        scope=scope,
        batch_size=5,
        temporary_parent=temporary_parent,
    )
    assert restored.schema == corpus.schema
    assert restored.counts == corpus.counts
    assert restored.fingerprint == corpus.fingerprint
    assert {relation.layout_identity for relation in restored.relations} == {
        layout.identity for layout in corpus.schema.relation_layouts
    }
    parallel = corpus.relations[-1]
    assert sum(relation == parallel for relation in restored.relations) == 2
    assert any(
        relation.source_key == relation.target_key for relation in restored.relations
    )
    values = [
        value
        for record in (*restored.nodes, *restored.relations)
        for value in record.properties.values()
    ]
    assert "" in values
    assert any(value is LOGICAL_NULL for value in values)
    assert any(isinstance(value, LogicalTimestamp) for value in values)
    assert any(isinstance(value, LogicalVector) for value in values)
    assert len(searchable_indexes(corpus.schema)) == (9 if scope == "board" else 4)
    assert list(temporary_parent.iterdir()) == []


@pytest.mark.parametrize(
    ("backend", "scope"),
    [
        ("ladybug", "board"),
        ("ladybug", "global_discovery"),
        ("grafx", "board"),
        ("grafx", "global_discovery"),
    ],
)
def test_source_snapshot_is_stable_during_concurrent_write(
    tmp_path: Path,
    backend: str,
    scope: str,
) -> None:
    """C1[4]: one opened snapshot does not drift under a concurrent writer."""

    temporary_parent = tmp_path / "endpoint-maps"
    temporary_parent.mkdir()
    baseline = one_node_corpus(scope)
    origin = tmp_path / "origin"
    seed_generation(
        backend,
        origin,
        baseline,
        batch_size=1,
        temporary_parent=temporary_parent,
    )
    database = open_generation_database(backend, origin, scope, read_only=False)
    source = make_physical_source(
        backend,
        database,
        scope=scope,
        scan_batch_size=1,
        temporary_parent=temporary_parent,
    )
    snapshot = ObservedSnapshot(source.open_snapshot())
    writer_failures: list[BaseException] = []
    type_name = "Decision" if scope == "board" else "Topic"
    intruder = complete_node(snapshot.schema(), type_name, "intruder", 9_001)
    iterator = snapshot.iter_nodes(batch_size=1)
    first = tuple(next(iterator))

    def write_concurrently() -> None:
        try:
            native_insert_node(backend, database, snapshot.schema(), intruder)
        except BaseException as failure:  # noqa: BLE001 - returned to test thread
            writer_failures.append(failure)

    writer = threading.Thread(target=write_concurrently, daemon=True)
    writer.start()
    writer.join(timeout=30)
    assert not writer.is_alive(), "concurrent physical writer did not finish"
    assert writer_failures == []
    try:
        during_counts = snapshot.counts()
        remaining = tuple(node for batch in iterator for node in batch)
        relations = tuple(
            relation
            for batch in snapshot.iter_relations(batch_size=1)
            for relation in batch
        )
    finally:
        snapshot.close()

    observed_nodes = first + remaining
    assert snapshot.close_calls == 1
    assert during_counts == baseline.counts
    assert fingerprint_graph(snapshot.delegate._schema, observed_nodes, relations) == (
        baseline.fingerprint
    )
    assert all(node.key != "intruder" for node in observed_nodes)

    fresh = export_database(
        backend,
        database,
        scope=scope,
        batch_size=1,
        temporary_parent=temporary_parent,
    )
    assert fresh.counts.nodes == baseline.counts.nodes + 1
    assert any(node.key == "intruder" for node in fresh.nodes)
    close_database(backend, database)
    assert list(temporary_parent.iterdir()) == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_community_batches_are_bounded_and_cleanup_is_total(
    tmp_path: Path,
    backend: str,
) -> None:
    """C2[2]: source/sink bounds hold on success and partial-import cleanup."""

    limit = 4
    corpus = dense_global_corpus(25)
    temporary_parent = tmp_path / "endpoint-maps"
    temporary_parent.mkdir()
    origin = tmp_path / "origin"
    seed_generation(
        backend,
        origin,
        corpus,
        batch_size=limit,
        temporary_parent=temporary_parent,
    )
    database = open_generation_database(
        backend, origin, "global_discovery", read_only=True
    )
    origin_digest = tree_digest(origin)
    try:
        source = ObservedSource(
            make_physical_source(
                backend,
                database,
                scope="global_discovery",
                scan_batch_size=limit,
                temporary_parent=temporary_parent,
            )
        )
        success = ObservedSink(
            make_physical_sink(
                backend,
                tmp_path / "success",
                scope="global_discovery",
                max_batch_size=limit,
                temporary_parent=temporary_parent,
            )
        )
        report = transfer_logical_graph(source, success, batch_size=limit)
        assert report.counts == corpus.counts
        assert source.snapshot is not None
        assert source.snapshot.close_calls == 1
        assert max(source.snapshot.node_batch_sizes) <= limit
        assert max(source.snapshot.relation_batch_sizes) <= limit
        assert len(source.snapshot.node_batch_sizes) > 1
        assert len(source.snapshot.relation_batch_sizes) > 1
        assert max(success.node_batch_sizes) <= limit
        assert max(success.relation_batch_sizes) <= limit
        assert not sink_retains_logical_records(success.delegate)

        failed_source = ObservedSource(
            make_physical_source(
                backend,
                database,
                scope="global_discovery",
                scan_batch_size=limit,
                temporary_parent=temporary_parent,
            )
        )
        failed_candidate = tmp_path / "failed"
        failed = ObservedSink(
            make_physical_sink(
                backend,
                failed_candidate,
                scope="global_discovery",
                max_batch_size=limit,
                temporary_parent=temporary_parent,
            ),
            fail_on="import",
        )
        with pytest.raises(PhasedTransferError) as caught:
            transfer_logical_graph(failed_source, failed, batch_size=limit)
        assert caught.value.phase == "import"
        assert failed.abort_calls == 1
        assert failed.finalize_calls == 0
        assert not failed_candidate.exists()
        assert not sink_retains_logical_records(failed.delegate)
    finally:
        close_database(backend, database)

    assert tree_digest(origin) == origin_digest
    assert list(temporary_parent.iterdir()) == []


@pytest.mark.parametrize(
    ("backend", "fault"),
    [
        ("ladybug", "write"),
        ("grafx", "write"),
        ("ladybug", "import"),
        ("ladybug", "checkpoint"),
        ("ladybug", "reopen"),
        ("grafx", "import"),
        ("grafx", "checkpoint"),
        ("grafx", "reopen"),
    ],
)
def test_transfer_failure_matrix(
    tmp_path: Path,
    backend: str,
    fault: str,
) -> None:
    """D1[8]: each finite phase abandons once and preserves the generation."""

    corpus = dense_global_corpus(5)
    temporary_parent = tmp_path / "endpoint-maps"
    temporary_parent.mkdir()
    origin = tmp_path / "origin"
    seed_generation(
        backend,
        origin,
        corpus,
        batch_size=2,
        temporary_parent=temporary_parent,
    )
    database = open_generation_database(
        backend, origin, "global_discovery", read_only=True
    )
    previous_digest = tree_digest(origin)
    delegate_source = make_physical_source(
        backend,
        database,
        scope="global_discovery",
        scan_batch_size=2,
        temporary_parent=temporary_parent,
    )
    source = ObservedSource(
        delegate_source,
        fail_after_first_node_batch=fault == "write",
    )
    candidate = tmp_path / f"candidate-{fault}"
    sink = ObservedSink(
        make_physical_sink(
            backend,
            candidate,
            scope="global_discovery",
            max_batch_size=2,
            temporary_parent=temporary_parent,
        ),
        fail_on=None if fault == "write" else fault,
    )
    try:
        with pytest.raises(PhasedTransferError) as caught:
            transfer_logical_graph(source, sink, batch_size=2)
    finally:
        close_database(backend, database)

    assert caught.value.phase == fault
    assert "injected physical" in str(caught.value.__cause__)
    assert source.snapshot is not None
    assert source.snapshot.close_calls == 1
    assert sink.abort_calls == 1
    assert sink.finalize_calls == 0
    assert not candidate.exists()
    assert tree_digest(origin) == previous_digest
    restored = export_generation(
        backend,
        origin,
        scope="global_discovery",
        batch_size=2,
        temporary_parent=temporary_parent,
    )
    assert restored.fingerprint == corpus.fingerprint
    assert list(temporary_parent.iterdir()) == []


@pytest.mark.parametrize("fault", ["success", "write", "fsync", "verify", "replace"])
def test_atomic_logical_graph_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    """D2[5]: accepted atomic publication and finite filesystem faults."""

    destination = tmp_path / "portable.logical-graph.jsonl"
    previous = b"previous-generation\n"
    destination.write_bytes(previous)
    corpus = portable_file_corpus()

    if fault == "write":
        published_nodes = failing_file_nodes()
    else:
        published_nodes = iter(corpus.nodes)

    if fault == "fsync":
        monkeypatch.setattr(
            logical_graph_file,
            "_fsync_file",
            lambda _descriptor: (_ for _ in ()).throw(
                OSError("injected fsync failure")
            ),
        )
    elif fault == "verify":
        monkeypatch.setattr(
            logical_graph_file,
            "_verify_complete_file",
            lambda _path: (_ for _ in ()).throw(
                ArtifactIntegrityError("injected verification failure")
            ),
        )
    elif fault == "replace":
        monkeypatch.setattr(
            logical_graph_file,
            "_replace_file",
            lambda _source, _destination: (_ for _ in ()).throw(
                OSError("injected replace failure")
            ),
        )
    elif fault == "success":
        monkeypatch.setattr(
            Path,
            "unlink",
            lambda _path, *, missing_ok=False: (_ for _ in ()).throw(
                AssertionError(
                    "successful publication attempted cleanup after os.replace"
                )
            ),
        )

    if fault == "success":
        certificate = logical_graph_file.publish_logical_graph_file(
            destination,
            corpus.schema,
            published_nodes,
            corpus.relations,
            counts=corpus.counts,
        )

        assert destination.read_bytes() != previous
        assert logical_graph_file.verify_logical_graph_file(destination) == certificate
        assert certificate.scope == "board"
        assert certificate.counts == corpus.counts
        assert len(certificate.schema_digest) == 64
        assert len(certificate.fingerprint) == 64
        assert len(certificate.stream_checksum) == 64
    else:
        with pytest.raises(PhasedTransferError) as caught:
            logical_graph_file.publish_logical_graph_file(
                destination,
                corpus.schema,
                published_nodes,
                corpus.relations,
                counts=corpus.counts,
            )

        assert caught.value.phase == "write"
        assert destination.read_bytes() == previous

    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []


@pytest.mark.parametrize(
    ("backend", "corrupt"),
    [
        ("ladybug", False),
        ("ladybug", True),
        ("grafx", False),
        ("grafx", True),
    ],
    ids=("ladybug-clean", "ladybug-corrupt", "grafx-clean", "grafx-corrupt"),
)
def test_restore_out_of_place_preserves_previous_generation(
    tmp_path: Path,
    backend: str,
    corrupt: bool,
) -> None:
    """D3[4]: artifact restore uses real sinks and never mutates the prior tree."""

    scope = "global_discovery"
    corpus = canonical_corpus(scope)
    artifact = tmp_path / "global.logical-graph.jsonl"
    logical_graph_file.publish_logical_graph_file(
        artifact,
        corpus.schema,
        corpus.nodes,
        corpus.relations,
        counts=corpus.counts,
    )
    if corrupt:
        corrupt_artifact_payload(artifact)

    temporary_parent = tmp_path / "endpoint-maps"
    temporary_parent.mkdir()
    previous = tmp_path / "previous"
    previous_corpus = one_node_corpus(scope, key="previous")
    seed_generation(
        backend,
        previous,
        previous_corpus,
        batch_size=3,
        temporary_parent=temporary_parent,
    )
    previous_digest = tree_digest(previous)
    candidate = tmp_path / "candidate"
    sink = ObservedSink(
        make_physical_sink(
            backend,
            candidate,
            scope=scope,
            max_batch_size=3,
            temporary_parent=temporary_parent,
        )
    )

    if corrupt:
        with pytest.raises(PhasedTransferError) as caught:
            restore_logical_graph_file(artifact, sink, batch_size=3)
        assert caught.value.phase == "write"
        assert sink.abort_calls == 1
        assert sink.finalize_calls == 0
        assert not candidate.exists()
    else:
        report = restore_logical_graph_file(artifact, sink, batch_size=3)
        assert report.counts == corpus.counts
        assert report.fingerprint == corpus.fingerprint
        assert sink.abort_calls == 0
        assert sink.finalize_calls == 1
        restored = export_generation(
            backend,
            candidate,
            scope=scope,
            batch_size=3,
            temporary_parent=temporary_parent,
        )
        assert restored.counts == corpus.counts
        assert restored.fingerprint == corpus.fingerprint
        if backend == "ladybug":
            assert sink.database_path.name == expected_ladybug_filename(scope)

    assert tree_digest(previous) == previous_digest
    still_previous = export_generation(
        backend,
        previous,
        scope=scope,
        batch_size=1,
        temporary_parent=temporary_parent,
    )
    assert still_previous.fingerprint == previous_corpus.fingerprint
    assert list(temporary_parent.iterdir()) == []
