"""Exercise F2A/F2B/permission ordering together on one physical database."""

import pytest
from sqlalchemy import update

from okto_pulse.core.ports.historical_archive import ArchiveSection
from okto_pulse.community.adapters.permission_retirement_checkpoint import capture_permission_retirement_checkpoint
from okto_pulse.community.adapters.permission_retirement_cleanup import retire_permission_documents
from okto_pulse.community.adapters.sqlalchemy_models import Board
from test_card_validation_retirement import prepare, raw_cards, run
from test_historical_archive_read import read
from test_permission_retirement_cleanup import old_full
from test_permission_retirement_review_installation import RETIRED
from test_sprint_retirement_access import add_agent
import test_sprint_retirement_inventory as relational

database = relational.database


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_first", [False, True])
async def test_archive_grants_then_card_policy_then_permission_cleanup(database, tmp_path, cleanup_first):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(update(Board).where(Board.id == "board-a").values(owner_id="local-user"))
        await add_agent(connection, "reader", flags=old_full())
    storage, references = await prepare(engine, tmp_path)
    checkpoint = await capture_permission_retirement_checkpoint(engine, migration_id="card-cutover")
    before = await raw_cards(engine)
    if cleanup_first:
        await retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
        # The frozen source fingerprint may not be reinterpreted after pruning.
        with pytest.raises(ValueError, match="archive_changed"):
            await run(engine, storage, references)
        assert await raw_cards(engine) == before
        return
    card_receipt = await run(engine, storage, references)
    permission_receipt = await retire_permission_documents(engine, checkpoint, retired_flags=RETIRED)
    assert card_receipt.card_count == 3 and permission_receipt.changed_documents == 1
    content = (await read(engine, storage)).records()
    assert content[0]["title"] == "Historical Sprint"
    assert (await read(engine, storage, section=ArchiveSection.QA)).records() == []
    assert (await read(engine, storage, section=ArchiveSection.EVALUATIONS)).records() == []
    detached = await raw_cards(engine)
    assert all(detached[identity]["sprint_id"] is None for identity in ("c1", "c2", "c3"))
    # Both completed stages verify their retained evidence instead of attempting
    # to recapture the old source from the transformed live policy or Card rows.
    assert await run(engine, storage, references, expected_receipt=card_receipt) == card_receipt
    assert await retire_permission_documents(engine, checkpoint, retired_flags=RETIRED,
        expected_receipt=permission_receipt) == permission_receipt
    assert await raw_cards(engine) == detached
