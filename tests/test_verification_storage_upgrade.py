"""Real exact406 fixtures and authentic legacy receipts; never live storage."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters import verification_storage_upgrade as upgrade
from okto_pulse.community.adapters.current_relational_schema import (
    APPLICATION_ID, StorageFormatError, current_schema_contract, inspect_current_schema, require_current_database_file,
)
from okto_pulse.community.adapters.historical_relational_schema import historical_schema_contract, inspect_historical_schema
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory, install_community_sqlite_pragmas
from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.community.adapters.sqlalchemy_verification_currentness import (
    CommunityVerificationCurrentnessStore as Store, CommunityVerificationReservationPort as ReservationPort,
)
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestEvidenceWriteVerifier, CommunityTestVerificationReportIssuer,
)
from okto_pulse.core.domain.verification_currentness import VerificationScope, VerificationCurrentnessError
from okto_pulse.core.ports.test_evidence import TestVerificationReportRequest as ReportRequest
from okto_pulse.core.ports.verification_currentness import VerificationReservationRequest
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256
import test_verification_currentness_store as native
from verification_report_fixtures import report

native_owner = native.native_owner
SCOPE = VerificationScope(board_id="board", spec_id="spec", spec_edition=2, scenario_id="ts")


@pytest.fixture(scope="session")
def format406(tmp_path_factory):
    path = tmp_path_factory.mktemp("exact406") / "baseline.db"
    with closing(sqlite3.connect(path)) as db:
        for row in sorted(historical_schema_contract().objects, key=lambda row: (row[0] != "table", row[1])):
            db.execute(row[3])
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=406")
        db.execute("INSERT INTO global_discovery_source_revision(scope_id,fence_version,trigger_manifest_version,"
            "incarnation_id,revision,mutation_nonce) VALUES('_global','gdsr-fence-v2','gdsr-trigger-manifest-v10',?,5,?)",
            ("a" * 64, "b" * 64))
        db.commit()
        inspect_historical_schema(db.cursor())
    return path


@pytest.fixture
def source(format406, tmp_path):
    path = tmp_path / "source.db"
    shutil.copyfile(format406, path)
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "original-evidence")
    criteria = [{"id": "ac-1", "text": "Public ports only"}]
    scenarios, envelopes = [], {}
    historical_receipts = []
    for identity, status in [("ts", "passed"), ("tf", "failed"), ("ti", "passed"), ("tm", "passed"), ("tr", "ready")]:
        scenario = {"id": identity, "given": "Source available", "when": "Inspected", "then": "Public ports only",
                    "linked_criteria": ["ac-1"], "verification_method": "inspection", "status": status}
        digest = compute_test_scenario_semantic_sha256(board_id="board", spec_id="spec", scenario=scenario,
                                                       acceptance_criteria=criteria)
        def signed(outcome):
            value = report()
            value["result"] = value["observations"][0]["outcome"] = outcome
            envelope = {"evidence_class": "verification_report", "verification_report": value,
                        "report_author_id": "actor", "scenario_sha256": digest}
            envelope["execution_receipt"] = ledger.issue_verification_report(board_id="board", spec_id="spec",
                scenario_id=identity, scenario_sha256=digest, status=outcome, actor_id="actor", evidence=envelope)
            return envelope
        if identity == "tf":
            historical_receipts.append(signed("passed")["execution_receipt"])
        envelope = signed(status if status in {"passed", "failed"} else "passed")
        if identity == "ti":
            envelope["verification_report"]["conclusion"] = "Tampered after signing"
        if identity == "tm":
            envelope["execution_receipt"] = "ev2r." + "0" * 32 + "." + "0" * 64
        scenario["evidence"] = envelope
        envelopes[identity] = envelope
        scenarios.append(scenario)
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO boards(id,name,owner_id,realm_id) VALUES('board','Preserve','actor','local')")
        db.execute("INSERT INTO specs(id,board_id,title,status,edition,version,created_by,architecture_adoption,"
            "test_scenarios,acceptance_criteria,context) VALUES('spec','board','Original','in_progress',2,7,'actor','{}',?,?,?)",
            (json.dumps(scenarios), json.dumps(criteria), "Exact historical text: ação ✓"))
        db.commit()
    return SimpleNamespace(path=path, ledger=ledger, evidence=envelopes, historical=historical_receipts, criteria=criteria)


def ledger_snapshot(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()} if root.exists() else {}


def convert(source, tmp_path, **kwargs):
    output = tmp_path / "converted.db"
    receipt = upgrade.upgrade_verification_copy(source.path, output,
        evidence_root=kwargs.get("evidence_root", source.ledger.evidence_root))
    return output, receipt


def test_current_only_passed_and_failed_seeds_preserve_every_old_row_and_receipt(source, tmp_path):
    before, receipts = source.path.read_bytes(), ledger_snapshot(source.ledger.evidence_root)
    output, result = convert(source, tmp_path)
    assert source.path.read_bytes() == before and ledger_snapshot(source.ledger.evidence_root) == receipts
    assert result["source_database_unchanged"] and result["receipt_files_unchanged"] and not result["installation_switched"]
    assert result["seed_summary"] == {"scenarios": 5, "seeded_passed": 1, "seeded_failed": 1,
                                       "unqualified_current_fact": 2, "no_current_terminal_fact": 1}
    with closing(sqlite3.connect(output)) as db:
        inspect_current_schema(db.cursor(), current_schema_contract())
        rows = db.execute("SELECT scenario_id,receipt_id,evidence,result,origin,migration_source_sha256 FROM verification_acceptances").fetchall()
        assert {row[0] for row in rows} == {"ts", "tf"}
        assert not set(source.historical) & {row[1] for row in rows}
        for identity, _, raw, result_json, origin, snapshot in rows:
            assert json.loads(raw) == source.evidence[identity]
            assert json.loads(result_json)["actor_kind"] == "unknown"
            assert origin == "legacy_seed" and snapshot == result["source_snapshot_sha256"]
        with closing(sqlite3.connect(source.path)) as old:
            for table in result["preserved_tables"]:
                assert upgrade._table_fingerprint(db, table) == upgrade._table_fingerprint(old, table)


def test_original_and_new_storage_are_admitted_only_by_matching_format(source, tmp_path):
    output, _ = convert(source, tmp_path)
    with closing(sqlite3.connect(source.path)) as db:
        assert not inspect_historical_schema(db.cursor())
    with pytest.raises(StorageFormatError, match="version"):
        require_current_database_file("sqlite:///" + source.path.as_posix(), current_schema_contract())
    with closing(sqlite3.connect(output)) as db, pytest.raises(StorageFormatError, match="version"):
        inspect_historical_schema(db.cursor())


def test_migrated_acceptance_cannot_omit_its_snapshot_provenance(source, tmp_path):
    output, _ = convert(source, tmp_path)
    with closing(sqlite3.connect(output)) as db:
        db.row_factory = sqlite3.Row
        existing = dict(db.execute("SELECT * FROM verification_acceptances LIMIT 1").fetchone())
        existing.update(id="invalid-seed", migration_source_sha256=None)
        with pytest.raises(sqlite3.IntegrityError, match="ck_verification_acceptance_origin"):
            upgrade._insert(db, "verification_acceptances", existing)


def test_missing_ledger_keeps_proof_unqualified_without_creating_key_or_receipts(source, tmp_path):
    missing = tmp_path / "missing-ledger"
    output, result = convert(source, tmp_path, evidence_root=missing)
    assert not missing.exists() and result["seed_summary"]["unqualified_current_fact"] == 4
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("SELECT count(*) FROM verification_acceptances").fetchone()[0] == 0


@pytest.mark.parametrize("mutation,reason", [
    ("PRAGMA user_version=405", "version"),
    ("CREATE INDEX unexpected ON specs(title)", "schema_fingerprint"),
    ("UPDATE specs SET board_id='missing'", "foreign_key_integrity"),
])
def test_unknown_or_corrupt_source_is_refused_without_publication(source, tmp_path, mutation, reason):
    with closing(sqlite3.connect(source.path)) as db:
        db.execute(mutation)
        db.commit()
    before = source.path.read_bytes()
    with pytest.raises(upgrade.StorageUpgradeError, match=reason):
        convert(source, tmp_path)
    assert source.path.read_bytes() == before and not (tmp_path / "converted.db").exists()
    assert not list(tmp_path.glob(".pulse-verification-upgrade-*"))


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_interrupted_conversion_never_publishes_partial_target(source, tmp_path, monkeypatch, failure):
    before, receipts = source.path.read_bytes(), ledger_snapshot(source.ledger.evidence_root)
    def reject(*args):
        raise failure("isolated crash injection")
    monkeypatch.setattr(upgrade, "inspect_current_schema", reject)
    with pytest.raises(failure, match="isolated crash"):
        convert(source, tmp_path)
    assert source.path.read_bytes() == before and ledger_snapshot(source.ledger.evidence_root) == receipts
    assert not (tmp_path / "converted.db").exists() and not list(tmp_path.glob(".pulse-verification-upgrade-*"))


def test_existing_and_racing_output_are_never_replaced(source, tmp_path, monkeypatch):
    output = tmp_path / "existing.db"
    output.write_bytes(b"User file")
    with pytest.raises(upgrade.StorageUpgradeError, match="unused_output"):
        upgrade.upgrade_verification_copy(source.path, output, evidence_root=source.ledger.evidence_root)
    assert output.read_bytes() == b"User file"
    original = upgrade.os.link
    def race(pending, destination):
        Path(destination).write_bytes(b"Racing creator")
        return original(pending, destination)
    monkeypatch.setattr(upgrade.os, "link", race)
    with pytest.raises(FileExistsError):
        convert(source, tmp_path)
    assert (tmp_path / "converted.db").read_bytes() == b"Racing creator"


def test_pending_wal_is_preserved_without_modifying_source_sidecars(source, tmp_path):
    with closing(sqlite3.connect(source.path)) as active:
        active.execute("PRAGMA journal_mode=WAL")
        active.execute("PRAGMA wal_autocheckpoint=0")
        active.execute("UPDATE specs SET context='Committed WAL data'")
        active.commit()
        before = {p.name: p.read_bytes() for p in tmp_path.glob("source.db*")}
        output, _ = convert(source, tmp_path)
        assert {p.name: p.read_bytes() for p in tmp_path.glob("source.db*")} == before
        with closing(sqlite3.connect(output)) as db:
            assert db.execute("SELECT context FROM specs").fetchone()[0] == "Committed WAL data"


def test_cli_routes_exact406_with_explicit_ledger_and_prints_only_json(source, tmp_path, monkeypatch, capsys):
    import sys
    from okto_pulse.community.cli import main
    monkeypatch.setattr(sys, "argv", ["okto-pulse", "storage", "upgrade-copy", "--source-db", str(source.path),
        "--output-db", str(tmp_path / "converted.db"), "--evidence-root", str(source.ledger.evidence_root), "--json"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["target_schema_version"] == 407 and result["seed_summary"]["seeded_failed"] == 1


def test_source_change_after_seeding_is_detected_before_exclusive_publication(source, tmp_path, monkeypatch):
    original = upgrade._source_state
    calls = 0
    def changed(path):
        nonlocal calls
        calls += 1
        if calls == 3:
            with closing(sqlite3.connect(source.path)) as writer:
                writer.execute("UPDATE specs SET context='Concurrent edit after conversion'")
                writer.commit()
        return original(path)
    monkeypatch.setattr(upgrade, "_source_state", changed)
    with pytest.raises(upgrade.StorageUpgradeError, match="source_changed_during_copy"):
        convert(source, tmp_path)
    assert not (tmp_path / "converted.db").exists() and not list(tmp_path.glob(".pulse-verification-upgrade-*"))


@pytest_asyncio.fixture
async def authority(source, tmp_path):
    output, receipt = convert(source, tmp_path)
    engine = create_async_engine("sqlite+aiosqlite:///" + str(output))
    install_community_sqlite_pragmas(engine)
    factory = build_community_session_factory(engine)
    verifier = CommunityTestEvidenceWriteVerifier(ledger=source.ledger)
    yield SimpleNamespace(engine=engine, factory=factory, ledger=source.ledger, verifier=verifier,
        source=source, receipt=receipt)
    await engine.dispose()


async def read(authority):
    async with authority.factory() as db:
        return await Store(db, verifier=authority.verifier).read_current(board_id="board", spec_id="spec",
            spec_edition=2, scenario_ids=("ts", "tf", "ti", "tm", "tr"))


@pytest.mark.asyncio
async def test_native_owner_exact_migrated_replay_is_noop_and_retains_identity(authority, native_owner):
    before = await read(authority)
    original = next(row for row in before.results if row.result.context.scenario_id == "ts")
    replay = SimpleNamespace(status="passed", evidence=authority.source.evidence["ts"])
    response = await native_owner.publish(replay)
    assert response["replayed"] and response["accepted_result_id"] == original.accepted_result_id
    assert response["verification_current"] and not response["operational_summary_updated"]
    assert await read(authority) == before
    async with authority.factory() as db:
        assert (await db.get(Spec, "spec")).version == 7


@pytest.mark.asyncio
async def test_migrated_failed_fact_has_exact_replay_without_falling_back_to_old_pass(authority):
    scope = SCOPE.model_copy(update={"scenario_id": "tf"})
    async with authority.factory() as db:
        result = await Store(db, verifier=authority.verifier).replay_legacy(scope=scope,
            evidence=authority.source.evidence["tf"], evidence_status="failed", actor_id="actor", expected_spec_version=7)
        assert result.replayed and result.acceptance.result.outcome == "failed"
        assert result.acceptance.result.receipt_id not in authority.source.historical


@pytest.mark.asyncio
async def test_valid_legacy_signature_issued_after_conversion_cannot_mint_authority(authority, native_owner):
    envelope = deepcopy(authority.source.evidence["ts"])
    envelope.pop("execution_receipt")
    envelope["execution_receipt"] = authority.ledger.issue_verification_report(board_id="board", spec_id="spec",
        scenario_id="ts", scenario_sha256=envelope["scenario_sha256"], status="passed", actor_id="actor", evidence=envelope)
    with pytest.raises(VerificationCurrentnessError, match="verification_legacy_seed_not_current"):
        await native_owner.publish(SimpleNamespace(status="passed", evidence=envelope))


@pytest.mark.asyncio
async def test_migrated_read_reauthenticates_original_receipt_and_fails_closed(authority):
    assert (await read(authority)).complete
    identity = authority.source.evidence["ts"]["execution_receipt"].split(".")[1]
    path = authority.ledger.receipt_root / (identity + ".json")
    # Isolated corruption injection; no native receipt is rewritten or accepted.
    path.write_bytes(b"{}")
    result = await read(authority)
    assert not result.complete and "verification_receipt_invalid" in result.reason_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["actor", "body", "status", "reset"])
async def test_legacy_replay_rejects_changed_or_stale_identity(authority, change):
    evidence, actor, status = deepcopy(authority.source.evidence["ts"]), "actor", "passed"
    if change == "actor":
        actor = "other"
    elif change == "body":
        evidence["verification_report"]["conclusion"] = "Changed"
    elif change == "status":
        status = "failed"
    else:
        async with authority.factory.begin() as db:
            spec = await db.get(Spec, "spec")
            scenarios = deepcopy(spec.test_scenarios)
            scenarios[0]["status"] = "ready"
            await db.execute(update(Spec).values(test_scenarios=scenarios))
    async with authority.factory() as db:
        with pytest.raises(VerificationCurrentnessError):
            await Store(db, verifier=authority.verifier).replay_legacy(scope=SCOPE, evidence=evidence,
                evidence_status=status, actor_id=actor, expected_spec_version=7)


async def fresh(authority, revision):
    async with authority.factory() as db:
        spec = await db.get(Spec, "spec")
        scenario = spec.test_scenarios[0]
        digest = compute_test_scenario_semantic_sha256(board_id="board", spec_id="spec", scenario=scenario,
            acceptance_criteria=spec.acceptance_criteria)
        version = spec.version
    context = (await ReservationPort(authority.factory).reserve(VerificationReservationRequest(
        SCOPE, digest.removeprefix("sha256:"), "inspection", "actor", "human", version))).context
    value = report()
    value["sources"][0]["revision"] = revision
    value["result"] = value["observations"][0]["outcome"] = "failed"
    issued = await CommunityTestVerificationReportIssuer(ledger=authority.ledger).admit(ReportRequest(
        board_id="board", spec_id="spec", scenario_id="ts", scenario_sha256=digest,
        actor_id="actor", report=value, verification_context=context))
    return SimpleNamespace(status="failed", evidence=issued.evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_basis", [True, False])
async def test_new_publication_supersedes_only_matching_seed_basis(authority, native_owner, same_basis):
    await native_owner.publish(await fresh(authority, "commit-1" if same_basis else "commit-2"))
    legacy = SimpleNamespace(status="passed", evidence=authority.source.evidence["ts"])
    if same_basis:
        with pytest.raises(VerificationCurrentnessError, match="verification_legacy_seed_not_current"):
            await native_owner.publish(legacy)
    else:
        response = await native_owner.publish(legacy)
        assert response["replayed"] and response["new_status"] == "failed"
    facts = await read(authority)
    assert facts.complete and len([r for r in facts.results if r.result.context.scenario_id == "ts"]) == (1 if same_basis else 2)
