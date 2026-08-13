"""Focused Community export adapter, renderer and transport contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

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
    ArchitectureDesign,
    ArchitectureDiagramPayload,
    Base,
    Board,
    Card,
    CardDependency,
    Ideation,
    Refinement,
    Spec,
    SpecQAItem,
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
async def test_denied_section_is_not_selected_or_counted_and_current_does_not_leak() -> (
    None
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    statements: list[str] = []
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _many: (
            statements.append(statement)
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


@pytest.mark.asyncio
async def test_reader_projects_cards_and_qa_for_human_consumption() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_community_session_factory(engine)
    async with sessions() as session:
        session.add_all(
            [
                Board(id="b", name="Board", owner_id="u", realm_id="realm"),
                Spec(id="spec", board_id="b", title="Spec", created_by="u"),
                Card(
                    id="test-card",
                    board_id="b",
                    spec_id="spec",
                    title="Verify graceful failover",
                    description="Exercise the three-instance Multi-AZ failover.",
                    details="Capture failover time and service availability.",
                    status="in_progress",
                    card_type="test",
                    archived=False,
                    policy_version=19,
                    created_by="u",
                ),
                SpecQAItem(
                    id="qa-answered",
                    spec_id="spec",
                    question="Which recovery target applies?",
                    question_type="choice",
                    choices=[
                        {"id": "fast", "label": "Under 30 seconds"},
                        {"id": "slow", "label": "Under 5 minutes"},
                    ],
                    answer="Under 30 seconds",
                    selected=["fast"],
                    asked_by="u",
                ),
                SpecQAItem(
                    id="qa-open",
                    spec_id="spec",
                    question="Which regions are required?",
                    question_type="multi_choice",
                    choices=[
                        {"id": "east", "label": "US East"},
                        {"id": "west", "label": "US West"},
                    ],
                    asked_by="u",
                ),
            ]
        )
        await session.commit()
        bundle = await CommunitySqlAlchemyEntityExportReader(
            session, clock=lambda: _NOW
        ).build_bundle(
            request=EntityExportRequest(
                board_id="b",
                entity_type=EntityExportType.SPEC,
                entity_id="spec",
                requested_sections=("cards", "qa"),
            ),
            disclosure=EntityExportDisclosure(
                frozenset({"card.entity.read", "spec.qa.read"}),
                ("cards", "qa"),
            ),
            actor_id="u",
            realm_scope=RealmScope.tenant("realm"),
        )

        sections = {item.section_key: item.payload for item in bundle.sections}
        card = sections["cards"]["records"]["cards"][0]
        assert card == {
            "title": "Verify graceful failover",
            "description": "Exercise the three-instance Multi-AZ failover.",
            "details": "Capture failover time and service availability.",
            "status": "in_progress",
            "priority": "none",
            "card_type": "test",
        }
        assert "policy_version" not in card
        assert "archived" not in card

        qa_items = sections["qa"]["records"]["spec_qa_items"]
        assert qa_items == (
            {
                "question": "Which recovery target applies?",
                "answer": "Under 30 seconds",
            },
            {
                "question": "Which regions are required?",
                "options": ("US East", "US West"),
            },
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_reader_counts_and_emits_only_human_reportable_support_records() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_community_session_factory(engine)
    mermaid = 'flowchart LR\nui["UI"] --> api["API"]\n'
    async with sessions() as session:
        session.add_all(
            [
                Board(id="b", name="Board", owner_id="u", realm_id="realm"),
                Ideation(id="idea", board_id="b", title="Idea", created_by="u"),
                Refinement(
                    id="refinement",
                    board_id="b",
                    ideation_id="idea",
                    title="Refinement",
                    created_by="u",
                ),
                Spec(
                    id="spec",
                    board_id="b",
                    refinement_id="refinement",
                    title="Spec",
                    created_by="u",
                ),
                ArchitectureDesign(
                    id="design",
                    board_id="b",
                    parent_type="refinement",
                    refinement_id="refinement",
                    title="Runtime architecture",
                    global_description="The UI calls the API.",
                    entities=[],
                    interfaces=[],
                    diagrams=[
                        {
                            "id": "diagram",
                            "title": "Runtime",
                            "format": "mermaid",
                            "adapter_payload_ref": "diagram-payload",
                            "order_index": 0,
                        }
                    ],
                    created_by="u",
                ),
                ArchitectureDiagramPayload(
                    id="diagram-payload",
                    design_id="design",
                    diagram_id="diagram",
                    board_id="b",
                    storage_backend="database",
                    storage_key="architecture/design/diagram",
                    format="mermaid",
                    payload_text=mermaid,
                    content_hash="a" * 64,
                    size_bytes=len(mermaid.encode("utf-8")),
                ),
            ]
        )
        await session.flush()
        tables = Base.metadata.tables
        await session.execute(
            tables["research_decision_snapshots"].insert(),
            {
                "id": "rdls-empty",
                "board_id": "b",
                "refinement_id": "refinement",
                "refinement_version": 1,
                "heads_json": [],
                "created_at": _NOW,
            },
        )
        await session.execute(
            tables["research_decision_derivations"].insert(),
            {
                "id": "rdld-empty",
                "board_id": "b",
                "spec_id": "spec",
                "spec_version": 1,
                "source_refinement_id": "refinement",
                "source_refinement_version": 1,
                "source_snapshot_id": "rdls-empty",
                "references_json": [],
                "created_at": _NOW,
            },
        )
        await session.execute(
            tables["code_investigation_requests"].insert(),
            {
                "id": "request",
                "board_id": "b",
                "subject_type": "refinement",
                "subject_id": "refinement",
                "subject_version": 1,
                "issued_to_actor_id": "agent",
                "source_ref": "source-main",
                "required_capabilities": ["file_read", "secret_scan"],
                "selector_scope_digest": "b" * 64,
                "expected_head_generation": 0,
                "canonicalization_profile": "code-investigation/v1",
                "limits_profile": "code-investigation-limits/v1",
                "challenge_key_id": "challenge-v1",
                "challenge_token_hash": "c" * 64,
                "single_use": True,
                "status": "consumed",
                "expires_at": _NOW + timedelta(minutes=10),
                "requested_by": "u",
                "created_at": _NOW,
                "consumed_at": _NOW + timedelta(seconds=1),
                "request_payload_sha256": "d" * 64,
                "idempotency_key": "request-once",
            },
        )
        await session.execute(
            tables["code_investigation_receipts"].insert(),
            {
                "id": "receipt",
                "request_id": "request",
                "board_id": "b",
                "subject_type": "refinement",
                "subject_id": "refinement",
                "subject_version": 1,
                "attestor_actor_id": "agent",
                "generation": 1,
                "trust_level": "single_attestation",
                "acceptance_status": "accepted",
                "outcome": "accessible",
                "capabilities": ["file_read", "secret_scan"],
                "source_ref": "source-main",
                "canonicalization_profile": "code-investigation/v1",
                "limits_profile": "code-investigation-limits/v1",
                "selector_scope_digest": "b" * 64,
                "omission_manifest": [],
                "omission_digest": "e" * 64,
                "omission_count": 0,
                "tooling": {
                    "tool_id": "external-agent",
                    "tool_version": "1",
                    "method_id": "inspection",
                },
                "observed_at": _NOW,
                "received_at": _NOW + timedelta(seconds=1),
                "expires_at": _NOW + timedelta(hours=1),
                "observation_sha256": "f" * 64,
                "payload_sha256": "0" * 64,
                "idempotency_key": "receipt-once",
            },
        )
        await session.commit()

        reader = CommunitySqlAlchemyEntityExportReader(session, clock=lambda: _NOW)
        selected = ("research_decisions", "code_investigations", "architecture")
        bundle = await reader.build_bundle(
            request=EntityExportRequest(
                board_id="b",
                entity_type=EntityExportType.REFINEMENT,
                entity_id="refinement",
                history_scope=EntityExportHistoryScope.COMPLETE,
                requested_sections=selected,
            ),
            disclosure=EntityExportDisclosure(
                frozenset(
                    {
                        "refinement.research_decisions.read",
                        "code_traceability.investigation.read",
                        "refinement.architecture.read",
                    }
                ),
                selected,
            ),
            actor_id="u",
            realm_scope=RealmScope.tenant("realm"),
        )
        raw = bundle.to_dict()
        manifest = {entry["section_key"]: entry for entry in raw["manifest"]["entries"]}
        sections = {section["section_key"]: section for section in raw["sections"]}

        assert manifest["research_decisions"]["status"] == "empty"
        assert manifest["research_decisions"]["total_count"] == 0
        assert not any(sections["research_decisions"]["payload"]["records"].values())
        assert manifest["code_investigations"]["status"] == "empty"
        assert manifest["code_investigations"]["total_count"] == 0
        code_payload = json.dumps(
            sections["code_investigations"]["payload"], sort_keys=True
        )
        assert "capabilities" not in code_payload
        assert "sha256" not in code_payload
        code_records = sections["code_investigations"]["payload"]["records"]
        assert "code_investigation_requests" not in code_records
        assert "code_investigation_receipts" not in code_records
        html = render_entity_export_html(raw)
        assert 'id="section-research_decisions"' not in html
        assert 'id="section-code_investigations"' not in html

        assert manifest["architecture"]["status"] == "included"
        assert manifest["architecture"]["total_count"] == 1
        assert manifest["architecture"]["included_count"] == 1
        assert (
            len(
                sections["architecture"]["payload"]["records"][
                    "architecture_diagram_payloads"
                ]
            )
            == 1
        )

        await session.execute(
            tables["code_investigation_receipt_revocations"].insert(),
            {
                "id": "revocation",
                "receipt_id": "receipt",
                "board_id": "b",
                "reason_code": "attestation_conflict",
                "justification": (
                    "The claimed source result conflicts with a later "
                    "independent observation."
                ),
                "revoked_by": "u",
                "revoked_at": _NOW + timedelta(minutes=2),
            },
        )
        await session.commit()
        curated = await reader.build_bundle(
            request=EntityExportRequest(
                board_id="b",
                entity_type=EntityExportType.REFINEMENT,
                entity_id="refinement",
                history_scope=EntityExportHistoryScope.COMPLETE,
                requested_sections=("code_investigations",),
            ),
            disclosure=EntityExportDisclosure(
                frozenset({"code_traceability.investigation.read"}),
                ("code_investigations",),
            ),
            actor_id="u",
            realm_scope=RealmScope.tenant("realm"),
        )
        curated_raw = curated.to_dict()
        curated_manifest = next(
            entry
            for entry in curated_raw["manifest"]["entries"]
            if entry["section_key"] == "code_investigations"
        )
        curated_section = next(
            section
            for section in curated_raw["sections"]
            if section["section_key"] == "code_investigations"
        )
        assert curated_manifest["status"] == "included"
        assert curated_manifest["total_count"] == 1
        revocation = curated_section["payload"]["records"][
            "code_investigation_receipt_revocations"
        ][0]
        assert revocation == {
            "summary": "Code investigation result revoked",
            "status": "revoked",
            "rationale": (
                "The claimed source result conflicts with a later "
                "independent observation."
            ),
            "reason": "attestation_conflict",
            "created_at": "2026-08-13T12:02:00+00:00",
        }
        curated_html = render_entity_export_html(curated_raw)
        assert 'id="section-code_investigations"' in curated_html
        assert "The claimed source result conflicts" in curated_html
        assert "Capabilities" not in curated_html
        assert "Sha256" not in curated_html

    await engine.dispose()


def test_human_projection_removes_validation_storage_noise() -> None:
    from okto_pulse.community.adapters.sqlalchemy_entity_export import (
        _definitions,
        _human_row_payload,
    )

    spec_definitions = {item.key: item for item in _definitions(EntityExportType.SPEC)}
    requirement_tables = {
        query.table_name for query in spec_definitions["requirement_lint"].queries
    }
    policy_tables = {
        query.table_name for query in spec_definitions["policy_compliance"].queries
    }
    checklist_queries = spec_definitions["checklist"].queries
    assert "requirement_lint_validation_snapshots" not in requirement_tables
    assert "semantic_guideline_validation_scopes" not in policy_tables
    assert "policy_compliance_adopted_revisions" not in policy_tables
    revision_query = next(
        query
        for query in spec_definitions["policy_compliance"].queries
        if query.table_name == "guideline_revisions"
    )
    assert revision_query.emit is False
    assert revision_query.projected_columns == ("revision_id", "title")
    assert [query.table_name for query in checklist_queries] == [
        "checklist_receipts",
        "checklist_item_results",
    ]
    assert checklist_queries[0].emit is False

    checklist = _human_row_payload(
        "checklist_item_results",
        {
            "receipt_id": "receipt-secret",
            "item_id": "chk_scope_boundaries",
            "execution_id": "execution-secret",
            "outcome": "pass",
            "anchor": "spec://secret/chk_scope_boundaries",
            "rationale": "Scope and exclusions are explicit.",
            "order_index": 0,
        },
    )
    assert checklist == {
        "title": "Scope and boundaries",
        "outcome": "pass",
        "description": (
            "The Spec explicitly defines in-scope behavior, out-of-scope "
            "behavior, and relevant boundaries."
        ),
        "rationale": "Scope and exclusions are explicit.",
    }

    policy = _human_row_payload(
        "semantic_guideline_assessment_receipts",
        {
            "receipt_id": "receipt-secret",
            "subject_type": "spec",
            "subject_version": 70,
            "validation_edition": 2,
            "binding_revision": 2,
            "minimum_confidence": 80,
            "confidence": 97,
            "confidence_admissible": True,
            "assessor_independent": False,
            "metric_result_count": 2,
            "failed_metric_count": 0,
            "sealed": True,
            "state": "passed",
        },
    )
    assert policy == {
        "minimum_confidence": 80,
        "confidence": 97,
        "state": "passed",
    }

    lint_finding = _human_row_payload(
        "quality_findings",
        {
            "id": "finding-secret",
            "receipt_id": "receipt-secret",
            "title": "Availability target is not measurable",
            "detail": "Replace 'high availability' with an explicit SLO.",
            "severity": "high",
            "confidence": 96,
            "anchor_ref": "fr_secret",
            "excerpt_hash": "0" * 64,
            "remediation": "State the target percentage and evaluation window.",
        },
    )
    assert lint_finding == {
        "severity": "high",
        "confidence": 96,
        "title": "Availability target is not measurable",
        "detail": "Replace 'high availability' with an explicit SLO.",
        "remediation": "State the target percentage and evaluation window.",
    }


def test_renderers_are_semantically_parallel_structured_and_passive() -> None:
    raw = _bundle().to_dict()
    markdown = render_entity_export_markdown(raw)
    html = render_entity_export_html(raw)
    for fact in (
        "confidence",
        "97",
        "ambiguity",
        "clarity",
        "Source complete",
    ):
        assert fact.casefold() in markdown.casefold()
        assert fact.casefold() in html.casefold()
    assert "Complete for viewer" in markdown
    assert "Complete for viewer" in html
    assert "ac\\_1" not in markdown
    assert "ac_1" not in html
    assert '<div class="score-grid">' in html
    assert '<article class="pinpoint-card">' in html
    assert "<summary>Report completeness</summary>" in html
    assert "0" * 64 not in html
    assert "0" * 64 not in markdown
    assert "<script>" not in html
    assert "onerror=" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x onerror&#61;alert(1)&gt;" in html
    assert "<iframe" not in html
    assert "<object" not in html
    assert "<embed" not in html


def test_metric_codes_are_humanized_instead_of_exposing_storage_punctuation() -> None:
    raw = _bundle(title="Metric report").to_dict()
    raw["sections"][0]["payload"] = {
        "records": {
            "semantic_guideline_metric_results": [
                {
                    "metric_code": "architecture.failClosedSeams",
                    "score": 97,
                    "direction": "minimum",
                    "effective_threshold": 90,
                    "rationale": "The seams fail closed.",
                }
            ]
        }
    }

    html = render_entity_export_html(raw)
    markdown = render_entity_export_markdown(raw)
    assert "Architecture Fail Closed Seams" in html
    assert "Architecture Fail Closed Seams" in markdown
    assert "architecture.failClosedSeams" not in html
    assert "architecture.failClosedSeams" not in markdown


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
    assert "fr_ced7e9e8" not in html
    assert "39ebfd41-f2a4-5fe7-9133-4ee9c0ee2cbc" not in html
    assert "15877207-c147-4805-96d7-d53a625571df" not in html
    assert "5a15282a-b704-5c89-8e15-4338ca3d0b1b" not in html
    assert "Item 1" not in html
    assert "Defines the availability behavior" in html
    assert ">Technical metadata<" not in html
    assert "FR-3 Complete entity coverage: export every authorized fact." in markdown
    assert "39ebfd41-f2a4-5fe7-9133-4ee9c0ee2cbc" not in markdown


def test_current_result_is_visible_and_previous_results_are_collapsed() -> None:
    raw = _bundle(title="Validation history").to_dict()
    raw["sections"][0]["payload"] = {
        "embedded": {
            "validations": [
                {
                    "id": "val_current12345678",
                    "is_current": True,
                    "outcome": "success",
                    "confidence": 97,
                },
                {
                    "id": "val_previous12345678",
                    "is_current": False,
                    "outcome": "failed",
                    "confidence": 61,
                },
            ]
        }
    }
    html = render_entity_export_html(raw)

    assert '<details class="previous-results">' in html
    assert "Previous results <span>1</span>" in html
    assert html.index("97<small>/100") < html.index("Previous results")
    assert "61<small>/100" in html
    assert "val_current12345678" not in html
    assert "val_previous12345678" not in html


def test_html_report_uses_sidebar_navigation_and_collapses_large_sections() -> None:
    raw = _bundle(title="Human report").to_dict()
    raw["sections"][0]["payload"] = {
        "record": {
            "title": "Human report",
            "functional_requirements": [
                {"id": f"fr_{index:08d}", "text": f"FR-{index}: Requirement {index}."}
                for index in range(1, 6)
            ],
        }
    }
    raw["manifest"]["entries"].append(
        {
            "section_key": "cards",
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
            "section_key": "cards",
            "schema_version": "entity-export-section/v1",
            "payload": {
                "records": {
                    "cards": [
                        {
                            "title": "Exercise failover",
                            "card_type": "test",
                            "description": "Verify continuity during an AZ failure.",
                            "details": "Capture outage duration and recovery behavior.",
                            "status": "in_progress",
                            "policy_version": 19,
                            "archived": False,
                        }
                    ]
                }
            },
        }
    )

    html = render_entity_export_html(raw)
    assert '<div class="report-layout"><nav class="toc"' in html
    assert ".toc { position:sticky" in html
    assert '<details class="chapter" id="section-cards">' in html
    assert '<details class="nested-group content-group">' in html
    assert '<span class="card-type-badge">Test</span>' in html
    assert "Verify continuity during an AZ failure." in html
    assert "Capture outage duration and recovery behavior." in html
    assert "Policy Version" not in html
    assert "Archived" not in html


def test_html_report_omits_validation_storage_noise() -> None:
    raw = _bundle(title="Validation report").to_dict()
    raw["sections"][0]["payload"] = {
        "records": {
            "requirement_lint_validation_snapshots": [{"id": "snapshot-secret"}],
            "semantic_guideline_validation_scopes": [{"id": "scope-secret"}],
            "semantic_guideline_assessment_receipts": [
                {
                    "confidence": 97,
                    "minimum_confidence": 80,
                    "subject_type": "spec",
                    "subject_version": 70,
                    "binding_revision": 2,
                    "confidence_admissible": True,
                    "assessor_independent": False,
                    "metric_result_count": 2,
                    "failed_metric_count": 0,
                    "sealed": True,
                }
            ],
        }
    }

    html = render_entity_export_html(raw)
    assert "Requirement Lint Validation Snapshot" not in html
    assert "Semantic Guideline Validation Scope" not in html
    for label in (
        "Subject Type",
        "Subject Version",
        "Binding Revision",
        "Confidence Admissible",
        "Assessor Independent",
        "Metric Result Count",
        "Failed Metric Count",
        "Sealed",
    ):
        assert label not in html
    assert "97<small>/100" in html
    assert "Minimum 80" in html


def test_separated_section_placeholders_are_not_rendered_twice() -> None:
    raw = _bundle(title="Lifecycle report").to_dict()
    raw["sections"][0]["payload"] = {
        "record": {
            "title": "Lifecycle report",
            "validations": {"state": "separated", "section_key": "spec_validation"},
            "test_scenarios": {"state": "separated", "section_key": "test_scenarios"},
        }
    }

    html = render_entity_export_html(raw)
    markdown = render_entity_export_markdown(raw)
    assert "Separated" not in html
    assert "Section Key" not in html
    assert "Separated" not in markdown
    assert "Section Key" not in markdown


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
    assert "ac_2ce4ec3f" not in html
    assert "The condition has an explicit outcome." in html


@pytest.mark.parametrize(
    "entity_type",
    ["story", "ideation", "refinement", "spec", "sprint", "card"],
)
def test_preflight_wire_contract_supports_all_six_types(
    monkeypatch, entity_type: str
) -> None:
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


def test_download_stale_and_secure_headers_and_render_after_materialization(
    monkeypatch,
) -> None:
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
    assert (
        response.headers["x-export-snapshot-fingerprint"] == bundle.snapshot_fingerprint
    )
    assert response.headers["etag"] == f'"{bundle.snapshot_fingerprint}"'
