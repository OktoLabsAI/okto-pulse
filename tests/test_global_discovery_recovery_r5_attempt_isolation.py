from __future__ import annotations

import importlib.util
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

import okto_pulse.core.ports.global_discovery_recovery_control as recovery_contract
from okto_pulse.community.adapters import (
    global_discovery_recovery as recovery_module,
)
from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecovery,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    active_pointer_path,
    read_active_generation,
)


NOW = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
_HELPERS: ModuleType | None = None


def _required(owner: object, name: str):
    value = getattr(owner, name, None)
    assert value is not None, f"R5 contract is missing {name}"
    return value


def _adapter_helpers() -> ModuleType:
    global _HELPERS
    if _HELPERS is not None:
        return _HELPERS
    path = Path(__file__).with_name("test_global_discovery_recovery_adapter.py")
    spec = importlib.util.spec_from_file_location("_r5_recovery_adapter_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _HELPERS = module
    return module


def _attempt_id(run_id: str, epoch: int) -> str:
    factory = _required(recovery_contract, "recovery_attempt_id")
    return str(factory(run_id, epoch))


def _call(adapter, *, run_id: str, epoch: int, attempt_id: str, before):
    helpers = _adapter_helpers()
    return adapter.rebuild_candidate_and_cutover(
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
        expected_live_sha256=before.sha256,
        boards=helpers._boards(),  # noqa: SLF001
    )


def _attempt_quarantine(live: Path, attempt_id: str) -> Path:
    return live.parent / "quarantine" / "global-discovery" / Path(attempt_id)


def _write_terminal_attempt(
    *,
    live: Path,
    run_id: str,
    epoch: int,
    age_hours: float,
) -> str:
    attempt_id = _attempt_id(run_id, epoch)
    directory = _attempt_quarantine(live, attempt_id)
    directory.mkdir(parents=True)
    terminal_at = NOW - timedelta(hours=age_hours)
    assert recovery_module._write_journal(  # noqa: SLF001
        directory / "recovery_journal.json",
        {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "epoch": epoch,
            "phase": "rolled_back",
            "outcome": "rolled_back",
            "rolled_back_at": terminal_at.isoformat(),
        },
    )
    return attempt_id


def test_physical_port_requires_run_epoch_and_attempt_identity() -> None:
    parameters = inspect.signature(
        CommunityGlobalDiscoveryRecovery.rebuild_candidate_and_cutover
    ).parameters
    assert {"run_id", "epoch", "attempt_id"}.issubset(parameters)

    assert _attempt_id("gdr_r5_identity", 1) == "gdr_r5_identity/attempt-1"
    assert _attempt_id("gdr_r5_identity", 2) == "gdr_r5_identity/attempt-2"


def test_rolled_back_epoch_one_does_not_poison_epoch_two_or_change_on_replay(
    tmp_path: Path,
) -> None:
    helpers = _adapter_helpers()
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"original-primary")
    live.with_name(live.name + ".wal").write_bytes(b"original-wal")
    run_id = "gdr_r5_attempt_isolation"
    attempt_one = _attempt_id(run_id, 1)
    attempt_two = _attempt_id(run_id, 2)

    first_adapter, _first_runtime, _first_created = helpers._build_adapter(  # noqa: SLF001
        live,
        fail_live_readback=True,
    )
    first_before = first_adapter.inspect_live_artifact()
    rolled_back = _call(
        first_adapter,
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_one,
        before=first_before,
    )
    assert rolled_back.outcome == "rolled_back"
    first_journal = _attempt_quarantine(live, attempt_one) / "recovery_journal.json"
    first_bytes = first_journal.read_bytes()

    second_adapter, _second_runtime, _second_created = helpers._build_adapter(  # noqa: SLF001
        live,
        fail_live_readback=False,
    )
    second_before = second_adapter.inspect_live_artifact()
    completed = _call(
        second_adapter,
        run_id=run_id,
        epoch=2,
        attempt_id=attempt_two,
        before=second_before,
    )
    assert completed.outcome == "completed"
    active = read_active_generation(live)
    assert active is not None
    second_generation = active.generation_id
    second_journal = _attempt_quarantine(live, attempt_two) / "recovery_journal.json"
    assert second_journal.exists()
    assert first_journal.read_bytes() == first_bytes

    manifest = json.loads(
        (active.graph_path.parent / "generation_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["run_id"] == run_id
    assert manifest["attempt_id"] == attempt_two
    assert manifest["epoch"] == 2

    stale_replay = _call(
        first_adapter,
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_one,
        before=first_before,
    )
    assert stale_replay.outcome == "rolled_back"
    assert first_journal.read_bytes() == first_bytes
    assert read_active_generation(live).generation_id == second_generation  # type: ignore[union-attr]


def test_unknown_attempt_artifact_is_quarantined_without_pointer_mutation(
    tmp_path: Path,
) -> None:
    helpers = _adapter_helpers()
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"original")
    adapter, _runtime, _created = helpers._build_adapter(live)  # noqa: SLF001
    run_id = "gdr_r5_orphan"
    known = _attempt_id(run_id, 1)
    orphan = _attempt_quarantine(live, f"{run_id}/attempt-99")
    orphan.mkdir(parents=True)
    (orphan / "untrusted.partial").write_bytes(b"must-not-be-adopted")
    pointer = active_pointer_path(live)
    pointer_before = pointer.read_bytes() if pointer.exists() else None

    reconcile = _required(adapter, "reconcile_attempt_artifacts")
    summary = reconcile(
        run_id=run_id,
        known_attempt_ids=(known,),
        now=NOW,
        fence_check=lambda: None,
    )

    assert tuple(summary.quarantined_ids) == (f"{run_id}/attempt-99",)
    assert known not in summary.quarantined_ids
    assert (pointer.read_bytes() if pointer.exists() else None) == pointer_before
    assert read_active_generation(live) is None


def test_attempt_retention_tie_keeps_latest_three_superseded_plus_active(
    tmp_path: Path,
) -> None:
    helpers = _adapter_helpers()
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"original")
    adapter, _runtime, _created = helpers._build_adapter(live)  # noqa: SLF001
    run_id = "gdr_r5_retention"
    attempt_ids = tuple(
        _write_terminal_attempt(
            live=live,
            run_id=run_id,
            epoch=epoch,
            age_hours=age_hours,
        )
        for epoch, age_hours in enumerate((48, 47, 2, 1.5, 1, 0), start=1)
    )

    summary = _required(adapter, "reconcile_attempt_artifacts")(
        run_id=run_id,
        known_attempt_ids=attempt_ids,
        now=NOW,
        fence_check=lambda: None,
    )

    # The three young superseded attempts are also the latest three.
    expected_retained = attempt_ids[2:]
    assert tuple(summary.retained_ids) == expected_retained
    assert tuple(summary.deleted_ids) == attempt_ids[:2]
    for attempt_id in expected_retained:
        assert _attempt_quarantine(live, attempt_id).is_dir()
    for attempt_id in attempt_ids[:2]:
        assert not _attempt_quarantine(live, attempt_id).exists()
    assert read_active_generation(live) is None


def test_attempt_retention_uses_latest_three_when_younger_set_is_larger(
    tmp_path: Path,
) -> None:
    helpers = _adapter_helpers()
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"original")
    adapter, _runtime, _created = helpers._build_adapter(live)  # noqa: SLF001
    run_id = "gdr_r5_retention_latest_three"
    attempt_ids = tuple(
        _write_terminal_attempt(
            live=live,
            run_id=run_id,
            epoch=epoch,
            age_hours=age_hours,
        )
        for epoch, age_hours in enumerate((48, 23, 22, 21, 20, 19, 18, 0), start=1)
    )

    summary = _required(adapter, "reconcile_attempt_artifacts")(
        run_id=run_id,
        known_attempt_ids=attempt_ids,
        now=NOW,
        fence_check=lambda: None,
    )

    # Six superseded attempts are young, but only the latest three are retained.
    assert tuple(summary.retained_ids) == attempt_ids[4:]
    assert tuple(summary.deleted_ids) == attempt_ids[:4]
    for attempt_id in attempt_ids[4:]:
        assert _attempt_quarantine(live, attempt_id).is_dir()
    for attempt_id in attempt_ids[:4]:
        assert not _attempt_quarantine(live, attempt_id).exists()


def test_attempt_retention_uses_younger_set_when_it_is_smaller(
    tmp_path: Path,
) -> None:
    helpers = _adapter_helpers()
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"original")
    adapter, _runtime, _created = helpers._build_adapter(live)  # noqa: SLF001
    run_id = "gdr_r5_retention_younger"
    attempt_ids = tuple(
        _write_terminal_attempt(
            live=live,
            run_id=run_id,
            epoch=epoch,
            age_hours=age_hours,
        )
        for epoch, age_hours in enumerate((72, 60, 48, 36, 30, 2, 0), start=1)
    )

    summary = _required(adapter, "reconcile_attempt_artifacts")(
        run_id=run_id,
        known_attempt_ids=attempt_ids,
        now=NOW,
        fence_check=lambda: None,
    )

    # Only attempt 6 is a young superseded attempt; attempt 7 is current.
    assert tuple(summary.retained_ids) == attempt_ids[5:]
    assert tuple(summary.deleted_ids) == attempt_ids[:5]
    for attempt_id in attempt_ids[5:]:
        assert _attempt_quarantine(live, attempt_id).is_dir()
    for attempt_id in attempt_ids[:5]:
        assert not _attempt_quarantine(live, attempt_id).exists()


def test_attempt_retention_fence_loss_prevents_physical_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helpers = _adapter_helpers()
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"original")
    adapter, _runtime, _created = helpers._build_adapter(live)  # noqa: SLF001
    run_id = "gdr_r5_retention_fence"
    attempt_ids = (
        _write_terminal_attempt(
            live=live,
            run_id=run_id,
            epoch=1,
            age_hours=72,
        ),
        _write_terminal_attempt(
            live=live,
            run_id=run_id,
            epoch=2,
            age_hours=0,
        ),
    )
    fence_calls = 0
    delete_fence_calls: list[int] = []

    def observe_fence() -> None:
        nonlocal fence_calls
        fence_calls += 1

    def observe_delete(
        _path: Path,
        *,
        fence_check,
    ) -> None:
        _required(recovery_module, "_assert_fenced")(fence_check)
        delete_fence_calls.append(fence_calls)

    monkeypatch.setattr(recovery_module, "_remove_tree_fenced", observe_delete)
    _required(adapter, "reconcile_attempt_artifacts")(
        run_id=run_id,
        known_attempt_ids=attempt_ids,
        now=NOW,
        fence_check=observe_fence,
    )
    assert len(delete_fence_calls) == 1

    fence_calls = 0
    observed_deletes_before_loss = len(delete_fence_calls)

    def lose_exact_fence() -> None:
        nonlocal fence_calls
        fence_calls += 1
        if fence_calls == delete_fence_calls[0]:
            raise RuntimeError("stale exact claim")

    fence_error = _required(
        recovery_module,
        "CommunityGlobalDiscoveryRecoveryFenceError",
    )
    with pytest.raises(fence_error):
        _required(adapter, "reconcile_attempt_artifacts")(
            run_id=run_id,
            known_attempt_ids=attempt_ids,
            now=NOW,
            fence_check=lose_exact_fence,
        )

    assert len(delete_fence_calls) == observed_deletes_before_loss
    assert _attempt_quarantine(live, attempt_ids[0]).is_dir()


def test_unknown_and_malformed_attempts_remain_quarantined_and_untouched(
    tmp_path: Path,
) -> None:
    helpers = _adapter_helpers()
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"original")
    adapter, _runtime, _created = helpers._build_adapter(live)  # noqa: SLF001
    run_id = "gdr_r5_retention_untrusted"
    malformed_id = _attempt_id(run_id, 1)
    nonterminal_id = _attempt_id(run_id, 2)
    nonterminal = _attempt_quarantine(live, nonterminal_id)
    nonterminal.mkdir(parents=True)
    assert recovery_module._write_journal(  # noqa: SLF001
        nonterminal / "recovery_journal.json",
        {
            "run_id": run_id,
            "attempt_id": nonterminal_id,
            "epoch": 2,
            "phase": "building",
            "outcome": "failed",
        },
    )
    active_id = _write_terminal_attempt(
        live=live,
        run_id=run_id,
        epoch=3,
        age_hours=0,
    )
    malformed = _attempt_quarantine(live, malformed_id)
    malformed.mkdir(parents=True)
    malformed_journal = malformed / "recovery_journal.json"
    malformed_journal.write_bytes(b"{not-json")
    malformed_evidence = malformed / "evidence.bin"
    malformed_evidence.write_bytes(b"malformed-must-remain")
    unknown_id = f"{run_id}/attempt-99"
    unknown = _attempt_quarantine(live, unknown_id)
    unknown.mkdir(parents=True)
    unknown_evidence = unknown / "evidence.bin"
    unknown_evidence.write_bytes(b"unknown-must-remain")

    summary = _required(adapter, "reconcile_attempt_artifacts")(
        run_id=run_id,
        known_attempt_ids=(malformed_id, nonterminal_id, active_id),
        now=NOW,
        fence_check=lambda: None,
    )

    assert tuple(summary.quarantined_ids) == (malformed_id, unknown_id)
    assert tuple(summary.retained_ids) == (nonterminal_id, active_id)
    assert tuple(summary.deleted_ids) == ()
    assert nonterminal.is_dir()
    assert malformed_journal.read_bytes() == b"{not-json"
    assert malformed_evidence.read_bytes() == b"malformed-must-remain"
    assert unknown_evidence.read_bytes() == b"unknown-must-remain"
    assert read_active_generation(live) is None
