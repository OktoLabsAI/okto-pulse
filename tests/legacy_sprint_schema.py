"""Upgrade-only schema fixture, never added to the runtime metadata.

The four retired table declarations and codecs are frozen from Community
89f495cac475077ea448ce2e22837314a3d770b2, sqlalchemy_models blob
e5c38207a7a66c08bab6d0e2b58816972a6b8487. Relationships to live mappers are
intentionally absent: this represents historical storage, not a live entity.
Surviving tables use isolated copies of the current schema so fixture writes
cannot register old columns or classes in the operational Base.
"""
from datetime import datetime
import uuid

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, ForeignKey, Integer, MetaData, String, Text, TypeDecorator, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, declarative_base, mapped_column

from okto_pulse.community.adapters.legacy_sprint_values import HistoricalSprintStatus, HistoricalSprintLaneType
from okto_pulse.community.adapters.sqlalchemy_models import Base as RuntimeBase, UTCDateTime

RETIRED_TABLES = ('sprints', 'sprint_history', 'sprint_qa_items', 'sprint_activation_baselines')
Base = declarative_base(metadata=MetaData())
for _table in RuntimeBase.metadata.tables.values():
    if _table.name not in RETIRED_TABLES:
        _table.to_metadata(Base.metadata)
if 'sprint_id' not in Base.metadata.tables['cards'].c:
    Base.metadata.tables['cards'].append_column(Column('sprint_id', String(36), ForeignKey('sprints.id', ondelete='SET NULL'), nullable=True, index=True))

class HistoricalSprintStatusType(TypeDecorator):
    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, HistoricalSprintStatus) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return HistoricalSprintStatus(value)


class HistoricalSprintLaneTypeType(TypeDecorator):
    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, HistoricalSprintLaneType) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return HistoricalSprintLaneType(value)


class Sprint(Base):
    """Sprint — an incremental delivery slice of a spec."""

    __tablename__ = "sprints"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[HistoricalSprintStatus] = mapped_column(
        HistoricalSprintStatusType(), default=HistoricalSprintStatus.DRAFT, nullable=False
    )
    lane_type: Mapped[HistoricalSprintLaneType] = mapped_column(
        HistoricalSprintLaneTypeType(),
        default=HistoricalSprintLaneType.NORMAL,
        server_default=text("'normal'"),
        nullable=False,
    )
    origin_sprint_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sprints.id", ondelete="SET NULL"), nullable=True
    )
    origin_bug_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("cards.id", ondelete="SET NULL"), nullable=True
    )
    # Dates
    start_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Sprint-specific fields
    objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Scoped test scenario IDs from spec
    test_scenario_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Scoped business rule IDs from spec
    business_rule_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Qualitative evaluations: [{id, evaluator_id, evaluator_name, evaluator_type, dimensions, overall_score, overall_justification, recommendation, stale, created_at}]
    evaluations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Skip flags (same pattern as Spec)
    skip_test_coverage: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    skip_rules_coverage: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    skip_qualitative_validation: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    validation_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Task validation gate override (null = inherit from spec/board)
    require_task_validation: Mapped[bool | None] = mapped_column(nullable=True)
    validation_min_confidence: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    validation_min_completeness: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    validation_max_drift: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Optimistic concurrency fence (spec bdfdc682, FR2/D2): every ORM
    # UPDATE/DELETE carries ``WHERE version = <loaded>``.  The services keep
    # authoring the increment (``_bump``, materialization, code traceability),
    # hence ``version_id_generator=False``; a stale write raises
    # ``StaleDataError`` instead of regressing or losing a bump.
    __mapper_args__ = {
        "version_id_col": version,
        "version_id_generator": False,
    }
    labels: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    archived: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    pre_archive_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Cancellation justification (ITEM 17): required when moving to 'cancelled';
    # reopening (cancelled -> any other status) clears all three fields.
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships


class SprintHistory(Base):
    """Change history for a sprint."""

    __tablename__ = "sprint_history"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    sprint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sprints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    changes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SprintActivationBaseline(Base):
    """Immutable analytical commitment captured by the activation UoW."""

    __tablename__ = "sprint_activation_baselines"
    __table_args__ = (
        UniqueConstraint("sprint_id", name="uq_sprint_activation_baselines_sprint_id"),
        CheckConstraint(
            "sprint_version >= 1",
            name="ck_sprint_activation_baselines_sprint_version",
        ),
        CheckConstraint(
            "member_count >= 1",
            name="ck_sprint_activation_baselines_member_count",
        ),
    )

    baseline_ref: Mapped[str] = mapped_column(String(96), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sprint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sprints.id", ondelete="CASCADE"),
        nullable=False,
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sprint_version: Mapped[int] = mapped_column(Integer, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    activated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    members: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )


class SprintQAItem(Base):
    """Q&A on a sprint — same pattern as spec/ideation/refinement Q&A."""

    __tablename__ = "sprint_qa_items"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    sprint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sprints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'text'")
    )
    choices: Mapped[list | None] = mapped_column(JSON, nullable=True)
    allow_free_text: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected: Mapped[list | None] = mapped_column(JSON, nullable=True)
    asked_by: Mapped[str] = mapped_column(String(255), nullable=False)
    answered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Card(Base):
    """Old origin column for historical setup/read assertions only."""
    __table__ = Base.metadata.tables['cards']

