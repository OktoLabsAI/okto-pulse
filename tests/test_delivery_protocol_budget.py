"""Fixed, auditable client-profile comparison at the MCP adapter boundary.

This is not a model-token/billing benchmark or an assertion about the minimum
number of calls in 0.4.2. Both supported profiles start with the same seeded,
accepted source observation/target and an already In Progress card. Bootstrap
and the controlled concurrent edit are outside the measured agent trajectory.
All measured commands use registered handlers, real authorization, SQL stores,
native report gates and Community's production MCP result projection.
"""

from contextlib import aclosing, asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from test_delivery_inline_execution import composed as composed_fixture, command
from test_delivery_progress import db as db_fixture
from test_delivery_report import setup
from okto_pulse.community.adapters.mcp_host import _project_transport_tool_result
from okto_pulse.community.adapters.sqlalchemy_code_traceability import (
    CommunitySqlAlchemyCodeInvestigationStore, CommunitySqlAlchemyCodeTraceabilityStore,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import CommunitySqlAlchemyDomainEventPublisher
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card, CardDeliveryEvidenceRecordRow as Record, DomainEventRow,
    ImplementationTargetExecutionRecordRow as Execution, ImplementationTargetRow as Target,
)
from okto_pulse.core.mcp.catalog import CoreMcpCatalog
from okto_pulse.core.mcp.code_traceability_tools import register_code_traceability_tools


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def progress(key, summary, remaining, source_ref):
    return dict(kind="progress", expected_card_version=1, expected_spec_edition=1,
        idempotency_key=key, justification=summary,
        progress=dict(contract_version="delivery-progress/v2", material_change="none",
            source_state=dict(workspace_state="clean", recoverability="declared_commit", source_ref=source_ref,
                declared_revision="a" * 40), remaining=remaining))


async def trajectory(tmp_path, profile):
    tmp_path.mkdir()
    async with aclosing(db_fixture.__wrapped__(tmp_path)) as database:
        db = await anext(database)
        async with aclosing(composed_fixture.__wrapped__(db, SimpleNamespace(param=("target",)))) as prepared:
            seed, original_uow, _, original_actor = await anext(prepared)
            session, uow, transition_actor = await setup(db)
            try:
                uow.services.code_investigations = CommunitySqlAlchemyCodeInvestigationStore(session)
                uow.services.code_traceability = CommunitySqlAlchemyCodeTraceabilityStore(session)

                async def publish(event):
                    await CommunitySqlAlchemyDomainEventPublisher().publish(
                        session, event=event, handler_names=("test_observer",))

                uow.services.publish_domain_event = publish

                @asynccontextmanager
                async def scope(**kwargs):
                    try:
                        yield uow
                    except BaseException:
                        await session.rollback()
                        raise

                async def agent(board_id):
                    return SimpleNamespace(agent_id=original_actor.actor_id, agent_name="Agent",
                        board_id=board_id, realm_id="local", permissions=[
                            *original_actor.permissions, *transition_actor.permissions,
                            "code_traceability.evidence.read", "card.entity.read"])

                catalog = CoreMcpCatalog(name="delivery-budget", version="0.4.3")
                register_code_traceability_tools(catalog, get_board_agent=agent,
                    get_uow=lambda: scope, get_settings=SimpleNamespace)
                calls = []

                async def invoke(name, *, error=False, **arguments):
                    tool_name = "okto_pulse_" + name
                    tool = await catalog.get_tool(tool_name)
                    outcome = await tool.fn(**arguments)
                    assert outcome.is_error == error, outcome
                    wire = _project_transport_tool_result(outcome, tool_name=tool_name).to_mcp_result().model_dump(
                        mode="json", by_alias=True, exclude_none=True)
                    calls.append(dict(tool=tool_name, arguments=arguments, response=wire,
                        argument_bytes=len(encoded(arguments)), response_bytes=len(encoded(wire))))
                    return outcome.structured_content(tool_name=tool_name) if error else outcome.payload

                async def read(*, resume=False):
                    return await invoke("get_delivery_evidence", board_id="b", spec_id="s",
                        **(dict(card_id="c", view="resume") if resume else {}))

                async def write(evidence, **kwargs):
                    return await invoke("record_delivery_evidence", board_id="b", card_id="c", spec_id="s",
                        evidence=evidence, **kwargs)

                compact = profile == "composed"
                source_ref = (await session.get(Target, "target")).source_ref
                await read(resume=compact)
                selected = []
                for key, summary, remaining in (
                    ("decision", "Confirmed parser behavior against the accepted criterion", "Run focused tests"),
                    ("test", "Focused parser tests passed at the unchanged committed revision", "Submit for review"),
                ):
                    saved = await write(progress(key, summary, remaining, source_ref))
                    selected.append(saved["id"])
                    if not compact:
                        await read()

                implementation = command(batch=False).model_dump(mode="json",
                    exclude={"board_id", "card_id", "spec_id", "expected_card_version", "expected_spec_edition", "idempotency_key"},
                    exclude_none=True)
                if not compact:
                    execution = await invoke("submit_implementation_target_execution_receipt", board_id="b", card_id="c",
                        **implementation.pop("execution_submission"), justification=implementation["justification"],
                        idempotency_key="execution")
                    implementation["execution_id"] = execution["record"]["id"]
                    saved = await write(dict(implementation, expected_card_version=1,
                        expected_spec_edition=1, idempotency_key="binding"))
                    selected.append(saved["id"])

                final = progress("handoff", "All assigned implementation facts recorded for review", "Independent verification", source_ref)
                entry = {k: v for k, v in final.items() if k not in {
                    "expected_card_version", "expected_spec_edition", "idempotency_key"}}
                entries = ([dict(implementation, client_ref="implementation")] if compact else [])
                entries.append(dict(entry, client_ref="handoff"))
                report = dict(contract_version="card-delivery-report/v1", expected_card_status="in_progress",
                    batch=dict(contract_version="card-delivery-batch/v1", expected_card_version=1,
                        expected_spec_edition=1, expected_delivery_revision=2 if compact else 3,
                        idempotency_key="report", entries=entries), existing_record_ids=selected,
                    report=dict(status="validation", conclusion="The implementation is ready for review",
                        completeness=100, completeness_justification="Assigned implementation recorded",
                        drift=0, drift_justification="Within assigned scope"))
                accepted = await write(report)
                assert accepted["report"]["accepted_state"]["readiness_evaluated"] is False
                if not compact:
                    await read()

                # Same controlled interference in each profile: not an agent MCP
                # call. No source change, fabricated progress or delivery credit.
                await session.execute(update(Card).where(Card.id == "c").values(policy_version=2))
                await session.commit()
                conflict = deepcopy(report)
                conflict["batch"]["idempotency_key"] = "stale-client"
                conflict["batch"]["expected_delivery_revision"] = 4
                failed = await write(conflict, error=True)
                assert failed["error_code"] == "delivery_version_conflict", failed
                if not compact:
                    await read()
                resumed = await read(resume=True)
                assert resumed["card_version"] == 2 and resumed["status"] == "validation"
                assert resumed["delivery_revision"] == 4
                rows = list((await session.scalars(select(Record).order_by(Record.created_at, Record.id))).all())
                executions = list((await session.scalars(select(Execution))).all())
                events = list((await session.scalars(select(DomainEventRow))).all())
                assert len(rows) == 4 and len(executions) == 1
                assert len({row.id for row in rows}) == 4
                execution = executions[0]
                bindings = [row for row in rows if row.kind == "implementation"]
                assert len(bindings) == 1 and bindings[0].payload["execution_id"] == execution.id
                card = await session.get(Card, "c")
                manifest = card.conclusions[-1]["delivery_manifest"]
                assert {row["id"] for row in manifest["records"]} == {row.id for row in rows}
                by_id = {row.id: row for row in rows}
                for row in rows:
                    batch = row.payload.get("_batch")
                    if batch:
                        head = by_id[batch["head_id"]].payload["_batch"]
                        assert any(item["id"] == row.id and item["client_ref"] == batch["client_ref"]
                            for item in head["receipt"]["entries"])
                        assert head["receipt"]["delivery_revision"] == 4
                # Preserve every semantic field. Only generated IDs/timestamps,
                # transport grouping and idempotency envelopes may differ.
                def normalize(value):
                    if isinstance(value, dict):
                        return {key: normalize(item) for key, item in value.items()}
                    if isinstance(value, list):
                        return [normalize(item) for item in value]
                    if isinstance(value, str) and value in aliases:
                        return aliases[value]
                    return value

                aliases = {execution.id: "execution"}
                for row in rows:
                    aliases[row.id] = row.kind + ":" + row.payload["justification"]
                facts = sorted([dict(board_id=row.board_id, card_id=row.card_id, spec_id=row.spec_id,
                    edition=row.spec_edition, actor_id=row.actor_id, actor_kind=row.actor_kind,
                    kind=row.kind, payload=normalize({key: value for key, value in row.payload.items()
                        if key != "_batch"})) for row in rows], key=lambda item: encoded(item))
                execution_fact = {column.name: getattr(execution, column.name) for column in Execution.__table__.columns
                    if column.name not in {"id", "received_at", "idempotency_key", "payload_sha256"}}
                result = dict(profile=profile, call_count=len(calls),
                    argument_bytes=sum(row["argument_bytes"] for row in calls),
                    response_bytes=sum(row["response_bytes"] for row in calls), calls=calls,
                    facts=facts, execution=execution_fact, event_types=sorted(event.event_type for event in events),
                    manifest_record_count=len(manifest["records"]), conflict=failed,
                    canonical_records=[dict(id=row.id, created_at=row.created_at.isoformat(),
                        payload=row.payload) for row in rows])
                return result
            finally:
                await session.close()


@pytest.mark.asyncio
async def test_fixed_mcp_trajectory_preserves_audit_facts_and_measures_payload(tmp_path):
    granular = await trajectory(tmp_path / "granular", "granular")
    composed = await trajectory(tmp_path / "composed", "composed")
    assert granular["facts"] == composed["facts"]
    assert granular["execution"] == composed["execution"]
    assert granular["event_types"] == composed["event_types"]
    assert granular["manifest_record_count"] == composed["manifest_record_count"] == 4
    metrics = dict(call_reduction=1 - composed["call_count"] / granular["call_count"],
        payload_reduction=1 - (composed["argument_bytes"] + composed["response_bytes"]) /
            (granular["argument_bytes"] + granular["response_bytes"]))
    report = dict(contract_version="delivery-client-profile-benchmark/v1",
        boundary="Registered Core MCP handlers plus production Community CallToolResult projection; canonical UTF-8 JSON",
        baseline="Supported granular writes with full Spec readbacks; not the minimum calls required in 0.4.2",
        bootstrap="Common accepted source observation and target are fixture-seeded, not measured MCP calls",
        concurrent_edit="Identical controlled policy-version edit outside measured agent calls",
        token_billing_measured=False, targets=dict(call_reduction=0.5, payload_reduction=0.6),
        metrics=metrics, profiles=[granular, composed])
    destination = os.environ.get("OKTO_PULSE_PROTOCOL_BENCHMARK_REPORT")
    if destination:
        Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(metrics))
    assert metrics["call_reduction"] >= 0.5
    # A missed payload target must remain visible, never be fixed with padding
    # or by removing required observations from either trajectory.
    assert metrics["payload_reduction"] >= 0.6
