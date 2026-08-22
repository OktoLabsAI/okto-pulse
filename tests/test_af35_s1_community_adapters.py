"""AF35-S1 Community adapter parity for Resource Gate, traceability and settings."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

# Registers every ORM model on Base.metadata so init_db builds the full schema.
import okto_pulse.community.app as _core_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import (
    CommunitySqlAlchemyResourceGateAdapter,
)
from okto_pulse.core.ports.relational_services import (
    register_resource_gate_adapter_factory,
)
from okto_pulse.core.services.resource_gate import ResourceGateService
from okto_pulse.community.adapters.sqlalchemy_runtime_settings_service import (
    AppSetting as CommunityAppSetting,
    get_runtime_settings as community_get_runtime_settings,
    put_runtime_settings as community_put_runtime_settings,
)
from okto_pulse.community.adapters.sqlalchemy_traceability_read_model import (
    build_traceability_report as community_build_traceability_report,
)
from okto_pulse.community.adapters.core_import_boundary import (
    audit_community_core_import_boundary,
)
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    register_community_relational_schema_lifecycle,
)
from okto_pulse.community.adapters.sqlalchemy_models import Board, Spec

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def _temp_session_factory(tmp_path):
    import okto_pulse.core.infra.config as _config
    from okto_pulse.community.config import CommunitySettings

    saved_data = os.environ.get("DATA_DIR")
    saved_kg = os.environ.get("KG_BASE_DIR")

    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["KG_BASE_DIR"] = str(tmp_path / "boards")
    _config.configure_settings(CommunitySettings())

    async def setup() -> None:
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'af35_s1.db'}")
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()

    asyncio.run(setup())
    try:
        yield _db_mod.get_session_factory()
    finally:
        try:
            asyncio.run(_db_mod.close_db())
        except Exception:
            pass
        for key, val in (("DATA_DIR", saved_data), ("KG_BASE_DIR", saved_kg)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def test_af35_s1_community_adapters_round_trip_real_sqlalchemy(
    _temp_session_factory,
):
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    sf = _temp_session_factory
    board_id = "af35-s1-board"
    spec_id = "af35-s1-spec"

    async def drive():
        async with sf() as db:
            db.add(Board(id=board_id, name="AF35", owner_id="agent", settings={}))
            db.add(
                Spec(
                    id=spec_id,
                    board_id=board_id,
                    title="AF35 Spec",
                    description="Traceability seed",
                    context="Adapter parity seed",
                    created_by="agent",
                )
            )
            await db.commit()

        async with sf() as db:
            resource_summary = await ResourceGateService(db).get_summary(
                board_id,
                "spec",
                spec_id,
            )
            traceability = await community_build_traceability_report(
                db,
                board_id,
                spec_id=spec_id,
                include_artifacts=False,
            )
            before = await community_get_runtime_settings(db)
            after = await community_put_runtime_settings(
                db,
                {"kg_queue_alert_threshold": 1234},
            )
            row = await db.get(CommunityAppSetting, "kg_queue_alert_threshold")
            return resource_summary, traceability, before, after, row

    resource_summary, traceability, before, after, row = asyncio.run(drive())

    assert resource_summary["entity_id"] == spec_id
    assert {item["resource_type"] for item in resource_summary["resources"]} == {
        "architecture",
        "mockup",
        "knowledge_base",
    }
    assert traceability["summary"]["specs"] == 1
    assert traceability["orphan_specs"][0]["id"] == spec_id
    assert traceability["code_traceability"] == {
        "evidence_total": 0,
        "evidence_linked": 0,
        "targets_total": 0,
        "targets_resolved": 0,
        "targets_outdated": 0,
        "high_overlaps": 0,
    }
    assert before["kg_queue_alert_threshold"] != 1234
    assert row is not None and row.value == "1234"
    assert after["restart_required"] is False


def test_af35_s1_community_adapter_imports_stay_boundary_clean() -> None:
    report = audit_community_core_import_boundary(REPO_ROOT)
    assert report["ok"] is True, report
    assert report["violations"] == []
