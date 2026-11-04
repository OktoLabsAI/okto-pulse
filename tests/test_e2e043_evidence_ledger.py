"""Native filesystem concurrency checks for immutable evidence publication."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from okto_pulse.community.adapters import test_evidence as module
from test_evidence_v2_adapter import _produce


def _issue(ledger):
    return ledger.issue(
        board_id="board",
        spec_id="spec",
        scenario_id="scenario",
        scenario_sha256="sha256:" + "a" * 64,
        status="passed",
        actor_id="agent",
        evidence={
            "manifest_ref": "example.json",
            "execution_attestation": {
                "manifest_sha256": "sha256:" + "b" * 64,
                "attestation_sha256": "sha256:" + "c" * 64,
                "run_id": "run",
            },
        },
    )


def _ledger(tmp_path):
    return module.CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")


@pytest.mark.parametrize("pause_secret", [False, True])
def test_concurrent_reader_never_observes_partial_published_bytes(
    tmp_path,
    monkeypatch,
    pause_secret,
):
    writer = _ledger(tmp_path)
    reader = _ledger(tmp_path)
    if not pause_secret:
        _issue(writer)
    paused, release = threading.Event(), threading.Event()
    original_write = module._write_all

    def write(fd, payload):
        selected = len(payload) == 32 if pause_secret else payload.startswith(b"{")
        if selected and threading.current_thread().name.startswith("publisher"):
            midpoint = len(payload) // 2
            original_write(fd, payload[:midpoint])
            paused.set()
            assert release.wait(10), "reader did not release publisher"
            original_write(fd, payload[midpoint:])
        else:
            original_write(fd, payload)

    monkeypatch.setattr(module, "_write_all", write)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="publisher") as pool:
        pending = pool.submit(_issue, writer)
        try:
            assert paused.wait(10), "publisher did not reach the write barrier"
            if pause_secret:
                assert not writer.secret_path.exists()
                other_receipt = _issue(reader)
            else:
                assert len(list(writer.receipt_root.iterdir())) == 1
                assert (
                    len(
                        reader._validate_existing_receipts(reader._secret(create=False))
                    )
                    == 1
                )
                other_receipt = _issue(reader)
        finally:
            release.set()
        receipt = pending.result(timeout=10)
    assert receipt != other_receipt
    assert len(reader._validate_existing_receipts(reader._secret(create=False))) == (
        2 if pause_secret else 3
    )
    assert not list(writer.staging_root.iterdir())
    for path in writer.receipt_root.iterdir():
        assert path.stat().st_nlink == 1
        assert json.loads(path.read_bytes())["receipt_id"] == path.stem


def test_atomic_publication_never_replaces_existing_receipt(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    original_receipt = _issue(ledger)
    original_id = original_receipt.split(".")[1]
    target = ledger.receipt_root / f"{original_id}.json"
    before = target.read_bytes()
    identity = module._file_identity(target.stat())
    next_id = "1" * 32 if original_id != "1" * 32 else "2" * 32
    ids = iter([original_id, next_id])
    monkeypatch.setattr(module.secrets, "token_hex", lambda count: next(ids))
    assert _issue(ledger).split(".")[1] == next_id
    assert target.read_bytes() == before
    assert module._file_identity(target.stat()) == identity
    assert not list(ledger.staging_root.iterdir())


def test_concurrent_first_issuer_may_publish_key_and_receipt_after_missing_read(
    tmp_path, monkeypatch
):
    writer, reader = _ledger(tmp_path), _ledger(tmp_path)
    original_read = module._read_regular_file_secure
    raced = False

    def read(path, **kwargs):
        nonlocal raced
        try:
            return original_read(path, **kwargs)
        except module.CommunityTestEvidenceError:
            if path == reader.secret_path and not raced:
                raced = True
                _issue(writer)
            raise

    monkeypatch.setattr(module, "_read_regular_file_secure", read)
    _issue(reader)
    assert len(list(reader.receipt_root.iterdir())) == 2
    assert reader._secret(create=False) == writer._secret(create=False)


def test_append_during_validation_is_reobserved_without_accepting_unstable_scan(
    tmp_path, monkeypatch
):
    writer, reader = _ledger(tmp_path), _ledger(tmp_path)
    _issue(writer)
    original_read = module._read_regular_file_secure
    appended = False

    def read(path, **kwargs):
        nonlocal appended
        value = original_read(path, **kwargs)
        if path.parent == reader.receipt_root and not appended:
            appended = True
            _issue(writer)
        return value

    monkeypatch.setattr(module, "_read_regular_file_secure", read)
    fingerprint = reader._validate_existing_receipts(reader._secret(create=False))
    assert appended
    assert len(fingerprint) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_observation_retry_is_bounded_and_never_reexecutes_test(
    tmp_path, monkeypatch, persistent
):
    ledger, _, _ = await _produce(tmp_path)
    original = ledger._receipt_history_fingerprint
    calls = 0

    def fingerprint(**kwargs):
        nonlocal calls
        calls += 1
        if persistent or calls <= 2:
            raise module.CommunityTestEvidenceError(
                "evidence_v2.receipt_ledger_changed_during_scan"
            )
        return original(**kwargs)

    monkeypatch.setattr(ledger, "_receipt_history_fingerprint", fingerprint)
    executions = []

    async def execute(manifest, manifest_ref):
        executions.append(manifest_ref)
        return module.ProductExecutionObservation(
            run_id="single-execution",
            outcome="passed",
            executed_at="2026-07-14T15:00:00Z",
            assertions=(
                {
                    "name": "about-version",
                    "expected": "0.3.0",
                    "observed": "0.3.0",
                    "status": "passed",
                },
            ),
        )

    if persistent:
        with pytest.raises(
            module.CommunityTestEvidenceError, match="receipt_ledger_busy"
        ) as error:
            await _produce(tmp_path, ledger=ledger, executor=execute)
        assert error.value.retryable is True
        assert calls == 3
        assert len(list(ledger.receipt_root.iterdir())) == 1
    else:
        await _produce(tmp_path, ledger=ledger, executor=execute)
        assert calls == 4  # third attempt authenticates then confirms a stable scan
        assert len(list(ledger.receipt_root.iterdir())) == 2
    assert len(executions) == 1


def test_receipt_hardlink_is_rejected_even_after_cache_hit(tmp_path):
    ledger = _ledger(tmp_path)
    _issue(ledger)
    target = next(ledger.receipt_root.iterdir())
    shadow = tmp_path / "shadow.json"
    try:
        shadow.hardlink_to(target)
    except OSError as exc:
        pytest.skip(f"host cannot create hard links: {exc}")
    with pytest.raises(module.CommunityTestEvidenceError, match="hardlink_forbidden"):
        _issue(ledger)


@pytest.mark.parametrize("failure", ["write", "fsync", "publish"])
def test_failed_staging_cleans_only_owned_file_and_publishes_nothing(
    tmp_path, monkeypatch, failure
):
    ledger = _ledger(tmp_path)
    _issue(ledger)
    before = {p.name: p.read_bytes() for p in ledger.receipt_root.iterdir()}
    ledger.staging_root.mkdir(exist_ok=True)
    unrelated = ledger.staging_root / "unrelated.tmp"
    unrelated.write_bytes(b"keep")

    def fail(*args):
        raise OSError("injected persistence failure")

    monkeypatch.setattr(
        module,
        {
            "write": "_write_all",
            "fsync": "_fsync_evidence_file",
            "publish": "_rename_no_replace",
        }[failure],
        fail,
    )
    with pytest.raises((OSError, module.CommunityTestEvidenceError)):
        _issue(ledger)
    assert {p.name: p.read_bytes() for p in ledger.receipt_root.iterdir()} == before
    assert list(ledger.staging_root.iterdir()) == [unrelated]
    assert unrelated.read_bytes() == b"keep"
