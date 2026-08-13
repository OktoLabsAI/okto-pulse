"""Focused Community export adapter, renderer and transport contracts."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlalchemy_entity_export import (
    CommunityEntityExportLimitError,
    CommunitySqlAlchemyEntityExportReader,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    Card,
    CardDependency,
    Ideation,
    Refinement,
    Spec,
    Sprint,
    Story,
    Topic,
)
from okto_pulse.community.api import entity_exports as api
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work_factory
from okto_pulse.community.services.entity_export_renderer import (
    render_entity_export_html,
    render_entity_export_markdown,
)
from okto_pulse.core.application.use_cases import EntityNotFoundError
from okto_pulse.core.domain.entity_export import (
    EntityExportBundle,
    EntityExportDisclosure,
    EntityExportHistoryScope,
    EntityExportManifest,
    EntityExportRequest,
    EntityExportSection,
    EntityExportSectionManifestEntry,
    EntityExportSectionStatus,
    EntityExportSubjectSnapshot,
    EntityExportType,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.authentication import Principal


_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _bundle(
    *,
    entity_type: EntityExportType = EntityExportType.SPEC,
    title: str = 'Danger "name" </style><script>alert(1)</script>',
) -> EntityExportBundle:
    base = EntityExportSection(
        section_key="base",
        schema_version="entity-export-base/v1",
        payload={
            "record": {
                "title": title,
                "metrics": {"confidence": 97, "ambiguity": 4},
                "pinpoints": [
                    {
                        "metric_tag": "clarity",
                        "item_id": "ac_1",
                        "text": "Requirement <img src=x onerror=alert(1)>",
                    }
                ],
            }
        },
    )
    manifest = EntityExportManifest(
        entries=(
            EntityExportSectionManifestEntry(
                section_key="base",
                status=EntityExportSectionStatus.INCLUDED,
                complete_for_actor=True,
                source_complete=True,
                schema_version="entity-export-base/v1",
                total_count=1,
                included_count=1,
                pagination_complete=True,
            ),
        ),
        source_complete=True,
        complete_for_actor=True,
    )
    return EntityExportBundle(
        subject=EntityExportSubjectSnapshot(
            board_id="board-1",
            entity_type=entity_type,
            entity_id="entity-1",
            title=title,
            status="draft",
            version=3,
            edition=2,
            captured_at=_NOW,
        ),
        history_scope=EntityExportHistoryScope.COMPLETE,
        sections=(base,),
        manifest=manifest,
        generated_at=_NOW,
    )


@pytest.mark.asyncio
async def test_reader_supports_six_types_and_fences_realm_and_related_rows() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_community_session_factory(engine)
    async with sessions() as session:
        session.add_all(
            [
                Board(id="b1", name="One", owner_id="u", realm_id="realm-1"),
                Board(id="b2", name="Two", owner_id="u", realm_id="realm-2"),
                Topic(id="topic", board_id="b1", name="Topic", created_by="u"),
                Story(
                    id="story",
                    board_id="b1",
                    topic_id="topic",
                    title="Story",
                    description="Story body",
                    created_by="u",
                ),
                Ideation(id="ideation", board_id="b1", title="Idea", created_by="u"),
                Refinement(
                    id="refinement",
                    board_id="b1",
                    ideation_id="ideation",
                    title="Refinement",
                    created_by="u",
                ),
                Spec(
                    id="spec",
                    board_id="b1",
                    title="Spec",
                    created_by="u",
                    test_scenarios=[{"id": "secret-scenario", "title": "Secret"}],
                    validations=[{"id": "old-validation", "edition": 1}],
                ),
                Sprint(
                    id="sprint",
                    board_id="b1",
                    spec_id="spec",
                    title="Sprint",
                    created_by="u",
                ),
                Card(id="card", board_id="b1", title="Card", created_by="u"),
                Card(id="foreign-card", board_id="b2", title="Foreign", created_by="u"),
                CardDependency(
                    id="cross-board-edge",
                    card_id="card",
                    depends_on_id="foreign-card",
                ),
            ]
        )
        await session.commit()
        reader = CommunitySqlAlchemyEntityExportReader(session, clock=lambda: _NOW)
        cases = (
            (EntityExportType.STORY, "story"),
            (EntityExportType.IDEATION, "ideation"),
            (EntityExportType.REFINEMENT, "refinement"),
            (EntityExportType.SPEC, "spec"),
            (EntityExportType.SPRINT, "sprint"),
            (EntityExportType.CARD, "card"),
        )
        for entity_type, entity_id in cases:
            bundle = await reader.build_bundle(
                request=EntityExportRequest(
                    board_id="b1",
                    entity_type=entity_type,
                    entity_id=entity_id,
                    requested_sections=("base",),
                ),
                disclosure=EntityExportDisclosure(frozenset(), ("base",)),
                actor_id="u",
                realm_scope=RealmScope.tenant("realm-1"),
            )
            assert bundle.subject.entity_type is entity_type
            assert bundle.subject.status in {"draft", "not_started"}

        with pytest.raises(EntityNotFoundError):
            await reader.build_bundle(
                request=EntityExportRequest(
                    board_id="b1",
                    entity_type=EntityExportType.SPEC,
                    entity_id="spec",
                    requested_sections=("base",),
                ),
                disclosure=EntityExportDisclosure(frozenset(), ("base",)),
                actor_id="u",
                realm_scope=RealmScope.tenant("realm-2"),
            )

        relationships = await reader.build_bundle(
            request=EntityExportRequest(
                board_id="b1",
                entity_type=EntityExportType.CARD,
                entity_id="card",
                requested_sections=("relationships",),
            ),
            disclosure=EntityExportDisclosure(
                frozenset({"card.entity.context_read"}),
                ("relationships",),
            ),
            actor_id="u",
            realm_scope=RealmScope.tenant("realm-1"),
        )
        payload = next(
            item.payload
            for item in relationships.sections
            if item.section_key == "relationships"
        )
        assert payload["records"]["card_dependencies"] == ()
    await engine.dispose()


@pytest.mark.asyncio
async def test_denied_section_is_not_selected_or_counted_and_current_does_not_leak() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    statements: list[str] = []
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _many: statements.append(
            statement
        ),
    )
    sessions = build_community_session_factory(engine)
    async with sessions() as session:
        session.add_all(
            [
                Board(id="b", name="Board", owner_id="u", realm_id="realm"),
                Spec(
                    id="spec",
                    board_id="b",
                    title="Spec",
                    created_by="u",
                    test_scenarios=[{"id": "must-not-leak"}],
                    validations=[{"id": "prior", "edition": 1, "score": 80}],
                    edition=2,
                ),
            ]
        )
        await session.commit()
        statements.clear()
        reader = CommunitySqlAlchemyEntityExportReader(session, clock=lambda: _NOW)
        denied = await reader.build_bundle(
            request=EntityExportRequest(
                board_id="b",
                entity_type=EntityExportType.SPEC,
                entity_id="spec",
                requested_sections=("test_scenarios",),
            ),
            disclosure=EntityExportDisclosure(frozenset(), ("test_scenarios",)),
            actor_id="u",
            realm_scope=RealmScope.tenant("realm"),
        )
        raw = denied.to_dict()
        denied_entry = next(
            item
            for item in raw["manifest"]["entries"]
            if item["section_key"] == "test_scenarios"
        )
        assert denied_entry["status"] == "omitted"
        assert denied_entry["reason_code"] == "permission_denied"
        assert "total_count" not in denied_entry
        assert "included_count" not in denied_entry
        base = next(item for item in raw["sections"] if item["section_key"] == "base")
        assert base["payload"]["record"]["test_scenarios"] == {
            "state": "separated",
            "section_key": "test_scenarios",
        }
        assert all("specs.test_scenarios" not in statement for statement in statements)

        current = await reader.build_bundle(
            request=EntityExportRequest(
                board_id="b",
                entity_type=EntityExportType.SPEC,
                entity_id="spec",
                history_scope=EntityExportHistoryScope.CURRENT,
                requested_sections=("spec_validation",),
            ),
            disclosure=EntityExportDisclosure(
                frozenset({"spec.validation.read"}),
                ("spec_validation",),
            ),
            actor_id="u",
            realm_scope=RealmScope.tenant("realm"),
        )
        section = next(
            item for item in current.sections if item.section_key == "spec_validation"
        )
        assert section.payload["embedded"]["validations"] == ()

        complete = await reader.build_bundle(
            request=EntityExportRequest(
                board_id="b",
                entity_type=EntityExportType.SPEC,
                entity_id="spec",
                history_scope=EntityExportHistoryScope.COMPLETE,
                requested_sections=("spec_validation",),
            ),
            disclosure=EntityExportDisclosure(
                frozenset({"spec.validation.read"}),
                ("spec_validation",),
            ),
            actor_id="u",
            realm_scope=RealmScope.tenant("realm"),
        )
        complete_section = next(
            item for item in complete.sections if item.section_key == "spec_validation"
        )
        assert complete_section.payload["embedded"]["validations"][0]["id"] == "prior"
    await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_section_limit_fails_without_truncation(monkeypatch) -> None:
    monkeypatch.setenv("OKTO_PULSE_EXPORT_MAX_SECTION_ROWS", "1")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_community_session_factory(engine)
    async with sessions() as session:
        session.add_all(
            [
                Board(id="b", name="Board", owner_id="u", realm_id="realm"),
                Spec(
                    id="spec",
                    board_id="b",
                    title="Spec",
                    created_by="u",
                    test_scenarios=[{"id": "one"}, {"id": "two"}],
                ),
            ]
        )
        await session.commit()
        with pytest.raises(CommunityEntityExportLimitError) as captured:
            await CommunitySqlAlchemyEntityExportReader(session).build_bundle(
                request=EntityExportRequest(
                    board_id="b",
                    entity_type=EntityExportType.SPEC,
                    entity_id="spec",
                    requested_sections=("test_scenarios",),
                ),
                disclosure=EntityExportDisclosure(
                    frozenset({"spec.tests.read"}),
                    ("test_scenarios",),
                ),
                actor_id="u",
                realm_scope=RealmScope.tenant("realm"),
            )
        assert captured.value.section_key == "test_scenarios"
        assert captured.value.limit == 1
    await engine.dispose()


def test_renderers_are_semantically_parallel_structured_and_passive() -> None:
    raw = _bundle().to_dict()
    markdown = render_entity_export_markdown(raw)
    html = render_entity_export_html(raw)
    for fact in (
        "confidence",
        "97",
        "ambiguity",
        "clarity",
        "Complete for actor",
        "Source complete",
    ):
        assert fact.casefold() in markdown.casefold()
        assert fact.casefold() in html.casefold()
    assert "ac\\_1" in markdown
    assert "ac_1" in html
    assert '<div class="score-grid">' in html
    assert '<article class="pinpoint-card">' in html
    assert '<summary>Report completeness and technical appendix</summary>' in html
    assert "0" * 64 not in html
    assert "0" * 64 not in markdown
    assert "<script>" not in html
    assert "onerror=" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x onerror&#61;alert(1)&gt;" in html
    assert "<iframe" not in html
    assert "<object" not in html
    assert "<embed" not in html


def test_human_first_report_leads_with_content_and_reduces_technical_identity() -> None:
    raw = _bundle(title="Availability report").to_dict()
    raw["sections"][0]["payload"] = {
        "record": {
            "id": "39ebfd41-f2a4-5fe7-9133-4ee9c0ee2cbc",
            "board_id": "15877207-c147-4805-96d7-d53a625571df",
            "title": "Availability report",
            "description": "Defines the availability behavior people must implement.",
            "functional_requirements": [
                {
                    "id": "fr_ced7e9e83aa644e1a7de44c7f2a11111",
                    "text": "FR-3 Complete entity coverage: export every authorized fact.",
                    "linked_task_ids": ["5a15282a-b704-5c89-8e15-4338ca3d0b1b"],
                }
            ],
        }
    }
    html = render_entity_export_html(raw)
    markdown = render_entity_export_markdown(raw)

    assert "FR-3 Complete entity coverage: export every authorized fact." in html
    assert "(fr_ced7e9e8)" in html
    assert "39ebfd41-f2a4-5fe7-9133-4ee9c0ee2cbc" not in html
    assert "15877207-c147-4805-96d7-d53a625571df" not in html
    assert "5a15282a-b704-5c89-8e15-4338ca3d0b1b" not in html
    assert "Item 1" not in html
    assert html.index("Defines the availability behavior") < html.index(">Technical metadata<")
    assert "FR-3 Complete entity coverage: export every authorized fact." in markdown
    assert "39ebfd41-f2a4-5fe7-9133-4ee9c0ee2cbc" not in markdown


def test_current_result_is_visible_and_previous_results_are_collapsed() -> None:
    raw = _bundle(title="Validation history").to_dict()
    raw["sections"][0]["payload"] = {
        "embedded": {
            "validations": [
                {"id": "val_current12345678", "is_current": True, "outcome": "success", "confidence": 97},
                {"id": "val_previous12345678", "is_current": False, "outcome": "failed", "confidence": 61},
            ]
        }
    }
    html = render_entity_export_html(raw)

    assert '<details class="previous-results">' in html
    assert "Previous results <span>1</span>" in html
    assert html.index("val_current1") < html.index("Previous results")
    assert "val_current12345678" not in html
    assert "val_previous12345678" not in html


def test_policy_pinpoint_uses_human_target_from_same_sealed_bundle() -> None:
    raw = _bundle(title="Policy report").to_dict()
    raw["sections"][0]["payload"] = {
        "record": {
            "title": "Policy report",
            "acceptance_criteria": [
                {
                    "id": "ac_2ce4ec3f12345678",
                    "text": "Given the current edition, the result remains readable.",
                }
            ],
        }
    }
    raw["manifest"]["entries"].append(
        {
            "section_key": "policy_compliance",
            "status": "included",
            "complete_for_actor": True,
            "source_complete": True,
            "schema_version": "entity-export-section/v1",
            "total_count": 1,
            "included_count": 1,
            "pagination_complete": True,
        }
    )
    raw["sections"].append(
        {
            "section_key": "policy_compliance",
            "schema_version": "entity-export-section/v1",
            "payload": {
                "records": {
                    "semantic_guideline_metric_results": [
                        {
                            "metric_code": "spec.verifiability",
                            "outcome": "pass",
                            "rationale": "The criterion is observable.",
                            "pinpoints": [
                                {
                                    "metric": "verifiability",
                                    "anchor_ref": "ac_2ce4ec3f12345678",
                                    "detail": "The condition has an explicit outcome.",
                                }
                            ],
                        }
                    ]
                }
            },
        }
    )

    html = render_entity_export_html(raw)
    assert "AC-1: Given the current edition, the result remains readable." in html
    assert "(ac_2ce4ec3f)" in html
    assert "The condition has an explicit outcome." in html


@pytest.mark.parametrize(
    "entity_type",
    ["story", "ideation", "refinement", "spec", "sprint", "card"],
)
def test_preflight_wire_contract_supports_all_six_types(monkeypatch, entity_type: str) -> None:
    bundle = _bundle(entity_type=EntityExportType(entity_type), title="Report")

    async def materialize(**kwargs):
        assert kwargs["entity_type"] is EntityExportType(entity_type)
        return bundle

    monkeypatch.setattr(api, "_materialize_bundle", materialize)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject="user", realm_id="realm", actor_kind="human"
    )
    app.dependency_overrides[get_unit_of_work_factory] = lambda: object()
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/boards/board-1/entity-exports/{entity_type}/entity-1/preflight",
            json={"scope": "complete", "sections": ["base"]},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["identity"]["entity_type"] == entity_type
    assert body["scope"] == "complete"
    assert body["sections"][0]["state"] == "included"
    assert body["snapshot_fingerprint"] == bundle.snapshot_fingerprint


def test_download_stale_and_secure_headers_and_render_after_materialization(monkeypatch) -> None:
    bundle = _bundle(title='Report "safe"')
    released = False

    async def materialize(**_kwargs):
        nonlocal released
        released = True
        return bundle

    original_renderer = api.render_entity_export_html

    def render(detached):
        assert released is True
        return original_renderer(detached)

    monkeypatch.setattr(api, "_materialize_bundle", materialize)
    monkeypatch.setattr(api, "render_entity_export_html", render)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject="user", realm_id="realm", actor_kind="human"
    )
    app.dependency_overrides[get_unit_of_work_factory] = lambda: object()
    path = "/api/v1/boards/board-1/entity-exports/spec/entity-1/download"
    with TestClient(app) as client:
        stale = client.post(
            path,
            json={
                "scope": "complete",
                "sections": ["base"],
                "format": "html",
                "expected_snapshot_fingerprint": "0" * 64,
            },
        )
        response = client.post(
            path,
            json={
                "scope": "complete",
                "sections": ["base"],
                "format": "html",
                "expected_snapshot_fingerprint": bundle.snapshot_fingerprint,
            },
        )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "entity_export_snapshot_changed"
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-security-policy"] == api.ENTITY_EXPORT_HTML_CSP
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "\r" not in response.headers["content-disposition"]
    assert "\n" not in response.headers["content-disposition"]
    assert response.headers["x-export-snapshot-fingerprint"] == bundle.snapshot_fingerprint
    assert response.headers["etag"] == f'"{bundle.snapshot_fingerprint}"'
