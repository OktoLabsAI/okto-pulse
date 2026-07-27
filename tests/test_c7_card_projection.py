"""Real-SQL proof for the C7 atomic card-list projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
    statement_budget,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.core.application.use_cases.entity_pagination import EntityPageService
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models import CardPageItem
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    PageRequest,
    get_application_persistence_port,
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)

pytestmark = pytest.mark.asyncio


async def test_card_projection_uses_one_atomic_first_pass_and_last_conclusion(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'c7.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES ('b1', 'Board', 'owner', 'local')"
            )
        )
        validations = [
            {"verdict": "pass", "confidence": 91, "completeness": 82, "drift": 7},
            {"verdict": "fail", "confidence": 2, "completeness": 1, "drift": 99},
            {"verdict": "pass", "confidence": 73, "completeness": 96, "drift": 3},
        ]
        conclusions = [
            {"completeness": 11, "drift": 88},
            {"completeness": 87, "drift": 6},
        ]
        await connection.execute(
            text(
                "INSERT INTO cards "
                "(id, board_id, title, description, status, priority, card_type, "
                "position, created_by, labels, validations, conclusions, archived, "
                "created_at, updated_at) VALUES "
                "('c1', 'b1', 'Atomic', NULL, 'done', 'high', 'normal', 1, "
                "'owner', :labels, :validations, :conclusions, 0, "
                "'2026-07-20 10:00:00', '2026-07-20 10:00:00'), "
                "('c2', 'b1', 'Empty', NULL, 'started', 'none', 'test', 2, "
                "'owner', NULL, NULL, NULL, 0, "
                "'2026-07-20 09:00:00', '2026-07-20 09:00:00')"
            ),
            {
                "labels": json.dumps(["blue"]),
                "validations": json.dumps(validations),
                "conclusions": json.dumps(conclusions),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO qa_items "
                "(id, card_id, question, asked_by, answered_at) VALUES "
                "('q1', 'c1', 'Open?', 'owner', NULL), "
                "('q2', 'c1', 'Closed?', 'owner', '2026-07-20 11:00:00')"
            )
        )

    adapter = CommunitySqlAlchemyApplicationPersistence()
    try:
        previous = get_application_persistence_port()
    except Exception:  # noqa: BLE001 - unset is valid in isolated tests
        previous = None
    register_application_persistence_port(adapter)
    try:
        async with AsyncSession(engine) as session:
            session.info["realm_scope"] = RealmScope.local()
            async with statement_budget(session, 3) as budget:
                page = await EntityPageService(session).list(
                    PageRequest(
                        surface="card_list",
                        scope=(
                            ApplicationFilter("board_id", "eq", "b1"),
                            ApplicationFilter("archived", "is_false", None),
                        ),
                        offset=0,
                        limit=25,
                    )
                )
        # Identical filtered/overall predicates share the exact COUNT.
        assert budget.used == 2
        assert page.total_filtered == page.total_overall == 2
        rows = {record.id: record.values for record in page.items}
        assert set(rows["c1"]) == set(CardPageItem.model_fields)
        assert not {
            "details",
            "screen_mockups",
            "knowledge_bases",
            "validations",
            "conclusions",
        } & set(rows["c1"])

        atomic = rows["c1"]
        assert atomic["validations_count"] == 3
        assert atomic["validations_fail_count"] == 1
        assert atomic["validations_has_pass"] is True
        assert (
            atomic["first_pass_confidence"],
            atomic["first_pass_completeness"],
            atomic["first_pass_drift"],
        ) == (91, 82, 7)
        assert atomic["conclusions_count"] == 2
        assert (
            atomic["last_conclusion_completeness"],
            atomic["last_conclusion_drift"],
        ) == (87, 6)
        assert atomic["open_qa_count"] == 1

        empty = rows["c2"]
        assert empty["validations_count"] == 0
        assert empty["validations_fail_count"] == 0
        assert empty["validations_has_pass"] is False
        assert empty["first_pass_confidence"] is None
        assert empty["conclusions_count"] == 0
        assert empty["last_conclusion_completeness"] is None
        assert empty["open_qa_count"] == 0
    finally:
        if previous is None:
            reset_application_persistence_port_for_tests()
        else:
            register_application_persistence_port(previous)
        await engine.dispose()
