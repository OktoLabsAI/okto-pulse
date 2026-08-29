from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
)

from okto_pulse.community.adapters.graph_rollout_capture import (
    invoke_captured_auto_commit,
)
from okto_pulse.community.adapters.graph_rollout_journal import (
    ROLLOUT_APPLICATION_ID,
    ROLLOUT_JOURNAL_FORMAT,
    ROLLOUT_SCHEMA_VERSION,
    CommunityGraphRolloutJournal,
    CommunityGraphRolloutMutationRecorder,
    GraphRolloutJournalConflict,
    RolloutEndpointIdentity,
    RolloutMutationToken,
)

SOURCE_SHA = "1" * 64
CANDIDATE_SHA = "2" * 64
SOURCE_FINGERPRINT = "a" * 64
TARGET_FINGERPRINT = "b" * 64
NEXT_SOURCE_FINGERPRINT = "c" * 64
NEXT_TARGET_FINGERPRINT = "d" * 64


def _journal(
    tmp_path: Path, *, board_id: str = "board-1"
) -> CommunityGraphRolloutJournal:
    root = tmp_path / "kg"
    root.mkdir(exist_ok=True)
    return CommunityGraphRolloutJournal(root, board_id)


def _identities(
    journal: CommunityGraphRolloutJournal,
    *,
    source_sha: str = SOURCE_SHA,
    candidate_sha: str | None = CANDIDATE_SHA,
) -> tuple[RolloutEndpointIdentity, RolloutEndpointIdentity]:
    board_root = journal.rollout_root.parent
    return (
        RolloutEndpointIdentity(
            backend="ladybug",
            binding_sha256=source_sha,
            generation="legacy",
            physical_path=board_root / "graph.lbug",
        ),
        RolloutEndpointIdentity(
            backend="grafx",
            binding_sha256=candidate_sha,
            generation="grafx-generation-1",
            physical_path=board_root / "grafx" / "grafx-generation-1",
            page_size=8192,
        ),
    )


def _start(journal: CommunityGraphRolloutJournal):
    source, candidate = _identities(journal)
    return journal.start(source=source, candidate=candidate)


def _prepare(
    journal: CommunityGraphRolloutJournal,
    number: int,
    *,
    binding_sha256: str = SOURCE_SHA,
):
    return journal.prepare_mutation(
        family="node.upsert",
        payload={"number": number, "nested": {"z": 2, "a": 1}},
        expected_binding_sha256=binding_sha256,
    )


def _open_canary_gate(journal: CommunityGraphRolloutJournal):
    rollout = journal.read()
    high_water = journal.capture_high_water()
    journal.record_checkpoint(
        direction="shadow",
        through_seq=high_water,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=SOURCE_FINGERPRINT,
        generation=rollout.candidate.generation,
    )
    journal.record_comparison_receipt(
        direction="shadow",
        through_seq=high_water,
        generation=rollout.candidate.generation,
        corpus_sha256="5" * 64,
        source_result_sha256="6" * 64,
        target_result_sha256="6" * 64,
        query_count=97,
    )
    return journal.compare_and_set_state(
        expected_state="shadowing",
        expected_version=rollout.state_version,
        new_state="canary_ready",
    )


def _simulate_legacy_canary_high_water_advance(
    journal: CommunityGraphRolloutJournal,
) -> None:
    """Reproduce the pre-fix crash gap with an authenticated stale watermark."""

    with sqlite3.connect(journal.database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM rollout_state").fetchone()
        assert row is not None
        body = dict(row)
        body.pop("row_sha256")
        body["next_seq"] = int(body["next_seq"]) + 1
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        row_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        connection.execute(
            "UPDATE rollout_state SET next_seq = ?, row_sha256 = ? WHERE singleton = 1",
            (body["next_seq"], row_sha256),
        )


def test_start_is_idempotent_and_persists_explicit_full_durability_schema(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    source, candidate = _identities(journal)

    first = journal.start(source=source, candidate=candidate)
    second = journal.start(source=source, candidate=candidate)

    assert first == second
    assert first.state == "shadowing"
    assert first.state_version == 1
    assert first.next_seq == 1
    assert journal.database_path == (
        tmp_path / "kg" / "boards" / "board-1" / "rollout" / "journal.sqlite3"
    )

    with sqlite3.connect(journal.database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA application_id").fetchone()[0] == (
            ROLLOUT_APPLICATION_ID
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            ROLLOUT_SCHEMA_VERSION
        )
        assert dict(connection.execute("SELECT key, value FROM journal_meta")) == {
            "format": ROLLOUT_JOURNAL_FORMAT,
            "schema_version": str(ROLLOUT_SCHEMA_VERSION),
        }
    connection.close()

    # Every journal method released its connection; this also exercises the
    # Windows file-sharing behavior that catches leaked sqlite handles.
    moved = journal.database_path.with_suffix(".moved")
    os.replace(journal.database_path, moved)
    os.replace(moved, journal.database_path)
    assert journal.read() == first


def test_start_rejects_different_persisted_identity(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    source, candidate = _identities(journal, candidate_sha="3" * 64)

    with pytest.raises(GraphRolloutJournalConflict) as failure:
        journal.start(source=source, candidate=candidate)

    assert failure.value.details["reason"] == "rollout_identity_conflict"


def test_absent_rollout_read_prepare_and_recorder_are_side_effect_free(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)

    assert journal.read_if_exists() is None
    assert (
        journal.prepare_if_active(
            family="node.upsert",
            payload={"id": "n-1"},
            expected_binding_sha256=SOURCE_SHA,
            backend="ladybug",
        )
        is None
    )
    recorder = CommunityGraphRolloutMutationRecorder(tmp_path / "kg")
    assert (
        recorder.prepare_mutation(
            board_id="board-1",
            binding_sha256=SOURCE_SHA,
            backend="ladybug",
            transaction_id="tx-1",
            family="create_node",
            payload={"format": "capture/1"},
        )
        is None
    )
    assert not (tmp_path / "kg" / "boards").exists()


def test_candidate_starts_unbound_then_is_certified_by_cas(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    source, candidate = _identities(journal, candidate_sha=None)
    initial = journal.start(source=source, candidate=candidate)

    assert initial.candidate.binding_sha256 is None
    assert initial.candidate.page_size == 8192
    with pytest.raises(GraphRolloutJournalConflict) as not_certified:
        journal.compare_and_set_state(
            expected_state="shadowing",
            expected_version=initial.state_version,
            new_state="canary_ready",
        )
    assert not_certified.value.details["reason"] == "candidate_binding_not_certified"

    certified = journal.certify_candidate(
        expected_version=initial.state_version,
        candidate_binding_sha256=CANDIDATE_SHA,
    )
    retry = journal.certify_candidate(
        expected_version=initial.state_version,
        candidate_binding_sha256=CANDIDATE_SHA,
    )
    assert retry == certified
    assert certified.candidate.binding_sha256 == CANDIDATE_SHA
    assert certified.state_version == 2
    # Retrying the pre-snapshot start identity remains idempotent after seal.
    assert journal.start(source=source, candidate=candidate) == certified


def test_grafx_candidate_requires_persisted_page_geometry(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    source, candidate = _identities(journal)
    candidate = RolloutEndpointIdentity(
        backend="grafx",
        binding_sha256=candidate.binding_sha256,
        generation=candidate.generation,
        physical_path=candidate.physical_path,
        page_size=None,
    )

    with pytest.raises(GraphCapabilityUnavailable):
        journal.start(source=source, candidate=candidate)
    assert not journal.rollout_root.exists()


def test_certified_candidate_replacement_is_fresh_cas_and_checkpoint_identified(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    initial = _start(journal)
    old_marker = initial.candidate.physical_path / "certified.marker"
    old_marker.parent.mkdir(parents=True)
    old_marker.write_text("keep", encoding="utf-8")
    old_checkpoint = journal.record_checkpoint(
        direction="shadow",
        through_seq=0,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=SOURCE_FINGERPRINT,
        generation=initial.candidate.generation,
    )
    replacement = RolloutEndpointIdentity(
        backend="grafx",
        binding_sha256="3" * 64,
        generation="grafx-generation-2",
        physical_path=journal.rollout_root.parent / "grafx" / "grafx-generation-2",
        page_size=4096,
    )

    replaced = journal.replace_candidate(
        expected_version=initial.state_version,
        expected_candidate=initial.candidate,
        replacement=replacement,
    )

    assert replaced.candidate == replacement
    assert replaced.state_version == 2
    assert old_marker.read_text(encoding="utf-8") == "keep"
    # Replacement cannot erase the identity used by the preceding ACK.
    assert journal.read_checkpoint("shadow") == old_checkpoint
    new_checkpoint = journal.record_checkpoint(
        direction="shadow",
        through_seq=0,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=SOURCE_FINGERPRINT,
        generation=replacement.generation,
    )
    assert new_checkpoint.ack_version == 2
    assert new_checkpoint.binding_sha256 == replacement.binding_sha256
    assert new_checkpoint.physical_path == replacement.physical_path
    assert new_checkpoint.page_size == replacement.page_size

    with pytest.raises(GraphRolloutJournalConflict):
        journal.replace_candidate(
            expected_version=initial.state_version,
            expected_candidate=initial.candidate,
            replacement=RolloutEndpointIdentity(
                backend="grafx",
                binding_sha256="4" * 64,
                generation="grafx-generation-3",
                physical_path=journal.rollout_root.parent
                / "grafx"
                / "grafx-generation-3",
                page_size=8192,
            ),
        )


def test_endpoint_identity_is_board_canonical_and_not_an_alias(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    source, candidate = _identities(journal)
    candidate = RolloutEndpointIdentity(
        backend="grafx",
        binding_sha256=candidate.binding_sha256,
        generation=candidate.generation,
        physical_path=journal.rollout_root.parent.parent
        / "another-board"
        / "grafx"
        / candidate.generation,
        page_size=candidate.page_size,
    )

    with pytest.raises(GraphCapabilityUnavailable) as failure:
        journal.start(source=source, candidate=candidate)

    assert failure.value.details["reason"] == "rollout_endpoint_identity_invalid"
    assert not journal.rollout_root.exists()


def test_state_changes_use_state_and_version_cas(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _start(journal)

    ready = _open_canary_gate(journal)

    assert ready.state == "canary_ready"
    assert ready.state_version == 2
    with pytest.raises(GraphRolloutJournalConflict) as failure:
        journal.compare_and_set_state(
            expected_state="shadowing",
            expected_version=1,
            new_state="grafx_active_rollback_open",
        )
    assert failure.value.details["reason"] == "stale_state_cas"
    assert journal.read() == ready


def test_canary_transition_requires_current_matching_checkpoint_and_receipt(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    rollout = _start(journal)

    with pytest.raises(GraphRolloutJournalConflict) as missing_checkpoint:
        journal.compare_and_set_state(
            expected_state="shadowing",
            expected_version=rollout.state_version,
            new_state="canary_ready",
        )
    assert missing_checkpoint.value.details["reason"] == "canary_checkpoint_missing"

    journal.record_checkpoint(
        direction="shadow",
        through_seq=0,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=SOURCE_FINGERPRINT,
        generation=rollout.candidate.generation,
    )
    with pytest.raises(GraphRolloutJournalConflict) as missing_receipt:
        journal.compare_and_set_state(
            expected_state="shadowing",
            expected_version=rollout.state_version,
            new_state="canary_ready",
        )
    assert missing_receipt.value.details["reason"] == (
        "canary_comparison_receipt_missing"
    )


def test_canary_transition_rejects_stale_high_water_and_fingerprint_divergence(
    tmp_path: Path,
) -> None:
    stale = _journal(tmp_path, board_id="stale-board")
    rollout = _start(stale)
    stale.record_checkpoint(
        direction="shadow",
        through_seq=0,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=SOURCE_FINGERPRINT,
        generation=rollout.candidate.generation,
    )
    stale.record_comparison_receipt(
        direction="shadow",
        through_seq=0,
        generation=rollout.candidate.generation,
        corpus_sha256="5" * 64,
        source_result_sha256="6" * 64,
        target_result_sha256="6" * 64,
        query_count=97,
    )
    mutation = _prepare(stale, 1)
    stale.mark_source_committed(
        seq=mutation.seq, payload_sha256=mutation.payload_sha256
    )
    with pytest.raises(GraphRolloutJournalConflict) as stale_checkpoint:
        stale.compare_and_set_state(
            expected_state="shadowing",
            expected_version=rollout.state_version,
            new_state="canary_ready",
        )
    assert stale_checkpoint.value.details["reason"] == "canary_checkpoint_stale"

    divergent = _journal(tmp_path, board_id="divergent-board")
    divergent_rollout = _start(divergent)
    divergent.record_checkpoint(
        direction="shadow",
        through_seq=0,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=TARGET_FINGERPRINT,
        generation=divergent_rollout.candidate.generation,
    )
    divergent.record_comparison_receipt(
        direction="shadow",
        through_seq=0,
        generation=divergent_rollout.candidate.generation,
        corpus_sha256="5" * 64,
        source_result_sha256="6" * 64,
        target_result_sha256="6" * 64,
        query_count=97,
    )
    with pytest.raises(GraphRolloutJournalConflict) as fingerprint:
        divergent.compare_and_set_state(
            expected_state="shadowing",
            expected_version=divergent_rollout.state_version,
            new_state="canary_ready",
        )
    assert fingerprint.value.details["reason"] == "canary_fingerprint_diverged"


def test_canary_transition_refuses_any_divergence_at_current_candidate_watermark(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    rollout = _start(journal)
    journal.record_checkpoint(
        direction="shadow",
        through_seq=0,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=SOURCE_FINGERPRINT,
        generation=rollout.candidate.generation,
    )
    journal.record_comparison_receipt(
        direction="shadow",
        through_seq=0,
        generation=rollout.candidate.generation,
        corpus_sha256="5" * 64,
        source_result_sha256="6" * 64,
        target_result_sha256="6" * 64,
        query_count=97,
    )
    journal.record_divergence(
        direction="shadow",
        through_seq=0,
        expected_fingerprint=SOURCE_FINGERPRINT,
        actual_fingerprint=TARGET_FINGERPRINT,
        generation=rollout.candidate.generation,
        details={"kind": "result"},
    )

    with pytest.raises(GraphRolloutJournalConflict) as failure:
        journal.compare_and_set_state(
            expected_state="shadowing",
            expected_version=rollout.state_version,
            new_state="canary_ready",
        )
    assert failure.value.details["reason"] == "canary_divergence_present"


def test_canary_ready_refuses_all_write_entrypoints_before_allocating_sequence(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    ready = _open_canary_gate(journal)

    write_attempts = (
        lambda: journal.prepare_mutation(
            family="node.upsert",
            payload={"number": 1},
            expected_binding_sha256=SOURCE_SHA,
            backend="ladybug",
        ),
        lambda: journal.prepare_if_active(
            family="node.upsert",
            payload={"number": 2},
            expected_binding_sha256=SOURCE_SHA,
            backend="ladybug",
        ),
        lambda: journal.close_rollback_before_write_if_active(
            expected_binding_sha256=SOURCE_SHA,
            backend="ladybug",
        ),
    )

    for attempt in write_attempts:
        with pytest.raises(GraphRolloutJournalConflict) as failure:
            attempt()
        assert failure.value.details["reason"] == "canary_recovery_required"

    persisted = journal.read()
    assert persisted == ready
    assert persisted.next_seq == 1
    with sqlite3.connect(journal.database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM logical_mutations").fetchone()[0]
            == 0
        )


def test_active_transition_atomically_revalidates_a_stale_canary_high_water(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    ready = _open_canary_gate(journal)
    assert (
        journal.require_current_canary_gate(expected_version=ready.state_version)
        == ready
    )

    _simulate_legacy_canary_high_water_advance(journal)

    with pytest.raises(GraphRolloutJournalConflict) as explicit_gate:
        journal.require_current_canary_gate(expected_version=ready.state_version)
    assert explicit_gate.value.details["reason"] == "canary_checkpoint_stale"
    with pytest.raises(GraphRolloutJournalConflict) as transition_gate:
        journal.compare_and_set_state(
            expected_state="canary_ready",
            expected_version=ready.state_version,
            new_state="grafx_active_rollback_open",
        )
    assert transition_gate.value.details["reason"] == "canary_checkpoint_stale"
    persisted = journal.read()
    assert persisted.state == "canary_ready"
    assert persisted.next_seq == 2


def test_legal_cutover_lifecycle_is_explicitly_persisted(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    current = _open_canary_gate(journal)

    for next_state in (
        "grafx_active_rollback_open",
        "grafx_active_rollback_closed",
        "completed",
    ):
        current = journal.compare_and_set_state(
            expected_state=current.state,
            expected_version=current.state_version,
            new_state=next_state,
        )

    assert current.state == "completed"
    assert current.state_version == 5
    assert journal.read() == current


def test_legal_rollback_lifecycle_is_explicitly_persisted(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    current = _open_canary_gate(journal)
    for next_state in (
        "grafx_active_rollback_open",
        "rolled_back",
        "completed",
    ):
        current = journal.compare_and_set_state(
            expected_state=current.state,
            expected_version=current.state_version,
            new_state=next_state,
        )
    assert current.state == "completed"


@pytest.mark.parametrize(
    ("source_state", "target_state"),
    [
        (source, target)
        for source, allowed in {
            "shadowing": {"canary_ready"},
            "canary_ready": {"grafx_active_rollback_open"},
            "grafx_active_rollback_open": {
                "grafx_active_rollback_closed",
                "rolled_back",
            },
            "grafx_active_rollback_closed": {"completed"},
            "rolled_back": {"completed"},
            "completed": set(),
            "erased": set(),
        }.items()
        for target in (
            "shadowing",
            "canary_ready",
            "grafx_active_rollback_open",
            "grafx_active_rollback_closed",
            "rolled_back",
            "completed",
            "erased",
        )
        if target != source and target not in allowed
    ],
)
def test_every_illegal_lifecycle_jump_is_rejected(
    tmp_path: Path, source_state: str, target_state: str
) -> None:
    journal = _journal(tmp_path)
    current = _start(journal)
    paths = {
        "shadowing": (),
        "canary_ready": ("canary_ready",),
        "grafx_active_rollback_open": (
            "canary_ready",
            "grafx_active_rollback_open",
        ),
        "grafx_active_rollback_closed": (
            "canary_ready",
            "grafx_active_rollback_open",
            "grafx_active_rollback_closed",
        ),
        "rolled_back": (
            "canary_ready",
            "grafx_active_rollback_open",
            "rolled_back",
        ),
        "completed": (
            "canary_ready",
            "grafx_active_rollback_open",
            "rolled_back",
            "completed",
        ),
    }
    if source_state == "erased":
        current = journal.close_for_privacy(expected_version=current.state_version)
    else:
        for next_state in paths[source_state]:
            if next_state == "canary_ready":
                current = _open_canary_gate(journal)
            else:
                current = journal.compare_and_set_state(
                    expected_state=current.state,
                    expected_version=current.state_version,
                    new_state=next_state,
                )

    with pytest.raises(GraphRolloutJournalConflict) as failure:
        journal.compare_and_set_state(
            expected_state=current.state,
            expected_version=current.state_version,
            new_state=target_state,
        )
    assert failure.value.details["reason"] in {
        "illegal_state_transition",
        "terminal_state",
    }


def test_prepare_first_allocates_monotonic_sequence_and_canonical_payload(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    _start(journal)

    first = _prepare(journal, 1)
    second = _prepare(journal, 2)

    assert (first.seq, second.seq) == (1, 2)
    assert first.status == "prepared"
    assert first.terminal_at_utc is None
    assert first.payload_json == '{"nested":{"a":1,"z":2},"number":1}'
    assert first.payload_sha256 != second.payload_sha256
    assert journal.read().next_seq == 3


def test_prepare_is_fenced_by_binding_identity_on_both_active_sides(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    _start(journal)

    with pytest.raises(GraphRolloutJournalConflict) as failure:
        _prepare(journal, 1, binding_sha256=CANDIDATE_SHA)
    assert failure.value.details["reason"] == "binding_fence_mismatch"

    ready = _open_canary_gate(journal)
    active = journal.compare_and_set_state(
        expected_state="canary_ready",
        expected_version=ready.state_version,
        new_state="grafx_active_rollback_open",
    )
    mutation = _prepare(journal, 2, binding_sha256=CANDIDATE_SHA)
    assert mutation.expected_binding_sha256 == CANDIDATE_SHA
    assert active.state == "grafx_active_rollback_open"
    assert journal.read().state == "grafx_active_rollback_closed"


def test_common_write_fence_closes_rollback_atomically_without_mutation_row(
    tmp_path: Path,
) -> None:
    absent = _journal(tmp_path, board_id="absent-board")
    assert (
        absent.close_rollback_before_write_if_active(
            expected_binding_sha256=SOURCE_SHA,
            backend="ladybug",
        )
        is None
    )
    assert not absent.rollout_root.exists()

    journal = _journal(tmp_path)
    shadowing = _start(journal)
    assert (
        journal.close_rollback_before_write_if_active(
            expected_binding_sha256=SOURCE_SHA,
            backend="ladybug",
        )
        == shadowing
    )
    ready = _open_canary_gate(journal)
    opened = journal.compare_and_set_state(
        expected_state=ready.state,
        expected_version=ready.state_version,
        new_state="grafx_active_rollback_open",
    )

    with pytest.raises(GraphRolloutJournalConflict):
        journal.close_rollback_before_write_if_active(
            expected_binding_sha256=SOURCE_SHA,
            backend="grafx",
        )
    assert journal.read() == opened

    recorder = CommunityGraphRolloutMutationRecorder(tmp_path / "kg")
    closed = recorder.close_rollback_before_write_if_active(
        "board-1",
        CANDIDATE_SHA,
        "grafx",
    )
    retry = journal.close_rollback_before_write_if_active(
        expected_binding_sha256=CANDIDATE_SHA,
        backend="grafx",
    )
    assert closed is not None
    assert closed.state == "grafx_active_rollback_closed"
    assert retry == closed
    with sqlite3.connect(journal.database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM logical_mutations").fetchone()[0]
            == 0
        )
    connection.close()

    completed = journal.compare_and_set_state(
        expected_state=closed.state,
        expected_version=closed.state_version,
        new_state="completed",
    )
    assert completed.state == "completed"
    assert (
        recorder.close_rollback_before_write_if_active(
            "board-1",
            CANDIDATE_SHA,
            "grafx",
        )
        == completed
    )


@pytest.mark.parametrize(
    ("terminal_state", "backend", "binding_sha256"),
    [
        ("grafx_active_rollback_closed", "grafx", CANDIDATE_SHA),
        ("rolled_back", "ladybug", SOURCE_SHA),
    ],
)
def test_completed_rollout_releases_capture_but_preserves_durable_history(
    tmp_path: Path,
    terminal_state: str,
    backend: str,
    binding_sha256: str,
) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    retained = _prepare(journal, 1)
    journal.mark_source_committed(
        seq=retained.seq,
        payload_sha256=retained.payload_sha256,
    )
    ready = _open_canary_gate(journal)
    opened = journal.compare_and_set_state(
        expected_state=ready.state,
        expected_version=ready.state_version,
        new_state="grafx_active_rollback_open",
    )
    terminal = journal.compare_and_set_state(
        expected_state=opened.state,
        expected_version=opened.state_version,
        new_state=terminal_state,
    )
    completed = journal.compare_and_set_state(
        expected_state=terminal.state,
        expected_version=terminal.state_version,
        new_state="completed",
    )
    recorder = CommunityGraphRolloutMutationRecorder(tmp_path / "kg")
    source_calls: list[str] = []

    result = invoke_captured_auto_commit(
        lambda: source_calls.append("applied") or "written",
        recorder=recorder,
        board_id="board-1",
        backend=backend,
        binding_sha256=binding_sha256,
        family="create_node",
        args=("Entity", "node-after-complete", {}),
        kwargs={},
    )

    assert result == "written"
    assert source_calls == ["applied"]
    assert journal.read() == completed
    assert journal.capture_high_water() == retained.seq
    page = journal.list_committed()
    assert [item.seq for item in page.items] == [retained.seq]
    assert (
        recorder.close_rollback_before_write_if_active(
            "board-1",
            binding_sha256,
            backend,
        )
        == completed
    )


def test_terminal_outcome_is_idempotent_but_cannot_be_reversed(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    prepared = _prepare(journal, 1)

    committed = journal.mark_source_committed(
        seq=prepared.seq, payload_sha256=prepared.payload_sha256
    )
    retry = journal.mark_source_committed(
        seq=prepared.seq, payload_sha256=prepared.payload_sha256
    )

    assert committed == retry
    assert committed.status == "source_committed"
    assert committed.terminal_at_utc is not None
    with pytest.raises(GraphRolloutJournalConflict) as failure:
        journal.mark_source_abandoned(
            seq=prepared.seq, payload_sha256=prepared.payload_sha256
        )
    assert failure.value.details["reason"] == "mutation_terminal_outcome_conflict"


def test_terminalization_requires_payload_checksum_fence(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    prepared = _prepare(journal, 1)

    with pytest.raises(GraphRolloutJournalConflict) as failure:
        journal.mark_source_committed(seq=prepared.seq, payload_sha256="f" * 64)

    assert failure.value.details["reason"] == "mutation_payload_fence_mismatch"


def test_committed_listing_stops_before_prepared_gap_and_advances_abandoned_gap(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    first = _prepare(journal, 1)
    gap = _prepare(journal, 2)
    third = _prepare(journal, 3)
    journal.mark_source_committed(seq=first.seq, payload_sha256=first.payload_sha256)
    journal.mark_source_committed(seq=third.seq, payload_sha256=third.payload_sha256)

    blocked = journal.list_committed()
    assert [item.seq for item in blocked.items] == [1]
    assert blocked.high_water == 1
    assert blocked.next_cursor == 1

    journal.mark_source_abandoned(seq=gap.seq, payload_sha256=gap.payload_sha256)
    unblocked = journal.list_committed(cursor=blocked.next_cursor)
    assert [item.seq for item in unblocked.items] == [3]
    assert unblocked.high_water == 3
    assert unblocked.next_cursor == 3


def test_committed_page_keeps_one_captured_high_water_across_bounded_pages(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    for number in range(1, 4):
        mutation = _prepare(journal, number)
        journal.mark_source_committed(
            seq=mutation.seq, payload_sha256=mutation.payload_sha256
        )

    first = journal.list_committed(limit=1)
    second = journal.list_committed(
        cursor=first.next_cursor, high_water=first.high_water, limit=1
    )
    third = journal.list_committed(
        cursor=second.next_cursor, high_water=second.high_water, limit=1
    )

    assert [page.high_water for page in (first, second, third)] == [3, 3, 3]
    assert [page.items[0].seq for page in (first, second, third)] == [1, 2, 3]
    assert [page.has_more for page in (first, second, third)] == [True, True, False]
    assert third.next_cursor == 3


def test_capture_compatible_recorder_returns_token_and_terminalizes(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    recorder = CommunityGraphRolloutMutationRecorder(tmp_path / "kg")

    token = recorder.prepare_mutation(
        board_id="board-1",
        binding_sha256=SOURCE_SHA,
        backend="ladybug",
        transaction_id="tx-1",
        family="create_node",
        payload={"format": "capture/1", "arguments_sha256": "f" * 64},
    )

    assert isinstance(token, RolloutMutationToken)
    recorder.mark_source_committed(token)
    page = journal.list_committed()
    assert [item.seq for item in page.items] == [token.seq]
    assert page.items[0].payload == {
        "capture_format": "okto-pulse-board-rollout-capture/1",
        "payload": {"arguments_sha256": "f" * 64, "format": "capture/1"},
        "transaction_id": "tx-1",
    }


def test_crash_after_source_apply_is_resolved_by_fenced_full_snapshot(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    recorder = CommunityGraphRolloutMutationRecorder(tmp_path / "kg")
    token = recorder.prepare_mutation(
        board_id="board-1",
        binding_sha256=SOURCE_SHA,
        backend="ladybug",
        transaction_id="tx-crash",
        family="create_node",
        payload={"format": "capture/1", "arguments_sha256": "e" * 64},
    )
    assert isinstance(token, RolloutMutationToken)
    # Source applied, process crashed before terminal ACK. Ambiguous explicitly
    # remains prepared and a restarted journal sees the allocated high-water.
    recorder.mark_source_ambiguous(token, error_type="ProcessCrash")
    restarted = CommunityGraphRolloutJournal(tmp_path / "kg", "board-1")
    high_water = restarted.capture_high_water()
    assert high_water == token.seq == 1
    assert restarted.list_committed().high_water == 0

    checkpoint = restarted.reconcile_snapshot(
        direction="shadow",
        through_seq=high_water,
        expected_binding_sha256=SOURCE_SHA,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=SOURCE_FINGERPRINT,
        generation="grafx-generation-1",
    )

    assert checkpoint.through_seq == 1
    with sqlite3.connect(restarted.database_path) as connection:
        assert (
            connection.execute(
                "SELECT status FROM logical_mutations WHERE seq = 1"
            ).fetchone()[0]
            == "source_reconciled"
        )
    connection.close()
    reconciled = restarted.list_committed(cursor=0)
    assert reconciled.items == ()
    assert reconciled.high_water == 1
    assert reconciled.next_cursor == 1

    second = _prepare(restarted, 2)
    restarted.mark_source_committed(
        seq=second.seq, payload_sha256=second.payload_sha256
    )
    assert [item.seq for item in restarted.list_committed(cursor=1).items] == [2]


def test_hot_mutation_path_does_not_repeat_expensive_quick_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    calls = 0
    original = journal._verify_integrity

    def counted(connection: sqlite3.Connection) -> None:
        nonlocal calls
        calls += 1
        original(connection)

    monkeypatch.setattr(journal, "_verify_integrity", counted)
    mutation = _prepare(journal, 1)
    journal.mark_source_committed(
        seq=mutation.seq, payload_sha256=mutation.payload_sha256
    )
    journal.list_committed()
    assert calls == 0

    journal.verify()
    assert calls == 1


def test_shadow_checkpoint_ack_is_monotonic_and_idempotent(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    first = _prepare(journal, 1)
    second = _prepare(journal, 2)
    journal.mark_source_committed(seq=first.seq, payload_sha256=first.payload_sha256)
    journal.mark_source_committed(seq=second.seq, payload_sha256=second.payload_sha256)

    checkpoint = journal.record_checkpoint(
        direction="shadow",
        through_seq=1,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=TARGET_FINGERPRINT,
        generation="grafx-generation-1",
    )
    same = journal.record_checkpoint(
        direction="shadow",
        through_seq=1,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=TARGET_FINGERPRINT,
        generation="grafx-generation-1",
    )
    advanced = journal.record_checkpoint(
        direction="shadow",
        through_seq=2,
        source_fingerprint=NEXT_SOURCE_FINGERPRINT,
        target_fingerprint=NEXT_TARGET_FINGERPRINT,
        generation="grafx-generation-1",
    )

    assert same == checkpoint
    assert checkpoint.ack_version == 1
    assert advanced.ack_version == 2
    assert journal.read_checkpoint("shadow") == advanced
    with pytest.raises(GraphRolloutJournalConflict) as failure:
        journal.record_checkpoint(
            direction="shadow",
            through_seq=1,
            source_fingerprint=SOURCE_FINGERPRINT,
            target_fingerprint=TARGET_FINGERPRINT,
            generation="grafx-generation-1",
        )
    assert failure.value.details["reason"] == "checkpoint_regression"


def test_checkpoint_cannot_ack_prepared_mutation_or_change_same_seq_evidence(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    prepared = _prepare(journal, 1)

    with pytest.raises(GraphRolloutJournalConflict) as beyond:
        journal.record_checkpoint(
            direction="shadow",
            through_seq=1,
            source_fingerprint=SOURCE_FINGERPRINT,
            target_fingerprint=TARGET_FINGERPRINT,
            generation="grafx-generation-1",
        )
    assert beyond.value.details["reason"] == "checkpoint_beyond_terminal_high_water"

    journal.mark_source_abandoned(
        seq=prepared.seq, payload_sha256=prepared.payload_sha256
    )
    journal.record_checkpoint(
        direction="shadow",
        through_seq=1,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=TARGET_FINGERPRINT,
        generation="grafx-generation-1",
    )
    with pytest.raises(GraphRolloutJournalConflict) as mismatch:
        journal.record_checkpoint(
            direction="shadow",
            through_seq=1,
            source_fingerprint=NEXT_SOURCE_FINGERPRINT,
            target_fingerprint=TARGET_FINGERPRINT,
            generation="grafx-generation-1",
        )
    assert mismatch.value.details["reason"] == "checkpoint_same_seq_mismatch"


def test_reverse_checkpoint_is_tied_to_ladybug_generation(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _start(journal)

    checkpoint = journal.record_checkpoint(
        direction="reverse",
        through_seq=0,
        source_fingerprint=SOURCE_FINGERPRINT,
        target_fingerprint=TARGET_FINGERPRINT,
        generation="legacy",
    )

    assert checkpoint.direction == "reverse"
    with pytest.raises(GraphRolloutJournalConflict) as failure:
        journal.record_checkpoint(
            direction="reverse",
            through_seq=0,
            source_fingerprint=SOURCE_FINGERPRINT,
            target_fingerprint=TARGET_FINGERPRINT,
            generation="grafx-generation-1",
        )
    assert failure.value.details["reason"] == "checkpoint_generation_mismatch"


def test_divergence_is_durable_authenticated_and_listable(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _start(journal)

    divergence = journal.record_divergence(
        direction="shadow",
        through_seq=0,
        expected_fingerprint=SOURCE_FINGERPRINT,
        actual_fingerprint=TARGET_FINGERPRINT,
        generation="grafx-generation-1",
        details={"query": "Q-1", "counts": {"actual": 4, "expected": 3}},
    )

    assert divergence.divergence_id == 1
    assert divergence.details == {
        "counts": {"actual": 4, "expected": 3},
        "query": "Q-1",
    }
    assert journal.list_divergences() == (divergence,)


def test_full_snapshot_divergence_can_cover_ambiguous_prepared_high_water(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    rollout = _start(journal)
    prepared = _prepare(journal, 1)

    divergence = journal.record_divergence(
        direction="shadow",
        through_seq=prepared.seq,
        expected_fingerprint=SOURCE_FINGERPRINT,
        actual_fingerprint=TARGET_FINGERPRINT,
        generation=rollout.candidate.generation,
        details={"phase": "full_snapshot"},
    )

    assert divergence.through_seq == journal.capture_high_water() == 1
    # A divergence is evidence, not an ACK; ambiguity remains prepared and
    # committed replay is still blocked before that sequence.
    assert journal.list_committed().high_water == 0


def test_matching_result_comparison_receipt_is_authenticated_and_paged(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    rollout = _start(journal)

    shadow = journal.record_comparison_receipt(
        direction="shadow",
        through_seq=0,
        generation=rollout.candidate.generation,
        corpus_sha256="5" * 64,
        source_result_sha256="6" * 64,
        target_result_sha256="6" * 64,
        query_count=97,
    )
    retry = journal.record_comparison_receipt(
        direction="shadow",
        through_seq=0,
        generation=rollout.candidate.generation,
        corpus_sha256="5" * 64,
        source_result_sha256="6" * 64,
        target_result_sha256="6" * 64,
        query_count=97,
    )
    reverse = journal.record_comparison_receipt(
        direction="reverse",
        through_seq=0,
        generation=rollout.source.generation,
        corpus_sha256="5" * 64,
        source_result_sha256="7" * 64,
        target_result_sha256="7" * 64,
        query_count=97,
    )

    assert retry == shadow
    assert shadow.binding_sha256 == CANDIDATE_SHA
    assert shadow.physical_path == rollout.candidate.physical_path
    assert shadow.page_size == 8192
    assert shadow.source_result_sha256 == shadow.target_result_sha256
    assert journal.list_comparison_receipts(limit=1) == (shadow,)
    assert journal.list_comparison_receipts(cursor=shadow.receipt_id) == (reverse,)
    assert journal.latest_comparison_receipt() == reverse
    assert journal.latest_comparison_receipt("shadow") == shadow


def test_divergent_results_cannot_be_persisted_as_success_receipt(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    rollout = _start(journal)

    with pytest.raises(GraphCapabilityUnavailable) as failure:
        journal.record_comparison_receipt(
            direction="shadow",
            through_seq=0,
            generation=rollout.candidate.generation,
            corpus_sha256="5" * 64,
            source_result_sha256="6" * 64,
            target_result_sha256="7" * 64,
            query_count=97,
        )

    assert failure.value.details["reason"] == "comparison_receipt_invalid"
    assert journal.latest_comparison_receipt() is None


def test_payload_and_state_tampering_fail_closed(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    mutation = _prepare(journal, 1)
    journal.mark_source_committed(
        seq=mutation.seq, payload_sha256=mutation.payload_sha256
    )
    with sqlite3.connect(journal.database_path) as connection:
        connection.execute(
            "UPDATE logical_mutations SET payload_json = ? WHERE seq = 1",
            ('{"number":999}',),
        )

    with pytest.raises(GraphCorruption):
        journal.list_committed()

    with sqlite3.connect(journal.database_path) as connection:
        connection.execute(
            "UPDATE rollout_state SET state = 'canary_ready' WHERE singleton = 1"
        )

    with pytest.raises(GraphCorruption):
        journal.read()


def test_unknown_schema_version_fails_closed(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _start(journal)
    with sqlite3.connect(journal.database_path) as connection:
        connection.execute("PRAGMA user_version=999")

    with pytest.raises(GraphCorruption):
        journal.read()


def test_privacy_closes_terminally_then_erases_all_rollout_residues(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    initial = _start(journal)
    mutation = _prepare(journal, 1)
    journal.mark_source_committed(
        seq=mutation.seq, payload_sha256=mutation.payload_sha256
    )
    closed = journal.close_for_privacy(expected_version=initial.state_version)
    assert closed.state == "erased"
    assert closed.state_version == 2

    with pytest.raises(GraphRolloutJournalConflict):
        _prepare(journal, 2)

    residue = journal.rollout_root / "journal.sqlite3.interrupted-cleanup"
    residue.write_bytes(b"residue")
    touched: list[None] = []
    proof = journal.erase_privacy_storage(
        invalidated_state_version=closed.state_version,
        before_mutation=lambda: touched.append(None),
    )

    assert touched
    assert proof.invalidated_state_version == 2
    assert proof.storage_absent is True
    assert proof.files_removed >= 2
    assert proof.directories_removed == 1
    assert all(not path.exists() for path in proof.checked_paths)
    assert not journal.rollout_root.exists()
    assert journal.privacy_storage_present() is False


def test_privacy_two_phase_operation_is_retryable_after_absence(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    initial = _start(journal)

    closed = journal.close_for_privacy(expected_version=initial.state_version)
    closed_retry = journal.close_for_privacy(expected_version=initial.state_version)
    proof = journal.erase_privacy_storage(
        invalidated_state_version=closed.state_version
    )
    retry = journal.erase_privacy_storage(
        invalidated_state_version=proof.invalidated_state_version
    )

    assert closed_retry == closed
    assert proof.storage_absent is True
    assert retry.storage_absent is True
    assert retry.files_removed == 0
    assert retry.directories_removed == 0


def test_privacy_storage_cannot_be_erased_before_durable_invalidation(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    _start(journal)

    with pytest.raises(GraphRolloutJournalConflict) as failure:
        journal.erase_privacy_storage()

    assert failure.value.details["reason"] == "privacy_storage_not_invalidated"
    assert journal.database_path.exists()
