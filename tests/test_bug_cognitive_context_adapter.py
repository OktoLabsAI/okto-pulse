"""Community round-trip tests for the shared bug closeout context producer."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_pulse.community.adapters.bug_cognitive_context import (
    CommunityBugCognitiveContextAssembler,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine,
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    AmendmentHotfixRevision,
    Base,
    Board,
    Card,
    Comment,
    Spec,
)
from okto_pulse.core.kg.bug_cognitive_closure import classify_bug_evidence


class _CanonicalBugReader:
    def __init__(self, result: bool = True, *, fail: bool = False) -> None:
        self.result = result
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def exists(self, *, board_id: str, bug_id: str) -> bool:
        self.calls.append((board_id, bug_id))
        if self.fail:
            raise RuntimeError("graph unavailable")
        return self.result


async def _runtime(path: Path):  # noqa: ANN202
    engine = build_community_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, build_community_session_factory(engine)


async def _seed_full_context(session_factory) -> None:  # noqa: ANN001
    from okto_pulse.core.services.test_scenario_lifecycle import (
        compute_execution_attestation_sha256,
        compute_test_scenario_semantic_sha256,
    )

    acceptance_criteria = [
        {"id": "ac-about", "text": "About renders v0.3.0."},
    ]
    scenario_contract = {
        "id": "scenario-regression",
        "scenario_type": "regression",
        "given": "the compiled Community frontend is installed",
        "when": "the About page is opened",
        "then": "the page renders v0.3.0",
        "linked_criteria": ["ac-about"],
    }
    scenario_sha256 = compute_test_scenario_semantic_sha256(
        board_id="board-bug-context",
        spec_id="spec-bug-context",
        scenario=scenario_contract,
        acceptance_criteria=acceptance_criteria,
    )
    manifest_ref = "mcp://replay/about-version"
    attestation = {
        "schema_version": 2,
        "run_id": "run-community-about-version",
        "executed_at": "2026-07-14T12:00:00+00:00",
        "scenario_id": "scenario-regression",
        "scenario_sha256": scenario_sha256,
        "outcome": "passed",
        "product_runtime_exercised": True,
        "manifest_sha256": "sha256:" + "c" * 64,
        "assertions": [{
            "name": "compiled About version",
            "expected": "v0.3.0",
            "observed": "v0.3.0",
            "status": "passed",
            "message": None,
        }],
        "provenance": {
            "producer": "okto-pulse-community",
            "producer_version": "0.3.0",
            "adapter": "mcp-replay-runner",
            "environment": "test",
        },
    }
    attestation["attestation_sha256"] = compute_execution_attestation_sha256(
        attestation,
        manifest_ref=manifest_ref,
    )
    scenario = {
        **scenario_contract,
        "title": "About version regression",
        "status": "passed",
        "evidence": {
            "manifest_ref": manifest_ref,
            "execution_attestation": attestation,
            "execution_receipt": "legacy-test-fixture-receipt",
        },
    }
    async with session_factory() as session:
        session.add(Board(id="board-bug-context", name="Context", owner_id="owner"))
        session.add(
            Spec(
                id="spec-bug-context",
                board_id="board-bug-context",
                title="Correct About version",
                acceptance_criteria=acceptance_criteria,
                test_scenarios=[scenario],
                created_by="agent",
            )
        )
        session.add_all(
            [
                Card(
                    id="origin-task",
                    board_id="board-bug-context",
                    spec_id="spec-bug-context",
                    title="Build About page",
                    status="done",
                    card_type="normal",
                    created_by="agent",
                ),
                Card(
                    id="regression-test",
                    board_id="board-bug-context",
                    spec_id="spec-bug-context",
                    title="Verify compiled About version",
                    status="done",
                    card_type="test",
                    conclusions=[{"text": "Runtime rendered v0.3.0."}],
                    validations=[{"outcome": "success"}],
                    created_by="agent",
                ),
                Card(
                    id="bug-context",
                    board_id="board-bug-context",
                    spec_id="spec-bug-context",
                    title="Stale About version",
                    status="done",
                    card_type="bug",
                    origin_task_id="origin-task",
                    expected_behavior="About renders v0.3.0.",
                    observed_behavior="About renders v0.2.5.",
                    steps_to_reproduce="Open About in the compiled frontend.",
                    action_plan=(
                        "Root cause was stale build metadata; rebuild the frontend "
                        "bundle from the authoritative release version."
                    ),
                    linked_test_task_ids=["regression-test"],
                    conclusions=[{"text": "Rebuilt and inspected the runtime bundle."}],
                    validations=[{"outcome": "success"}],
                    created_by="agent",
                ),
            ]
        )
        session.add(
            Comment(
                id="comment-context",
                card_id="bug-context",
                content="The compiled bundle hash now matches the release source.",
                author_id="agent",
            )
        )
        session.add(
            AmendmentHotfixRevision(
                id="lineage-context",
                board_id="board-bug-context",
                original_spec_id="spec-bug-context",
                origin_bug_id="bug-context",
                origin_task_ids=["origin-task"],
                affected_task_ids=["origin-task"],
                regression_scenario_ids=["scenario-regression"],
                regression_test_task_ids=["regression-test"],
                automated_regression_refs=["pytest::test_about_version"],
                status="done",
                lineage_state="complete",
                validation_metadata={"verified": True},
                created_by="agent",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_full_context_round_trip_is_classified_by_core(tmp_path: Path) -> None:
    engine, session_factory = await _runtime(tmp_path / "bug-context.db")
    graph = _CanonicalBugReader(True)
    try:
        await _seed_full_context(session_factory)
        async with session_factory() as session:
            context = await CommunityBugCognitiveContextAssembler(graph).assemble(
                session,
                board_id="board-bug-context",
                bug_id="bug-context",
            )

        assert context.eligible_for_closeout is True
        assert context.canonical_bug_present is True
        assert context.linked_test_task_ids == ("regression-test",)
        assert context.linked_test_tasks[0].status == "done"
        assert context.acceptance_criteria == (
            {"id": "ac-about", "text": "About renders v0.3.0."},
        )
        assert context.lineage[0]["lineage_state"] == "complete"
        assert graph.calls == [("board-bug-context", "bug-context")]

        classification = classify_bug_evidence(None, context=context)
        assert classification["evidence_ready"] is True
        assert classification["missing_categories"] == ()
        assert all(classification["categories_present"].values())
        assert "sql:cards/bug-context" in context.provenance_refs
        assert "kg:canonical/bug/bug-context" in context.provenance_refs
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_graph_failure_is_observable_and_never_coerced_to_absent(
    tmp_path: Path,
) -> None:
    engine, session_factory = await _runtime(tmp_path / "bug-context-fail.db")
    try:
        await _seed_full_context(session_factory)
        async with session_factory() as session:
            context = await CommunityBugCognitiveContextAssembler(
                _CanonicalBugReader(fail=True)
            ).assemble(
                session,
                board_id="board-bug-context",
                bug_id="bug-context",
            )

        assert context.canonical_bug_present is None
        assert context.load_errors == ("canonical_bug_probe_failed",)
        assert context.verified is False
        classification = classify_bug_evidence(None, context=context)
        assert classification["context_verified"] is False
        assert classification["evidence_ready"] is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_board_mismatch_does_not_leak_bug_context(tmp_path: Path) -> None:
    engine, session_factory = await _runtime(tmp_path / "bug-context-scope.db")
    graph = _CanonicalBugReader(True)
    try:
        await _seed_full_context(session_factory)
        async with session_factory() as session:
            context = await CommunityBugCognitiveContextAssembler(graph).assemble(
                session,
                board_id="different-board",
                bug_id="bug-context",
            )

        assert context.card_exists is False
        assert context.provenance_refs == ("sql:cards/bug-context",)
        assert graph.calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unrelated_spec_scenario_is_not_bug_evidence(tmp_path: Path) -> None:
    engine, session_factory = await _runtime(tmp_path / "bug-context-scope-link.db")
    try:
        await _seed_full_context(session_factory)
        async with session_factory() as session:
            spec = await session.get(Spec, "spec-bug-context")
            lineage = await session.get(AmendmentHotfixRevision, "lineage-context")
            assert spec is not None and lineage is not None
            spec.test_scenarios = [
                {
                    "id": "unrelated-scenario",
                    "title": "A different feature",
                    "status": "passed",
                    "linked_task_ids": ["unrelated-task"],
                    "evidence": {},
                }
            ]
            await session.delete(lineage)
            await session.commit()

        async with session_factory() as session:
            context = await CommunityBugCognitiveContextAssembler(
                _CanonicalBugReader(True)
            ).assemble(
                session,
                board_id="board-bug-context",
                bug_id="bug-context",
            )

        assert context.test_scenarios == ()
        classification = classify_bug_evidence(None, context=context)
        assert classification["categories_present"]["test_scenarios"] is False
        assert "test_scenarios" in classification["missing_categories"]
    finally:
        await engine.dispose()
