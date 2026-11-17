"""Scenario epoch and Spec version each advance once per native transaction."""

from copy import deepcopy

import pytest

from test_skb3_semantic_subject_writer_bridge import _database, _seed_subjects
from okto_pulse.community.adapters.sqlalchemy_models import Spec


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_nested", [False, True])
async def test_independent_spec_tokens_survive_flushes_and_savepoint_rollback(
    tmp_path, rollback_nested
):
    engine, factory = await _database(tmp_path / "independent-fences.db")
    try:
        async with factory() as db:
            seed = await _seed_subjects(db)
            await db.commit()
        async with factory() as db:
            spec = await db.get(Spec, seed.spec_id)
            version, epoch = spec.version, spec.test_scenario_policy_epoch
            if rollback_nested:
                nested = await db.begin_nested()
                spec.test_scenarios = [
                    {**spec.test_scenarios[0], "title": "Rolled back"}
                ]
                await db.flush()
                assert (spec.version, spec.test_scenario_policy_epoch) == (
                    version + 1,
                    epoch + 1,
                )
                await nested.rollback()
                await db.refresh(spec)
                assert (spec.version, spec.test_scenario_policy_epoch) == (
                    version,
                    epoch,
                )
            scenario = deepcopy(spec.test_scenarios[0])
            spec.test_scenarios = [{**scenario, "title": "Changed scenario"}]
            await db.flush()
            assert (spec.version, spec.test_scenario_policy_epoch) == (
                version + 1,
                epoch + 1,
            )
            spec.title = "Changed Spec in same transaction"
            spec.test_scenarios = [{**scenario, "title": "Changed twice"}]
            await db.flush()
            assert (spec.version, spec.test_scenario_policy_epoch) == (
                version + 1,
                epoch + 1,
            )
            await db.commit()
        async with factory() as db:
            spec = await db.get(Spec, seed.spec_id)
            assert (spec.version, spec.test_scenario_policy_epoch) == (
                version + 1,
                epoch + 1,
            )
    finally:
        await engine.dispose()
