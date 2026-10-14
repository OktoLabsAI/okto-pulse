"""Indexed manifest reads in the same Community transaction as conclusion."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.spec_planning_manifest import (
    SpecPlanningManifest,
    SpecPlanningManifestConflict,
    SpecPlanningManifestUnavailable,
)
from .sqlalchemy_models import Board, Spec, ValidationSourceRevisionRow


class CommunitySpecPlanningManifest:
    def __init__(self, session: AsyncSession, realm_scope: RealmScope) -> None:
        self._session = session
        self._realm_scope = realm_scope

    async def read_many(
        self, *, board_id: str, spec_ids: tuple[str, ...],
    ) -> tuple[SpecPlanningManifest, ...]:
        if not 1 <= len(spec_ids) <= 50 or len(set(spec_ids)) != len(spec_ids):
            raise ValueError("spec_planning_manifest_batch_invalid")
        subject = aliased(ValidationSourceRevisionRow)
        authority = aliased(ValidationSourceRevisionRow)
        # Column projection deliberately excludes requirements, evaluations,
        # snapshots, receipt bodies and previous editions.
        rows = (await self._session.execute(
            select(
                Spec.id, Spec.edition, Spec.version,
                subject.incarnation.label("subject_incarnation"),
                subject.revision.label("subject_revision"),
                authority.incarnation.label("authority_incarnation"),
                authority.revision.label("authority_revision"),
            )
            .join(Board, Board.id == Spec.board_id)
            .join(subject, (subject.scope_kind == "spec") & (subject.scope_id == Spec.id))
            .join(authority, (authority.scope_kind == "board") & (authority.scope_id == Board.id))
            .where(Spec.id.in_(spec_ids), Board.id == board_id, Board.realm_id == self._realm_scope.realm_id)
        )).all()
        if len(rows) != len(spec_ids):
            raise SpecPlanningManifestUnavailable("Subject, realm or revision authority unavailable.")
        manifests = {
            row.id: SpecPlanningManifest(
                board_id=board_id, spec_id=row.id, edition=row.edition, spec_version=row.version,
                subject_incarnation=row.subject_incarnation, subject_revision=row.subject_revision,
                authority_incarnation=row.authority_incarnation, authority_revision=row.authority_revision,
            ) for row in rows
        }
        return tuple(manifests[spec_id] for spec_id in spec_ids)

    async def require_current(self, expected: SpecPlanningManifest) -> None:
        current, = await self.read_many(board_id=expected.board_id, spec_ids=(expected.spec_id,))
        if current != expected:
            raise SpecPlanningManifestConflict("Planning inputs changed; read the current validation cycle before submitting again.")
