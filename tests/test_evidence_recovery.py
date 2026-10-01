"""Offline byte continuity never changes the evidence admission decision."""

from copy import deepcopy
import os

import pytest

from okto_pulse.community.adapters.evidence_recovery import (
    evidence_capture_window, evidence_restore_window, verify_evidence_recovery,
)
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestEvidenceWriteVerifier,
    CommunityTestVerificationReportIssuer,
)
from okto_pulse.core.ports.test_evidence import TestVerificationReportRequest as Request
from verification_report_fixtures import report


async def issue(root):
    ledger = CommunityEvidenceLedger(evidence_root=root)
    issued = await CommunityTestVerificationReportIssuer(ledger=ledger).admit(Request(
        board_id='board-a', spec_id='spec-a', scenario_id='scenario-a',
        scenario_sha256='sha256:' + 'a' * 64, actor_id='reviewer', report=report()))
    return ledger, dict(issued.evidence)


def verify(root, evidence):
    return CommunityTestEvidenceWriteVerifier(ledger=CommunityEvidenceLedger(evidence_root=root)).verify(
        board_id='board-a', spec_id='spec-a', scenario_id='scenario-a',
        scenario_sha256='sha256:' + 'a' * 64, status='passed', actor_id=None, evidence=evidence)


@pytest.mark.asyncio
async def test_roundtrip_preserves_acceptance_and_denial_without_issuing(tmp_path):
    source, retained, restored = (tmp_path / name for name in ('source', 'retained', 'restored'))
    ledger, evidence = await issue(source)
    # Retain authored manifests as opaque bytes too, including nested paths.
    (ledger.manifest_root / 'nested').mkdir(parents=True)
    (ledger.manifest_root / 'nested/original.json').write_bytes(b'{"opaque":true}')
    denied = deepcopy(evidence)
    denied['report_sha256'] = 'sha256:' + 'b' * 64
    before = {path.relative_to(source): path.read_bytes() for path in source.rglob('*') if path.is_file()}
    with evidence_capture_window(source, retained) as capture:
        capture.validate()
        record = capture.record
    with evidence_restore_window(retained, record, restored, current_root=source) as guard:
        guard.validate()
    assert verify(source, evidence).verified and verify(restored, evidence).verified
    assert not verify(source, denied).verified and not verify(restored, denied).verified
    assert {path.relative_to(restored): path.read_bytes() for path in restored.rglob('*') if path.is_file()} == before
    assert {path.relative_to(source): path.read_bytes() for path in source.rglob('*') if path.is_file()} == before
    if os.name != 'nt':
        assert restored.joinpath('receipt.key').stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['receipt', 'key', 'manifest', 'extra'])
async def test_retained_tampering_is_rejected(tmp_path, change):
    ledger, _ = await issue(tmp_path / 'source')
    retained = tmp_path / 'retained'
    with evidence_capture_window(ledger.evidence_root, retained) as capture:
        record = capture.record
    if change == 'receipt':
        next((retained / 'receipts').iterdir()).write_bytes(b'{}')
    elif change == 'key':
        (retained / 'receipt.key').write_bytes(b'x' * 32)
    elif change == 'manifest':
        (retained / 'manifests').mkdir(exist_ok=True)
        (retained / 'manifests/extra.json').write_bytes(b'{}')
    else:
        (retained / 'unknown').write_bytes(b'x')
    with pytest.raises(ValueError):
        verify_evidence_recovery(retained, record)


@pytest.mark.asyncio
async def test_live_receipt_append_refuses_restore_and_capture_publication(tmp_path):
    source, retained, target = (tmp_path / name for name in ('source', 'retained', 'target'))
    await issue(source)
    with evidence_capture_window(source, retained) as capture:
        await issue(source)
        with pytest.raises(ValueError, match='source_changed'):
            capture.validate()
    with pytest.raises(ValueError, match='source_changed'):
        with evidence_restore_window(retained, capture.record, target, current_root=source):
            pytest.fail('changed evidence may not be restored')
    assert not target.exists()


@pytest.mark.asyncio
async def test_live_change_after_copy_refuses_publication(tmp_path):
    source, retained, target = (tmp_path / name for name in ('source', 'retained', 'target'))
    await issue(source)
    with evidence_capture_window(source, retained) as capture:
        record = capture.record
    with evidence_restore_window(retained, record, target, current_root=source) as guard:
        await issue(source)
        with pytest.raises(ValueError, match='source_changed'):
            guard.validate()


def test_absent_ledger_remains_absent_without_generating_key(tmp_path):
    source, retained, target = (tmp_path / name for name in ('source', 'retained', 'target'))
    with evidence_capture_window(source, retained) as capture:
        assert capture.record['present'] is False
        capture.validate()
    with evidence_restore_window(retained, capture.record, target, current_root=source) as guard:
        guard.validate()
    assert not source.exists() and not retained.exists() and not target.exists()


@pytest.mark.asyncio
async def test_missing_key_never_reissues_it(tmp_path):
    ledger, _ = await issue(tmp_path / 'source')
    ledger.secret_path.unlink()
    with pytest.raises(ValueError, match='secret_missing'):
        with evidence_capture_window(ledger.evidence_root, tmp_path / 'retained'):
            pytest.fail('missing key may not be fabricated')
    assert not ledger.secret_path.exists()


@pytest.mark.asyncio
async def test_hardlinked_key_is_refused(tmp_path):
    ledger, _ = await issue(tmp_path / 'source')
    os.link(ledger.secret_path, tmp_path / 'alias')
    with pytest.raises(ValueError, match='private_file_required'):
        with evidence_capture_window(ledger.evidence_root, tmp_path / 'retained'):
            pytest.fail('shared signing key may not be captured')
