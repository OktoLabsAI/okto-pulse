"""SK-B/B11 Community default-guideline pins, REST contracts and materialization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

import okto_pulse.community.app as _community_app  # noqa: F401
import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_default_board_configuration import (
    CommunitySqlAlchemyDefaultBoardConfigurationStore,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
    guideline_revision_content_digest,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    DefaultBoardConfiguration,
    GuidelineBoardBindingRow,
)
from okto_pulse.community.api import default_board_config as api
from okto_pulse.core.domain.guideline_policy import (
    Guideline,
    GuidelineHead,
    GuidelineLifecycleStatus,
    GuidelineRetirement,
    GuidelineRevision,
    GuidelineScope,
)
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.default_board_configuration import (
    DEFAULT_GUIDELINE_REF_NATIVE_FIELDS,
)
from okto_pulse.core.ports.relational_application import (
    register_relational_application_adapter,
    reset_relational_application_adapter_for_tests,
)
from okto_pulse.core.services.main import GuidelineService

NOW = datetime(2026, 7, 29, 20, 0, tzinfo=timezone.utc)
OWNER_ID = "actor-b11"


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


def _revision(
    *,
    guideline_id: str,
    number: int,
    at: datetime,
) -> GuidelineRevision:
    title = f"B11 policy {number}"
    content = f"Immutable B11 content {number}"
    revision_id = str(
        uuid5(
            NAMESPACE_URL,
            f"okto-pulse/tests/b11/{guideline_id}/revision/{number}",
        )
    )
    return GuidelineRevision(
        revision_id=revision_id,
        guideline_id=guideline_id,
        revision_number=number,
        semantic_version=f"1.0.{number - 1}",
        title=title,
        content=content,
        content_digest=guideline_revision_content_digest(
            title=title,
            content=content,
        ),
        rules=(),
        created_by=OWNER_ID,
        created_at=at,
        parent_revision_id=(
            None
            if number == 1
            else str(
                uuid5(
                    NAMESPACE_URL,
                    (
                        "okto-pulse/tests/b11/"
                        f"{guideline_id}/revision/{number - 1}"
                    ),
                )
            )
        ),
    )


def _head(revision: GuidelineRevision, *, at: datetime) -> GuidelineHead:
    return GuidelineHead(
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        revision_number=revision.revision_number,
        semantic_version=revision.semantic_version,
        head_revision=revision.revision_number,
        updated_at=at,
    )


async def _seed_two_revisions(
    session,
    *,
    guideline_id: str,
) -> tuple[GuidelineRevision, GuidelineRevision]:
    revision_1 = _revision(
        guideline_id=guideline_id,
        number=1,
        at=NOW,
    )
    revision_2 = _revision(
        guideline_id=guideline_id,
        number=2,
        at=NOW + timedelta(seconds=2),
    )
    adapter = CommunitySqlAlchemyGuidelinePolicy(session)
    await adapter.create_guideline(
        guideline=Guideline(
            guideline_id=guideline_id,
            owner_id=OWNER_ID,
            scope=GuidelineScope.GLOBAL,
            created_at=NOW,
        ),
        initial_revision=revision_1,
        initial_head=_head(
            revision_1,
            at=NOW + timedelta(seconds=1),
        ),
        idempotency_key=f"create-{guideline_id}",
        request_digest="a" * 64,
    )
    await adapter.append_revision_cas(
        revision=revision_2,
        next_head=_head(
            revision_2,
            at=NOW + timedelta(seconds=3),
        ),
        expected_head_revision=1,
        idempotency_key=f"revise-{guideline_id}",
        request_digest="b" * 64,
    )
    return revision_1, revision_2


def _pin(
    guideline_id: str,
    revision: GuidelineRevision,
    *,
    priority: int = 0,
) -> dict[str, str | int]:
    return {
        "guideline_id": guideline_id,
        "priority": priority,
        "revision_id": revision.revision_id,
        "revision_number": revision.revision_number,
        "semantic_version": revision.semantic_version,
        "revision_digest": revision.content_digest,
    }


def test_b11_native_ref_is_closed_and_requires_an_exact_pin() -> None:
    payload = {
        "guideline_id": "guideline-1",
        "priority": 3,
        "revision_id": "revision-1",
        "revision_number": 1,
        "semantic_version": "1.0.0",
        "revision_digest": "a" * 64,
    }
    parsed = api.GuidelineDefaultRefRequest.model_validate(payload)
    assert parsed.model_dump() == payload
    assert set(api.GuidelineDefaultRefRequest.model_fields) == set(
        DEFAULT_GUIDELINE_REF_NATIVE_FIELDS
    )

    for missing in (
        "revision_id",
        "revision_number",
        "semantic_version",
        "revision_digest",
    ):
        with pytest.raises(ValidationError):
            api.GuidelineDefaultRefRequest.model_validate(
                {key: value for key, value in payload.items() if key != missing}
            )

    for invalid in (
        {**payload, "guideline_version": 1},
        {**payload, "legacy_version": 1},
        {**payload, "unsupported": True},
        {**payload, "priority": True},
    ):
        with pytest.raises(ValidationError):
            api.GuidelineDefaultRefRequest.model_validate(invalid)

    compatible = api._CompatibleGuidelineDefaultRefRequest.model_validate(
        {
            "guideline_id": "legacy-guideline",
            "guideline_version": 7,
            "legacy_version": 11,
            "legacy_version_unresolvable": True,
        }
    )
    assert compatible.revision_id is None
    assert compatible.guideline_version == 7
    assert compatible.model_dump(exclude_unset=True) == {
        "guideline_id": "legacy-guideline",
        "guideline_version": 7,
        "legacy_version": 11,
        "legacy_version_unresolvable": True,
    }
    with pytest.raises(ValidationError):
        api._CompatibleGuidelineDefaultRefRequest.model_validate(
            {"guideline_id": "legacy-guideline", "unknown_legacy_alias": 1}
        )


@pytest.mark.asyncio
async def test_b11_update_route_dumps_refs_and_rejects_duplicates_before_use_case(
    monkeypatch,
) -> None:
    captured: list[dict] = []

    async def execute(self, command, *, actor, uow):
        del self, actor, uow
        captured.append(command.payload)
        return SimpleNamespace(data={"updated": True})

    monkeypatch.setattr(
        api.UpdateDefaultGuidelineRefsUseCase,
        "execute",
        execute,
    )
    payload = {
        "guideline_default_refs": [
            {
                "guideline_id": "guideline-1",
                "priority": 2,
                "revision_id": "revision-1",
                "revision_number": 1,
                "semantic_version": "1.0.0",
                "revision_digest": "a" * 64,
            }
        ]
    }
    result = await api.update_default_guideline_refs(
        template_id="template-1",
        raw=payload,
        db=object(),
        principal=Principal(subject=OWNER_ID, realm_id="local"),
    )
    assert result == {"updated": True}
    assert captured == [payload]
    assert isinstance(captured[0]["guideline_default_refs"][0], dict)

    duplicate = {
        "guideline_default_refs": [
            payload["guideline_default_refs"][0],
            {
                **payload["guideline_default_refs"][0],
                "priority": 9,
            },
        ]
    }
    with pytest.raises(HTTPException) as exc_info:
        await api.update_default_guideline_refs(
            template_id="template-1",
            raw=duplicate,
            db=object(),
            principal=Principal(subject=OWNER_ID, realm_id="local"),
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "default_guideline_duplicate"
    assert len(captured) == 1

    with pytest.raises(HTTPException) as exc_info:
        await api.update_default_guideline_refs(
            template_id="template-1",
            raw={"guideline_default_refs": [{"title": "inline"}]},
            db=object(),
            principal=Principal(subject=OWNER_ID, realm_id="local"),
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "default_guideline_inline_not_allowed"
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_b11_board_config_import_preserves_only_supplied_compat_fields(
    monkeypatch,
) -> None:
    captured: list[dict] = []

    async def execute(self, command, *, actor, uow):
        del self, actor, uow
        captured.extend(command.items)
        return SimpleNamespace(
            payload=lambda *, dry_run: {
                "created": 1,
                "skipped": [],
                "errors": [],
                "dry_run": dry_run,
            }
        )

    monkeypatch.setattr(api.ImportBoardConfigUseCase, "execute", execute)
    legacy_ref = {
        "guideline_id": "legacy-guideline",
        "priority": 3,
        "guideline_version": 7,
        "legacy_version": 11,
        "legacy_version_unresolvable": True,
    }
    response = await api.import_default_board_config(
        envelope={
            "schema_version": "1",
            "kind": "board_config",
            "exported_at": NOW.isoformat(),
            "items": [
                {
                    "scope": "global",
                    "guideline_default_refs": [legacy_ref],
                    "is_active": False,
                }
            ],
        },
        dry_run=True,
        db=object(),
        principal=Principal(subject=OWNER_ID, realm_id="local"),
    )

    assert response["dry_run"] is True
    assert captured[0]["guideline_default_refs"] == [legacy_ref]
    assert captured[0]["activate"] is False


def test_b11_candidate_rest_projection_is_closed_and_reasoned(monkeypatch) -> None:
    head = {
        "revision_id": "revision-head",
        "revision_number": 2,
        "semantic_version": "1.1.0",
        "revision_digest": "b" * 64,
    }
    default = {
        "revision_id": "revision-default",
        "revision_number": 1,
        "semantic_version": "1.0.0",
        "revision_digest": "a" * 64,
    }
    payload = {
        "scope": "global",
        "template_id": "template-1",
        "template_version": 4,
        "candidates": [
            {
                "guideline_id": "guideline-active",
                "title": "Active",
                "scope": "global",
                "guideline_version": 2,
                "revision_id": head["revision_id"],
                "revision_number": head["revision_number"],
                "semantic_version": head["semantic_version"],
                "revision_digest": head["revision_digest"],
                "head_revision": head,
                "default_revision": default,
                "retired": False,
                "eligible": True,
                "eligibility_reason": None,
                "is_default": True,
                "priority": 4,
            },
            {
                "guideline_id": "guideline-retired",
                "title": "Retired",
                "scope": "global",
                "guideline_version": 2,
                "revision_id": head["revision_id"],
                "revision_number": head["revision_number"],
                "semantic_version": head["semantic_version"],
                "revision_digest": head["revision_digest"],
                "head_revision": head,
                "default_revision": None,
                "retired": True,
                "eligible": False,
                "eligibility_reason": "guideline_retired",
                "is_default": False,
                "priority": None,
            },
        ],
    }

    async def execute(self, command, *, actor, uow):
        del self, command, actor, uow
        return SimpleNamespace(data=payload)

    monkeypatch.setattr(
        api.ListDefaultGuidelineCandidatesUseCase,
        "execute",
        execute,
    )
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[api.require_user] = lambda: OWNER_ID
    app.dependency_overrides[api.get_unit_of_work] = lambda: object()
    with TestClient(app) as client:
        response = client.get("/api/v1/guidelines/default-candidates")

    assert response.status_code == 200
    assert response.json() == payload
    with pytest.raises(ValidationError):
        api.DefaultGuidelineCandidatesResponse.model_validate(
            {
                **payload,
                "candidates": [
                    {
                        **payload["candidates"][1],
                        "eligibility_reason": "not_a_contract_reason",
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_b11_store_resolves_head_historical_pin_and_retirement(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b11-projection.sqlite3")
    guideline_id = "guideline-b11-projection"
    revision_1: GuidelineRevision
    revision_2: GuidelineRevision
    async with get_session_factory()() as session:
        revision_1, revision_2 = await _seed_two_revisions(
            session,
            guideline_id=guideline_id,
        )
        session.add(
            DefaultBoardConfiguration(
                id="template-b11-projection",
                version=1,
                status="active",
                is_active=True,
                scope="global",
                settings_payload={},
                guideline_default_refs=[_pin(guideline_id, revision_1, priority=3)],
                created_by=OWNER_ID,
            )
        )
        await CommunitySqlAlchemyGuidelinePolicy(session).retire_guideline_cas(
            retirement=GuidelineRetirement(
                retirement_id="retirement-b11-projection",
                guideline_id=guideline_id,
                status=GuidelineLifecycleStatus.RETIRED,
                retired_revision_id=revision_2.revision_id,
                retired_revision_number=revision_2.revision_number,
                retired_semantic_version=revision_2.semantic_version,
                retired_revision_digest=revision_2.content_digest,
                retired_head_revision=revision_2.revision_number,
                reason="No longer applicable.",
                retired_by=OWNER_ID,
                retired_at=NOW + timedelta(seconds=4),
            ),
            expected_head_revision=2,
            idempotency_key="retire-b11-projection",
            request_digest="c" * 64,
        )
        await session.commit()

    async with get_session_factory()() as session:
        store = CommunitySqlAlchemyDefaultBoardConfigurationStore()
        head = await store.get_guideline(
            session,
            guideline_id=guideline_id,
        )
        default = await store.get_guideline_revision(
            session,
            guideline_id=guideline_id,
            revision_id=revision_1.revision_id,
        )
        template = await store.get_template(
            session,
            template_id="template-b11-projection",
        )

    assert head is not None and default is not None and template is not None
    assert head.revision_id == revision_2.revision_id
    assert head.revision_number == 2
    assert default.revision_id == revision_1.revision_id
    assert default.revision_number == 1
    assert head.retired is default.retired is True
    assert template.guideline_default_refs == [
        _pin(guideline_id, revision_1, priority=3)
    ]


@pytest.mark.asyncio
async def test_b11_materialization_keeps_exact_pin_and_rolls_back_partial_batch(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b11-materialization.sqlite3")
    guideline_id = "guideline-b11-materialization"
    other_id = "guideline-b11-materialization-other"
    async with get_session_factory()() as session:
        revision_1, revision_2 = await _seed_two_revisions(
            session,
            guideline_id=guideline_id,
        )
        other_revision_1, _ = await _seed_two_revisions(
            session,
            guideline_id=other_id,
        )
        template = DefaultBoardConfiguration(
            id="template-b11-materialization",
            version=1,
            status="active",
            is_active=True,
            scope="global",
            settings_payload={},
            guideline_default_refs=[_pin(guideline_id, revision_1, priority=7)],
            created_by=OWNER_ID,
        )
        session.add(template)
        await session.commit()

    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    try:
        async with get_session_factory()() as session:
            board = Board(
                id="board-b11-exact",
                name="B11 exact",
                owner_id=OWNER_ID,
                realm_id="local",
                default_config_snapshot={
                    "template_id": template.id,
                    "template_version": template.version,
                },
            )
            session.add(board)
            await session.flush()
            created = await GuidelineService(session).apply_default_guidelines(
                board.id,
                [_pin(guideline_id, revision_1, priority=7)],
                template_id=template.id,
                template_version=template.version,
                actor=OWNER_ID,
            )
            await session.commit()
            assert len(created) == 1

        async with get_session_factory()() as session:
            exact = (
                await session.execute(
                    select(GuidelineBoardBindingRow).where(
                        GuidelineBoardBindingRow.board_id == "board-b11-exact"
                    )
                )
            ).scalar_one()
            assert exact.revision_id == revision_1.revision_id
            assert exact.revision_id != revision_2.revision_id
            assert exact.legacy_template_id == template.id
            assert exact.legacy_template_version == template.version
            assert exact.legacy_guideline_version == revision_1.revision_number

        async with get_session_factory()() as session:
            rollback_template = DefaultBoardConfiguration(
                id="template-b11-rollback",
                version=2,
                status="inactive",
                is_active=False,
                scope="global",
                settings_payload={},
                guideline_default_refs=[],
                created_by=OWNER_ID,
            )
            rollback_board = Board(
                id="board-b11-rollback",
                name="B11 rollback",
                owner_id=OWNER_ID,
                realm_id="local",
                default_config_snapshot={
                    "template_id": rollback_template.id,
                    "template_version": rollback_template.version,
                },
            )
            session.add_all([rollback_template, rollback_board])
            await session.flush()
            invalid_other_pin = {
                **_pin(other_id, other_revision_1, priority=8),
                "revision_id": "missing-revision",
            }
            with pytest.raises(
                ValueError,
                match="default_guideline_revision_not_found",
            ):
                await GuidelineService(session).apply_default_guidelines(
                    rollback_board.id,
                    [
                        _pin(guideline_id, revision_1, priority=1),
                        invalid_other_pin,
                    ],
                    template_id=rollback_template.id,
                    template_version=rollback_template.version,
                    actor=OWNER_ID,
                )
            await session.rollback()

        async with get_session_factory()() as session:
            assert await session.get(Board, "board-b11-rollback") is None
            rolled_back = (
                await session.execute(
                    select(GuidelineBoardBindingRow).where(
                        GuidelineBoardBindingRow.board_id == "board-b11-rollback"
                    )
                )
            ).scalars()
            assert list(rolled_back) == []
    finally:
        reset_relational_application_adapter_for_tests()
