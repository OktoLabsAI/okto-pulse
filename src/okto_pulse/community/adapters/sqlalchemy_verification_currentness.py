"""Native verification authority; no execution, schema repair or implicit commit."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import and_, exists, insert, or_, select, text, tuple_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError

from okto_pulse.core.domain.verification_currentness import (
    AuthenticatedVerificationResult, VerificationContext, VerificationCurrentnessError,
    VerificationHistoryCursor, VerificationHistoryQuery,
    VerificationReservation, VerificationScope, canonical_scenario_sha256,
    next_verification_counter, plan_verification_publication,
    require_current_verification_context, result_from_authenticated_evidence,
    result_from_authenticated_legacy_evidence,
)
from okto_pulse.core.domain.verification_report import (
    parse_verification_report, require_verification_report_context,
)
from okto_pulse.core.ports.test_evidence import resolve_test_evidence_write_verifier
from okto_pulse.core.ports.verification_currentness import (
    AcceptedVerificationResult, VerificationCurrentnessRead, VerificationPublicationResult,
    VerificationHistoryPage,
)
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256

from .sqlalchemy_models import (
    Spec, VerificationAcceptanceRow as Acceptance, VerificationBasisHeadRow as Head,
    VerificationReservationRow as Reservation, VerificationScenarioStateRow as State,
)


MAX_SCENARIOS = 5000
MAX_FACTS = 20000
_SCOPE_KEYS = ("board_id", "spec_id", "spec_edition", "scenario_id")


def _where(model, scope):
    return tuple(getattr(model, key) == getattr(scope, key) for key in _SCOPE_KEYS)


def _reservation(row):
    value = VerificationReservation(context=VerificationContext.model_validate(row.context),
                                    actor_id=row.actor_id, actor_kind=row.actor_kind)
    if (value.context.reservation_id != row.id or value.context.generation != row.generation
        or value.context.ordinal != row.ordinal
        or any(getattr(value.context, key) != getattr(row, key) for key in _SCOPE_KEYS)):
        raise VerificationCurrentnessError("verification_reservation_invalid")
    return value


class CommunityVerificationReservationPort:
    """Own only the short allocation transaction, released before external work."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def reserve(self, request):
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    # Obtain the SQLite writer before reads; an observation must
                    # never be retried automatically after external execution.
                    await session.connection()
                    await session.execute(text("BEGIN IMMEDIATE"))
                    store = CommunityVerificationCurrentnessStore(session)
                    _, scenario, state = await store._facts(request.scope, request.expected_spec_version)
                    if (state.scenario_sha256 != request.scenario_sha256
                        or state.verification_method != request.verification_method):
                        raise VerificationCurrentnessError("verification_context_stale")
                    ordinal = next_verification_counter(state.last_ordinal)
                    context = VerificationContext(**request.scope.model_dump(),
                        reservation_id="verification_reservation_" + uuid4().hex,
                        scenario_sha256=request.scenario_sha256, verification_method=request.verification_method,
                        generation=state.generation, ordinal=ordinal)
                    reserved = VerificationReservation(context=context, actor_id=request.actor_id,
                                                       actor_kind=request.actor_kind)
                    await session.execute(update(State).where(*_where(State, request.scope)).values(last_ordinal=ordinal))
                    await session.execute(insert(Reservation).values(**request.scope.model_dump(),
                        id=context.reservation_id, generation=context.generation, ordinal=context.ordinal,
                        context=context.model_dump(mode="json"), actor_id=request.actor_id, actor_kind=request.actor_kind))
                return reserved
        except DBAPIError as exc:
            raise VerificationCurrentnessError("verification_currentness_conflict") from exc


class CommunityVerificationCurrentnessStore:
    """Publication and reads use the caller's transaction and never commit it."""

    def __init__(self, session, *, verifier=None):
        self.session = session
        self.verifier = verifier

    async def _pin_read_snapshot(self):
        """Keep a caller-owned physical SQLite snapshot without taking a writer."""
        bind = self.session.get_bind()
        if bind is not None and bind.dialect.name == "sqlite":
            connection = await self.session.connection()
            active = await connection.run_sync(lambda sync: bool(getattr(
                getattr(sync.connection, "driver_connection", None), "in_transaction", False)))
            if not active:
                await connection.exec_driver_sql("BEGIN")

    async def read_history(self, query):
        """Bounded keyset on one scope; retained success never revives authority."""
        rows, revision, edition = [], 0, 0
        try:
            if not isinstance(query, VerificationHistoryQuery):
                raise VerificationCurrentnessError("verification_history_query_invalid")
            scope, cursor = query.scope, query.cursor
            await self._pin_read_snapshot()
            # Authorization belongs to the caller. Recheck the explicit parent
            # identity; a cursor never supplies or broadens this read scope.
            actual = (await self.session.execute(select(Spec.id, Spec.board_id, Spec.edition,
                Spec.test_scenarios, Spec.acceptance_criteria).where(Spec.id == scope.spec_id,
                    Spec.board_id == scope.board_id).execution_options(autoflush=False))).mappings().one_or_none()
            if actual is None or scope.spec_edition > actual["edition"]:
                raise VerificationCurrentnessError("verification_context_stale")
            spec, edition = SimpleNamespace(**actual), actual["edition"]
            state_row = (await self.session.execute(select(State.__table__).where(*_where(State, scope))
                .execution_options(autoflush=False))).mappings().one_or_none()
            if state_row is None:
                raise VerificationCurrentnessError("verification_currentness_unavailable")
            state, revision = SimpleNamespace(**state_row), state_row["revision"]
            if cursor is not None and (cursor.source_revision != revision or cursor.spec_edition_at_read != edition):
                raise VerificationCurrentnessError("verification_history_cursor_stale")
            active = scope.spec_edition == edition and not state.deleted
            scenarios = spec.test_scenarios or []
            if not isinstance(scenarios, list) or any(not isinstance(s, dict) for s in scenarios):
                raise VerificationCurrentnessError("verification_currentness_unavailable")
            if len(scenarios) > MAX_SCENARIOS:
                raise VerificationCurrentnessError("verification_resolution_limit")
            if active:
                self._check_state(spec, self._scenario(spec, scope.scenario_id), state)
            elif scope.spec_edition == edition and any(s.get("id") == scope.scenario_id for s in scenarios):
                raise VerificationCurrentnessError("verification_currentness_unavailable")
            if cursor is not None:
                anchor = await self.session.scalar(select(Acceptance.id).where(*_where(Acceptance, scope),
                    Acceptance.id == cursor.before_acceptance_id, Acceptance.generation == cursor.before_generation,
                    Acceptance.ordinal == cursor.before_ordinal).execution_options(autoflush=False))
                if anchor is None:
                    raise VerificationCurrentnessError("verification_history_cursor_invalid")
            statement = select(Acceptance, Reservation, Head).select_from(Acceptance).outerjoin(
                Reservation, Reservation.id == Acceptance.reservation_id).outerjoin(
                Head, Head.accepted_result_id == Acceptance.id).where(*_where(Acceptance, scope))
            if cursor is not None:
                statement = statement.where(tuple_(Acceptance.generation, Acceptance.ordinal)
                    < (cursor.before_generation, cursor.before_ordinal))
            statement = statement.order_by(Acceptance.generation.desc(), Acceptance.ordinal.desc()).limit(query.limit + 1)
            expanded, has_more = 0, False
            stream = await self.session.stream(statement.execution_options(
                populate_existing=True, autoflush=False, yield_per=1))
            try:
                async for accepted, reserved, head in stream:
                    if len(rows) == query.limit:
                        has_more = True
                        break
                    if reserved is None:
                        raise VerificationCurrentnessError("verification_currentness_unavailable")
                    current = active and head is not None and accepted.generation == state.generation
                    value = self._accepted(accepted, reserved, current=current)
                    if head is not None:
                        self._require_head(head, value)
                    if (accepted.generation > state.generation or (accepted.generation == state.generation
                        and accepted.ordinal > state.last_ordinal)):
                        raise VerificationCurrentnessError("verification_currentness_unavailable")
                    if current:
                        require_current_verification_context(value.result.context, scope=scope,
                            generation=state.generation, scenario_sha256=state.scenario_sha256,
                            verification_method=state.verification_method)
                    report = accepted.evidence.get("verification_report") or {}
                    expanded += 1 + len(value.result.basis.sources) + len(report.get("observations") or [])
                    if expanded > MAX_FACTS:
                        raise VerificationCurrentnessError("verification_resolution_limit")
                    rows.append(value)
            finally:
                await stream.close()
            next_cursor = None
            if has_more:
                last = rows[-1]
                next_cursor = VerificationHistoryCursor(scope=scope, spec_edition_at_read=edition,
                    source_revision=revision, page_size=query.limit, before_generation=last.result.context.generation,
                    before_ordinal=last.result.context.ordinal, before_acceptance_id=last.accepted_result_id)
            return VerificationHistoryPage(tuple(rows), True, revision, edition, next_cursor)
        except (VerificationCurrentnessError, DBAPIError, ValueError, TypeError, KeyError, OSError) as exc:
            code = exc.code if isinstance(exc, VerificationCurrentnessError) else "verification_currentness_unavailable"
            return VerificationHistoryPage(tuple(rows), False, revision, edition, reason_codes=(code,))

    async def _spec(self, scope, expected_version=None):
        row = (await self.session.execute(select(Spec.id, Spec.board_id, Spec.edition, Spec.version,
            Spec.test_scenarios, Spec.acceptance_criteria).where(Spec.id == scope.spec_id,
            Spec.board_id == scope.board_id, Spec.edition == scope.spec_edition)
            .execution_options(autoflush=False))).mappings().one_or_none()
        if row is None:
            raise VerificationCurrentnessError("verification_context_stale")
        if expected_version is not None and (type(expected_version) is not int or expected_version != row["version"]):
            raise VerificationCurrentnessError("verification_currentness_conflict")
        return SimpleNamespace(**row)

    @staticmethod
    def _scenario(spec, scenario_id):
        matches = [s for s in spec.test_scenarios or [] if isinstance(s, dict) and s.get("id") == scenario_id]
        if len(matches) != 1:
            raise VerificationCurrentnessError("verification_context_stale")
        return matches[0]

    @staticmethod
    def _check_state(spec, scenario, state):
        digest = canonical_scenario_sha256(compute_test_scenario_semantic_sha256(
            board_id=spec.board_id, spec_id=spec.id, scenario=scenario,
            acceptance_criteria=spec.acceptance_criteria or []))
        if state is None:
            raise VerificationCurrentnessError("verification_currentness_unavailable")
        if (state.deleted or state.scenario_sha256 != digest
            or state.verification_method != (scenario.get("verification_method") or "automated_test")):
            raise VerificationCurrentnessError("verification_context_stale")

    async def _facts(self, scope, expected_version=None):
        spec = await self._spec(scope, expected_version)
        scenario = self._scenario(spec, scope.scenario_id)
        row = (await self.session.execute(select(State.__table__).where(*_where(State, scope)))).mappings().one_or_none()
        state = SimpleNamespace(**row) if row else None
        self._check_state(spec, scenario, state)
        return spec, scenario, state

    def _authenticate(self, evidence, status, reserved, *, legacy=False):
        verifier = self.verifier or resolve_test_evidence_write_verifier()
        context = reserved.context
        if verifier is None:
            raise VerificationCurrentnessError("verification_currentness_unavailable")
        verdict = verifier.verify(board_id=context.board_id, spec_id=context.spec_id,
            scenario_id=context.scenario_id, scenario_sha256="sha256:" + context.scenario_sha256,
            status=status, actor_id=reserved.actor_id, evidence=evidence)
        if not verdict.verified:
            raise VerificationCurrentnessError("verification_receipt_invalid")
        if legacy:
            if reserved.actor_kind != "unknown":
                raise VerificationCurrentnessError("verification_legacy_seed_invalid")
            result = result_from_authenticated_legacy_evidence(evidence, context=context, actor_id=reserved.actor_id)
        else:
            result = result_from_authenticated_evidence(evidence, actor_id=reserved.actor_id, actor_kind=reserved.actor_kind)
        if result.context != context:
            raise VerificationCurrentnessError("verification_reservation_invalid")
        return result

    def _accepted(self, row, reservation_row, *, current):
        reserved = _reservation(reservation_row)
        legacy = row.origin == "legacy_seed"
        if (row.origin not in {"native", "legacy_seed"}
            or (not legacy and row.migration_source_sha256 is not None)
            or (legacy and (not isinstance(row.migration_source_sha256, str)
                or len(row.migration_source_sha256) != 64
                or any(char not in "0123456789abcdef" for char in row.migration_source_sha256)))):
            raise VerificationCurrentnessError("verification_currentness_unavailable")
        authenticated = self._authenticate(row.evidence, row.evidence_status, reserved, legacy=legacy)
        if (authenticated != AuthenticatedVerificationResult.model_validate(row.result)
            or row.reservation_id != reserved.context.reservation_id
            or row.basis_digest != authenticated.basis.digest or row.receipt_id != authenticated.receipt_id
            or row.generation != authenticated.context.generation or row.ordinal != authenticated.context.ordinal
            or any(getattr(row, key) != getattr(authenticated.context, key) for key in _SCOPE_KEYS)):
            raise VerificationCurrentnessError("verification_currentness_unavailable")
        return AcceptedVerificationResult(row.id, authenticated, current, deepcopy(row.evidence), row.evidence_status, row.origin)

    @staticmethod
    def _require_head(head, accepted):
        result = accepted.result
        if (head.accepted_result_id != accepted.accepted_result_id
            or head.generation != result.context.generation or head.ordinal != result.context.ordinal
            or head.basis_digest != result.basis.digest
            or any(getattr(head, key) != getattr(result.context, key) for key in _SCOPE_KEYS)):
            raise VerificationCurrentnessError("verification_currentness_unavailable")

    async def replay_legacy(self, *, scope, evidence, evidence_status, actor_id, expected_spec_version):
        """A historical signature cannot create new authority after conversion."""
        if evidence.get("verification_context") is not None:
            raise VerificationCurrentnessError("verification_legacy_seed_invalid")
        try:
            with self.session.no_autoflush:
                fence = await self.session.execute(update(State).where(*_where(State, scope),
                    State.deleted.is_(False), exists(select(Spec.id).where(Spec.id == scope.spec_id,
                        Spec.board_id == scope.board_id, Spec.edition == scope.spec_edition,
                        Spec.version == expected_spec_version))).values(generation=State.generation))
                _, _, state = await self._facts(scope, expected_spec_version)
                if fence.rowcount != 1:
                    raise VerificationCurrentnessError("verification_currentness_conflict")
                row = (await self.session.execute(select(Acceptance, Reservation, Head)
                    .select_from(Head).join(Acceptance, Acceptance.id == Head.accepted_result_id)
                    .join(Reservation, Reservation.id == Acceptance.reservation_id)
                    .where(*_where(Head, scope), Head.generation == state.generation,
                        Acceptance.origin == "legacy_seed", Acceptance.receipt_id == evidence.get("execution_receipt"))
                    .execution_options(populate_existing=True))).one_or_none()
                if row is None:
                    raise VerificationCurrentnessError("verification_legacy_seed_not_current")
                accepted = self._accepted(*row[:2], current=True)
                self._require_head(row[2], accepted)
                require_current_verification_context(accepted.result.context, scope=scope,
                    generation=state.generation, scenario_sha256=state.scenario_sha256,
                    verification_method=state.verification_method)
                if (accepted.evidence != evidence or accepted.evidence_status != evidence_status
                    or accepted.result.actor_id != actor_id):
                    raise VerificationCurrentnessError("verification_legacy_seed_invalid")
                return VerificationPublicationResult(accepted, True, False)
        except DBAPIError as exc:
            raise VerificationCurrentnessError("verification_currentness_conflict") from exc

    async def publish(self, result, *, evidence, evidence_status, expected_spec_version):
        context, scope = result.context, result.context.scope
        try:
            with self.session.no_autoflush:
                # A no-change write fences even ORM/bulk competitors. Only the
                # eventual accepted mutation increments the read revision.
                fence = await self.session.execute(update(State).where(*_where(State, scope),
                    State.generation == context.generation, State.deleted.is_(False),
                    exists(select(Spec.id).where(Spec.id == scope.spec_id, Spec.board_id == scope.board_id,
                        Spec.edition == scope.spec_edition, Spec.version == expected_spec_version)))
                    .values(generation=State.generation))
                if fence.rowcount != 1:
                    _, _, actual = await self._facts(scope, expected_spec_version)
                    require_current_verification_context(context, scope=scope, generation=actual.generation,
                        scenario_sha256=actual.scenario_sha256, verification_method=actual.verification_method)
                    raise VerificationCurrentnessError("verification_currentness_conflict")
                _, scenario, state = await self._facts(scope, expected_spec_version)
                require_current_verification_context(context, scope=scope, generation=state.generation,
                    scenario_sha256=state.scenario_sha256, verification_method=state.verification_method)
                reservation_row = await self.session.scalar(select(Reservation).where(Reservation.id == context.reservation_id)
                    .execution_options(populate_existing=True))
                if reservation_row is None:
                    raise VerificationCurrentnessError("verification_reservation_invalid")
                reserved = _reservation(reservation_row)
                if self._authenticate(evidence, evidence_status, reserved) != result:
                    raise VerificationCurrentnessError("verification_currentness_conflict")
                if evidence.get("evidence_class") == "verification_report":
                    require_verification_report_context(parse_verification_report(evidence["verification_report"]),
                        method=state.verification_method, status=evidence_status,
                        criterion_ids=tuple(scenario.get("linked_criteria") or ()))
                prior_row = await self.session.scalar(select(Acceptance).where(or_(
                    Acceptance.reservation_id == context.reservation_id, Acceptance.receipt_id == result.receipt_id))
                    .execution_options(populate_existing=True))
                prior = self._accepted(prior_row, reservation_row, current=False) if prior_row else None
                head_query = select(Acceptance, Reservation, Head).join(Head, Head.accepted_result_id == Acceptance.id).join(
                    Reservation, Reservation.id == Acceptance.reservation_id).where(*_where(Head, scope),
                        Head.generation == state.generation, Head.basis_digest == result.basis.digest)
                head_rows = (await self.session.execute(head_query.execution_options(populate_existing=True))).one_or_none()
                head = self._accepted(*head_rows[:2], current=True) if head_rows else None
                if head:
                    self._require_head(head_rows[2], head)
                plan = plan_verification_publication(result, reservation=reserved, scope=scope,
                    generation=state.generation, scenario_sha256=state.scenario_sha256,
                    verification_method=state.verification_method, basis_head=head.result if head else None,
                    prior_acceptance=prior.result if prior else None, operational_summary_ordinal=state.summary_ordinal)
                if plan.replayed:
                    return VerificationPublicationResult(AcceptedVerificationResult(prior.accepted_result_id, result,
                        plan.current, deepcopy(evidence), evidence_status), True, False)
                identity = "verification_acceptance_" + uuid4().hex
                await self.session.execute(insert(Acceptance).values(**scope.model_dump(), id=identity,
                    reservation_id=context.reservation_id, generation=context.generation, ordinal=context.ordinal,
                    basis_digest=result.basis.digest, receipt_id=result.receipt_id, result=result.model_dump(mode="json"),
                    evidence=deepcopy(evidence), evidence_status=evidence_status))
                statement = sqlite_insert(Head).values(**scope.model_dump(), generation=context.generation,
                    basis_digest=result.basis.digest, ordinal=context.ordinal, accepted_result_id=identity)
                statement = statement.on_conflict_do_update(index_elements=[*_SCOPE_KEYS, "generation", "basis_digest"],
                    set_={"ordinal": statement.excluded.ordinal, "accepted_result_id": statement.excluded.accepted_result_id},
                    where=Head.ordinal < statement.excluded.ordinal)
                if (await self.session.execute(statement)).rowcount != 1:
                    raise VerificationCurrentnessError("verification_currentness_conflict")
                changes = {"revision": next_verification_counter(state.revision)}
                if plan.replace_operational_summary:
                    changes["summary_ordinal"] = context.ordinal
                await self.session.execute(update(State).where(*_where(State, scope)).values(**changes))
                return VerificationPublicationResult(AcceptedVerificationResult(identity, result, True,
                    deepcopy(evidence), evidence_status), False, plan.replace_operational_summary)
        except DBAPIError as exc:
            raise VerificationCurrentnessError("verification_currentness_conflict") from exc

    async def read_current(self, *, board_id, spec_id, spec_edition, scenario_ids, accepted_result_ids=()):
        """Indexed current heads plus explicitly requested history; never latest pass."""
        rows, revision = [], 0
        try:
            if len(scenario_ids) > MAX_SCENARIOS or len(accepted_result_ids) > MAX_FACTS:
                raise VerificationCurrentnessError("verification_resolution_limit")
            scope = VerificationScope(board_id=board_id, spec_id=spec_id, spec_edition=spec_edition,
                                      scenario_id=scenario_ids[0] if scenario_ids else "_scope")
            await self._pin_read_snapshot()
            spec = await self._spec(scope)
            scenarios = spec.test_scenarios or []
            if len(scenarios) > MAX_SCENARIOS:
                raise VerificationCurrentnessError("verification_resolution_limit")
            ids = tuple(dict.fromkeys(scenario_ids or [s["id"] for s in scenarios]))
            states = list((await self.session.execute(select(State).where(State.board_id == board_id,
                State.spec_id == spec_id, State.spec_edition == spec_edition, State.scenario_id.in_(ids))
                .execution_options(populate_existing=True, autoflush=False))).scalars())
            by_id = {state.scenario_id: state for state in states}
            if set(by_id) != set(ids):
                raise VerificationCurrentnessError("verification_currentness_unavailable")
            for identity in ids:
                self._check_state(spec, self._scenario(spec, identity), by_id[identity])
            revision = sum(state.revision for state in states)
            # Begin at the bounded current-head index, not acceptance history.
            # Requested history is a separate primary-key lookup, so an OR does
            # not turn this read into a scan of every old run in the scope.
            query = select(Acceptance, Reservation, Head).select_from(Head).join(State,
                and_(*(getattr(State, key) == getattr(Head, key) for key in _SCOPE_KEYS),
                    State.generation == Head.generation, State.deleted.is_(False))).join(
                Acceptance, Acceptance.id == Head.accepted_result_id).join(
                Reservation, Reservation.id == Acceptance.reservation_id).where(
                    Head.board_id == board_id, Head.spec_id == spec_id,
                    Head.spec_edition == spec_edition, Head.scenario_id.in_(ids)).limit(MAX_FACTS + 1)
            expanded = 0

            async def consume(statement, *, current):
                nonlocal expanded
                # Do not materialize 20,000 large reports before applying the
                # expanded-fact limit. Read one bounded envelope at a time.
                stream = await self.session.stream(statement.execution_options(
                    populate_existing=True, autoflush=False, yield_per=1))
                try:
                    async for record in stream:
                        accepted, reservation = record[:2]
                        state = by_id[accepted.scenario_id]
                        value = self._accepted(accepted, reservation, current=current)
                        if current:
                            self._require_head(record[2], value)
                            require_current_verification_context(value.result.context, scope=value.result.context.scope,
                                generation=state.generation, scenario_sha256=state.scenario_sha256,
                                verification_method=state.verification_method)
                        report = accepted.evidence.get("verification_report") or {}
                        expanded += 1 + len(value.result.basis.sources) + len(report.get("observations") or [])
                        if expanded > MAX_FACTS:
                            raise VerificationCurrentnessError("verification_resolution_limit")
                        rows.append(value)
                finally:
                    await stream.close()

            await consume(query, current=True)
            missing = set(accepted_result_ids) - {row.accepted_result_id for row in rows}
            if missing:
                historical = select(Acceptance, Reservation).join(Reservation,
                    Reservation.id == Acceptance.reservation_id).where(Acceptance.id.in_(missing),
                        Acceptance.board_id == board_id, Acceptance.spec_id == spec_id,
                        Acceptance.spec_edition == spec_edition, Acceptance.scenario_id.in_(ids))
                await consume(historical, current=False)
            if set(accepted_result_ids) - {value.accepted_result_id for value in rows}:
                raise VerificationCurrentnessError("verification_acceptance_not_found")
            return VerificationCurrentnessRead(tuple(rows), True, revision)
        except (VerificationCurrentnessError, DBAPIError, ValueError, TypeError, KeyError, OSError) as exc:
            code = exc.code if isinstance(exc, VerificationCurrentnessError) else "verification_currentness_unavailable"
            return VerificationCurrentnessRead(tuple(rows), False, revision, (code,))
