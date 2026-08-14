"""SQLAlchemy database models."""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DDL,
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from okto_pulse.core.domain.amendment_eligibility import (
    AmendmentLineageState,
    AmendmentRevisionStatus,
)
from okto_pulse.core.domain.enums import (
    BugSeverity,
    CardPriority,
    CardStatus,
    CardType,
    IdeationComplexity,
    IdeationStatus,
    RefinementStatus,
    SpecStatus,
    SprintLaneType,
    SprintStatus,
    StoryStatus,
)
from okto_pulse.core.domain.datetime_utils import normalize_utc_datetime
from okto_pulse.community.adapters.sqlalchemy_base import Base

if TYPE_CHECKING:
    pass


# The preparation snapshot consumes the SDLC source rows, relational health
# evidence, and the materialization-generation marker represented by these
# tables.  The schema lifecycle installs one INSERT/UPDATE/DELETE revision
# trigger for every table in this closed census.  Keep the tuple concrete and
# edition-owned: Core policy must never need to know Community table names.
GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES: tuple[str, ...] = (
    "amendment_hotfix_revisions",
    "app_settings",
    "artifact_deletion_tombstones",
    "boards",
    "canonical_debt",
    "cards",
    "code_evidence",
    "code_evidence_dispositions",
    "code_evidence_spec_links",
    "code_investigation_heads",
    "code_investigation_receipt_revocations",
    "code_investigation_receipts",
    "code_investigation_requests",
    "consolidation_audit",
    "consolidation_dead_letter",
    "consolidation_queue",
    "global_update_outbox",
    "ideations",
    "ideation_qa_items",
    "implementation_target_evidence_links",
    "implementation_target_execution_records",
    "implementation_target_resolutions",
    "implementation_target_spec_links",
    "implementation_targets",
    "kg_cognitive_sources",
    "kg_cognitive_source_revisions",
    "kuzu_node_refs",
    "quality_assessment_heads",
    "quality_assessment_receipts",
    "refinements",
    "refinement_qa_items",
    "research_decision_entries",
    "research_decision_heads",
    "spec_dependencies",
    "specs",
    "spec_qa_items",
    "sprints",
    "stories",
)
GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID = "_global"
GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION = "gdsr-fence-v2"
GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION = "gdsr-trigger-manifest-v7"
GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX = "trg_global_discovery_source_revision"


HUMAN_LIFECYCLE_EDITION_SUBJECT_TABLES: tuple[str, ...] = (
    "ideations",
    "refinements",
    "specs",
)


def human_lifecycle_edition_sqlite_trigger_manifest() -> dict[str, tuple[str, str]]:
    """Return the canonical SQLite guards for human lifecycle editions.

    SQLite cannot add the mapped CHECK constraints to a legacy table in
    place.  The migration therefore uses triggers as its compatibility guard,
    while ``create_all`` installs the same guards through ``after_create``.
    Keeping both paths on this one manifest makes a first boot and an upgraded
    database converge to byte-equivalent ``sqlite_master`` trigger SQL.
    """

    manifest: dict[str, tuple[str, str]] = {}
    for table_name in HUMAN_LIFECYCLE_EDITION_SUBJECT_TABLES:
        condition = "NEW.edition IS NULL OR NEW.edition < 1"
        if table_name != "specs":
            condition += (
                " OR (NEW.skip_ambiguity_gate_edition IS NOT NULL "
                "AND NEW.skip_ambiguity_gate_edition < 1)"
            )
        for operation in ("INSERT", "UPDATE"):
            trigger_name = f"trg_{table_name}_lifecycle_edition_{operation.lower()}"
            manifest[trigger_name] = (
                table_name,
                f'CREATE TRIGGER IF NOT EXISTS "{trigger_name}" '
                f'BEFORE {operation} ON "{table_name}" '
                f"WHEN {condition} BEGIN SELECT RAISE(ABORT, "
                "'lifecycle_edition_invalid'); END",
            )
    return manifest


class UTCDateTime(TypeDecorator):
    """Timezone-preserving datetime for dialects that return naive values.

    SQLite drops timezone information even for ``DateTime(timezone=True)``.
    Normalize on both sides so the application and KG always observe aware UTC
    cancellation instants without changing the physical SQL column type.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return normalize_utc_datetime(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return normalize_utc_datetime(value)


class CardTypeType(TypeDecorator):
    """SQLAlchemy type that stores CardType as a string."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, CardType) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return CardType(value)


class BugSeverityType(TypeDecorator):
    """SQLAlchemy type that stores BugSeverity as a string."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, BugSeverity) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return BugSeverity(value)


class CardPriorityType(TypeDecorator):
    """SQLAlchemy type that stores CardPriority as a string but returns the enum on load."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, CardPriority) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return CardPriority(value)


class AmendmentRevisionStatusType(TypeDecorator):
    """SQLAlchemy type that stores AmendmentRevisionStatus as a string (spec 7ea1e4be)."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, AmendmentRevisionStatus) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return AmendmentRevisionStatus(value)


class AmendmentLineageStateType(TypeDecorator):
    """SQLAlchemy type that stores AmendmentLineageState as a string (spec 7ea1e4be)."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, AmendmentLineageState) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return AmendmentLineageState(value)


class IdeationStatusType(TypeDecorator):
    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, IdeationStatus) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return IdeationStatus(value)


class IdeationComplexityType(TypeDecorator):
    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, IdeationComplexity) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return IdeationComplexity(value)


class StoryStatusType(TypeDecorator):
    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, StoryStatus) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return StoryStatus(value)


class RefinementStatusType(TypeDecorator):
    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, RefinementStatus) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return RefinementStatus(value)


class SprintStatusType(TypeDecorator):
    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, SprintStatus) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return SprintStatus(value)


class SprintLaneTypeType(TypeDecorator):
    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, SprintLaneType) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return SprintLaneType(value)


class SpecStatusType(TypeDecorator):
    """SQLAlchemy type that stores SpecStatus as a string but returns the enum on load."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, SpecStatus) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return SpecStatus(value)


class CardStatusType(TypeDecorator):
    """SQLAlchemy type that stores CardStatus as a string but returns the enum on load."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, CardStatus) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return CardStatus(value)


class Board(Base):
    """Board model - represents a Kanban board."""

    __tablename__ = "boards"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    realm_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # Board settings (JSON): {max_scenarios_per_card: int, skip_test_coverage_global: bool}
    settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Applied DefaultBoardConfiguration snapshot metadata (spec 9df814bc / FR4).
    # Lives OUTSIDE Board.settings so it never affects BoardSettings/governance
    # normalization. Shape: {template_id, template_version, scope, applied_at,
    # applied_by, override_summary}. Null for boards created via the no-active-template
    # fallback or legacy boards (no backfill — TR4).
    default_config_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    cards: Mapped[list["Card"]] = relationship(
        "Card", back_populates="board", cascade="all, delete-orphan"
    )
    ideations: Mapped[list["Ideation"]] = relationship(
        "Ideation", back_populates="board", cascade="all, delete-orphan"
    )
    refinements: Mapped[list["Refinement"]] = relationship(
        "Refinement", back_populates="board", cascade="all, delete-orphan"
    )
    topics: Mapped[list["Topic"]] = relationship(
        "Topic", back_populates="board", cascade="all, delete-orphan"
    )
    stories: Mapped[list["Story"]] = relationship(
        "Story", back_populates="board", cascade="all, delete-orphan"
    )
    specs: Mapped[list["Spec"]] = relationship(
        "Spec", back_populates="board", cascade="all, delete-orphan"
    )
    sprints: Mapped[list["Sprint"]] = relationship(
        "Sprint", back_populates="board", cascade="all, delete-orphan"
    )
    agent_grants: Mapped[list["AgentBoard"]] = relationship(
        "AgentBoard", back_populates="board", cascade="all, delete-orphan"
    )
    shares: Mapped[list["BoardShare"]] = relationship(
        "BoardShare", back_populates="board", cascade="all, delete-orphan"
    )


class AppSetting(Base):
    """Community-owned persisted runtime setting row."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(64), nullable=False)


class ResourceNotApplicable(Base):
    """Explicit N/A marker for mandatory SDLC resources.

    Provided resources are inferred from existing artifacts; only the
    consciously-declared absence is persisted here.
    """

    __tablename__ = "resource_not_applicable"
    __table_args__ = (
        Index(
            "ix_resource_na_entity_active",
            "board_id",
            "entity_type",
            "entity_id",
            "resource_type",
            "active",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_channel: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="ui"
    )
    active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    cleared_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    clear_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    board: Mapped["Board"] = relationship("Board")


# ============================================================================
# STORIES
# ============================================================================


class Topic(Base):
    """Topic — board-scoped grouping entity for optional pre-ideation Stories."""

    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("board_id", "name", name="uq_topic_board_name"),
        Index("ix_topics_board_archived", "board_id", "archived"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    board: Mapped["Board"] = relationship("Board", back_populates="topics")
    stories: Mapped[list["Story"]] = relationship("Story", back_populates="topic")


class Story(Base):
    """Story — lightweight optional intake item that can converge into ideations."""

    __tablename__ = "stories"
    __table_args__ = (
        Index("ix_stories_board_status_archived", "board_id", "status", "archived"),
        Index("ix_stories_board_topic", "board_id", "topic_id"),
        Index(
            "ix_stories_board_topic_archived",
            "board_id",
            "topic_id",
            "archived",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    topic_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("topics.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    benefit: Mapped[str | None] = mapped_column(Text, nullable=True)
    labels: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[StoryStatus] = mapped_column(
        StoryStatusType(), default=StoryStatus.DRAFT, nullable=False
    )
    assignee_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    screen_mockups: Mapped[list | None] = mapped_column(JSON, nullable=True)
    archived: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    pre_archive_status: Mapped[str | None] = mapped_column(String(50), nullable=True)

    board: Mapped["Board"] = relationship("Board", back_populates="stories")
    topic: Mapped["Topic"] = relationship("Topic", back_populates="stories")
    ideation_links: Mapped[list["StoryIdeationLink"]] = relationship(
        "StoryIdeationLink", back_populates="story", cascade="all, delete-orphan"
    )


class StoryIdeationLink(Base):
    """Link from one Story to one Ideation; an Ideation may collect many Stories."""

    __tablename__ = "story_ideation_links"
    __table_args__ = (
        UniqueConstraint("story_id", "ideation_id", name="uq_story_ideation_link"),
        Index("uq_story_ideation_link_story", "story_id", unique=True),
        Index("ix_story_ideation_links_board", "board_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    story_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("stories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ideation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ideations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    story: Mapped["Story"] = relationship("Story", back_populates="ideation_links")
    ideation: Mapped["Ideation"] = relationship(
        "Ideation", back_populates="story_links"
    )
    board: Mapped["Board"] = relationship("Board")


# ============================================================================
# IDEATION
# ============================================================================


class Ideation(Base):
    """Ideation — the starting point of the framework. A raw idea that may be refined into specs."""

    __tablename__ = "ideations"
    __table_args__ = (
        CheckConstraint("edition >= 1", name="ck_ideation_edition"),
        CheckConstraint(
            "skip_ambiguity_gate_edition IS NULL OR skip_ambiguity_gate_edition >= 1",
            name="ck_ideation_skip_ambiguity_gate_edition",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    problem_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_approach: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Scope assessment: {"domains": 1-5, "ambiguity": 1-5, "dependencies": 1-5}
    scope_assessment: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    complexity: Mapped[IdeationComplexity | None] = mapped_column(
        IdeationComplexityType(), nullable=True
    )
    status: Mapped[IdeationStatus] = mapped_column(
        IdeationStatusType(), default=IdeationStatus.DRAFT, nullable=False
    )
    # Human-facing review cycle.  Unlike ``version`` this advances only when
    # a non-Draft lifecycle returns to Draft.
    edition: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    assignee_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    labels: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Screen mockups: [{id, title, description, screen_type, html_content, annotations, order}]
    screen_mockups: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Archive support
    archived: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    pre_archive_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Max ambiguity gate (spec 2485780b): per-ideation opt-out of the board's
    # ideation ambiguity gate. Explicit top-level column — NOT stored inside
    # scope_assessment (which is evaluation-owned). Default false; the write
    # path works while the ideation is in evaluating status.
    skip_ambiguity_gate: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    # A skip is effective only for the explicitly recorded lifecycle edition.
    # NULL is intentionally history-only for legacy rows.
    skip_ambiguity_gate_edition: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    # Cancellation justification (ITEM 17): required when moving to 'cancelled';
    # reopening (cancelled -> any other status) clears all three fields.
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    board: Mapped["Board"] = relationship("Board", back_populates="ideations")
    refinements: Mapped[list["Refinement"]] = relationship(
        "Refinement", back_populates="ideation", cascade="all, delete-orphan"
    )
    specs: Mapped[list["Spec"]] = relationship("Spec", back_populates="ideation")
    qa_items: Mapped[list["IdeationQAItem"]] = relationship(
        "IdeationQAItem", back_populates="ideation", cascade="all, delete-orphan"
    )
    knowledge_bases: Mapped[list["IdeationKnowledgeBase"]] = relationship(
        "IdeationKnowledgeBase", back_populates="ideation", cascade="all, delete-orphan"
    )
    history: Mapped[list["IdeationHistory"]] = relationship(
        "IdeationHistory", back_populates="ideation", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["IdeationSnapshot"]] = relationship(
        "IdeationSnapshot", back_populates="ideation", cascade="all, delete-orphan"
    )
    architecture_designs: Mapped[list["ArchitectureDesign"]] = relationship(
        "ArchitectureDesign", back_populates="ideation", cascade="all, delete-orphan"
    )
    story_links: Mapped[list["StoryIdeationLink"]] = relationship(
        "StoryIdeationLink", back_populates="ideation", cascade="all, delete-orphan"
    )

    @property
    def stories(self) -> list["Story"]:
        return [link.story for link in self.story_links if link.story is not None]


class IdeationSnapshot(Base):
    """Immutable snapshot of an ideation at a specific version. Created when status moves to 'done'."""

    __tablename__ = "ideation_snapshots"
    __table_args__ = (
        UniqueConstraint("ideation_id", "version", name="uq_ideation_snapshot_version"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    ideation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ideations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Full state capture
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    problem_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_approach: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_assessment: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    complexity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    labels: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Q&A snapshot (list of {question, answer, asked_by, answered_by})
    qa_snapshot: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ideation: Mapped["Ideation"] = relationship("Ideation", back_populates="snapshots")


class IdeationHistory(Base):
    """Change history for an ideation."""

    __tablename__ = "ideation_history"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    ideation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ideations.id", ondelete="CASCADE"),
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

    ideation: Mapped["Ideation"] = relationship("Ideation", back_populates="history")


class IdeationQAItem(Base):
    """Q&A on an ideation — same pattern as spec Q&A with text + choice support."""

    __tablename__ = "ideation_qa_items"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    ideation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ideations.id", ondelete="CASCADE"),
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
    # Clarification identity is revision/lifecycle aware in the SK-A
    # canonicalization contract. Legacy rows converge to active revision 1.
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    lifecycle: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'active'"),
    )
    tombstoned: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("false"),
    )

    ideation: Mapped["Ideation"] = relationship("Ideation", back_populates="qa_items")


class IdeationKnowledgeBase(Base):
    """Knowledge base item attached to an ideation."""

    __tablename__ = "ideation_knowledge_bases"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    ideation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ideations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="text/markdown"
    )
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_kb_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # R6-IMP4: multi-hop KB lineage. root_source_kb_id = the INITIAL canonical
    # origin (preserved across ideation->refinement->spec->card hops, never
    # overwritten by the immediate parent); immediate_parent_kb_id = the direct
    # parent. source_kb_id stays == immediate parent for back-compat.
    root_source_kb_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    immediate_parent_kb_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    governance_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    ideation: Mapped["Ideation"] = relationship(
        "Ideation", back_populates="knowledge_bases"
    )


# ============================================================================
# REFINEMENT
# ============================================================================


class Refinement(Base):
    """Refinement — a focused analysis of one aspect of an ideation."""

    __tablename__ = "refinements"
    __table_args__ = (
        CheckConstraint("edition >= 1", name="ck_refinement_edition"),
        CheckConstraint(
            "skip_ambiguity_gate_edition IS NULL OR skip_ambiguity_gate_edition >= 1",
            name="ck_refinement_skip_ambiguity_gate_edition",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    ideation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ideations.id", ondelete="CASCADE"),
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
    in_scope: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    out_of_scope: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    analysis: Mapped[str | None] = mapped_column(Text, nullable=True)
    decisions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[RefinementStatus] = mapped_column(
        RefinementStatusType(), default=RefinementStatus.DRAFT, nullable=False
    )
    # Human-facing review cycle; technical revisions continue to use version.
    edition: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    assignee_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    labels: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Screen mockups: [{id, title, description, screen_type, html_content, annotations, order}]
    screen_mockups: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Archive support
    archived: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    pre_archive_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Human-only override for the receipt-backed Refinement ambiguity gate.
    # This is governance metadata and therefore does not bump semantic version.
    skip_ambiguity_gate: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("false"),
    )
    skip_ambiguity_gate_edition: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    # Cancellation justification (ITEM 17): required when moving to 'cancelled';
    # reopening (cancelled -> any other status) clears all three fields.
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    board: Mapped["Board"] = relationship("Board", back_populates="refinements")
    ideation: Mapped["Ideation"] = relationship(
        "Ideation", back_populates="refinements"
    )
    specs: Mapped[list["Spec"]] = relationship("Spec", back_populates="refinement")
    qa_items: Mapped[list["RefinementQAItem"]] = relationship(
        "RefinementQAItem", back_populates="refinement", cascade="all, delete-orphan"
    )
    history: Mapped[list["RefinementHistory"]] = relationship(
        "RefinementHistory", back_populates="refinement", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["RefinementSnapshot"]] = relationship(
        "RefinementSnapshot", back_populates="refinement", cascade="all, delete-orphan"
    )
    knowledge_bases: Mapped[list["RefinementKnowledgeBase"]] = relationship(
        "RefinementKnowledgeBase",
        back_populates="refinement",
        cascade="all, delete-orphan",
    )
    architecture_designs: Mapped[list["ArchitectureDesign"]] = relationship(
        "ArchitectureDesign", back_populates="refinement", cascade="all, delete-orphan"
    )


class RefinementSnapshot(Base):
    """Immutable snapshot of a refinement at a specific version. Created when status moves to 'done'."""

    __tablename__ = "refinement_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "refinement_id", "version", name="uq_refinement_snapshot_version"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    refinement_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("refinements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    in_scope: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    out_of_scope: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    analysis: Mapped[str | None] = mapped_column(Text, nullable=True)
    decisions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    labels: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    qa_snapshot: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Frozen evidence identities/digests inherited by Specs derived from this
    # exact Refinement version.  This is relational Pulse metadata submitted by
    # authenticated agents; Community never reads or re-resolves source code.
    code_evidence_manifest: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    refinement: Mapped["Refinement"] = relationship(
        "Refinement", back_populates="snapshots"
    )


class RefinementKnowledgeBase(Base):
    """Knowledge base item attached to a refinement."""

    __tablename__ = "refinement_knowledge_bases"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    refinement_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("refinements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="text/markdown"
    )
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_kb_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # R6-IMP4: multi-hop KB lineage. root_source_kb_id = the INITIAL canonical
    # origin (preserved across ideation->refinement->spec->card hops, never
    # overwritten by the immediate parent); immediate_parent_kb_id = the direct
    # parent. source_kb_id stays == immediate parent for back-compat.
    root_source_kb_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    immediate_parent_kb_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    governance_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    refinement: Mapped["Refinement"] = relationship(
        "Refinement", back_populates="knowledge_bases"
    )


class RefinementHistory(Base):
    """Change history for a refinement."""

    __tablename__ = "refinement_history"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    refinement_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("refinements.id", ondelete="CASCADE"),
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

    refinement: Mapped["Refinement"] = relationship(
        "Refinement", back_populates="history"
    )


class RefinementQAItem(Base):
    """Q&A on a refinement — same pattern with text + choice support."""

    __tablename__ = "refinement_qa_items"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    refinement_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("refinements.id", ondelete="CASCADE"),
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
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    lifecycle: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'active'"),
    )
    tombstoned: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("false"),
    )

    refinement: Mapped["Refinement"] = relationship(
        "Refinement", back_populates="qa_items"
    )


# ============================================================================
# SPEC
# ============================================================================


class Spec(Base):
    """Spec model - represents a specification that drives card creation."""

    __tablename__ = "specs"
    __table_args__ = (
        CheckConstraint("edition >= 1", name="ck_spec_edition"),
        CheckConstraint(
            "last_started_edition IS NULL OR "
            "(last_started_edition >= 1 AND last_started_edition <= edition)",
            name="ck_spec_last_started_edition",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ideation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("ideations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    refinement_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("refinements.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_refinement_snapshot_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("refinement_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    source_refinement_version: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    functional_requirements: Mapped[list | None] = mapped_column(JSON, nullable=True)
    technical_requirements: Mapped[list | None] = mapped_column(JSON, nullable=True)
    acceptance_criteria: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Test scenarios: [{id, title, linked_criteria, scenario_type, given, when, then, notes, status, linked_task_ids}]
    test_scenarios: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Screen mockups: [{id, title, description, screen_type, html_content, annotations, order}]
    screen_mockups: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Business rules: [{id, title, rule, when, then, linked_requirements, notes}]
    business_rules: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # API contracts: [{id, method, path, description, request_body, response_success, response_errors, linked_requirements, linked_rules, notes}]
    api_contracts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Integration requirements: [{id, title, integration_type, description, provider,
    # consumer, contract_ref, endpoint, method, data_contract, linked_requirements,
    # linked_api_contracts, linked_task_ids, status, notes}]
    integration_requirements: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Observability requirements: [{id, title, signal_type, description, target,
    # metric_name, threshold, severity, owner, linked_requirements,
    # linked_integration_requirements, linked_task_ids, status, notes}]
    observability_requirements: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Decisions (spec 0eb51d3e R2.1 + Decisions formalization):
    # [{id, title, rationale, context, alternatives_considered, supersedes_decision_id,
    #   linked_requirements, linked_task_ids, status, notes}]
    decisions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # If true, spec can move to Done without full test coverage — set by user only
    skip_test_coverage: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    # If true, cards can start without full FR→BR coverage — set by user only
    skip_rules_coverage: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    # If true, cards can start without full TR→Task coverage
    skip_trs_coverage: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    # Decisions coverage gate — default False (enforced) since ideação #10
    # Fase 1 para paridade com TR/BR/Contract. Specs migradas pré-ideação #10
    # mantêm True via migration backward-compat.
    skip_decisions_coverage: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    # If true, spec can move to validated without full API contract coverage
    skip_contract_coverage: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    # If true, spec can move forward without full IR→Task coverage
    skip_ir_coverage: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    # If true, spec can move forward without full OR→Task coverage
    skip_or_coverage: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    # If true, Spec validation can proceed while Code Evidence Matrix items are
    # still neither linked to this Spec nor explicitly dispositioned.
    skip_code_evidence_coverage: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    # If true, spec can skip qualitative validation (validated→in_progress without evaluations)
    skip_qualitative_validation: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    # Minimum avg score for qualitative validation (None = use board or default 70)
    validation_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Task validation gate: when True, cards must pass through "validation" before "done"
    require_task_validation: Mapped[bool | None] = mapped_column(nullable=True)
    # Threshold overrides for task validation (null = inherit from board)
    validation_min_confidence: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    validation_min_completeness: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    validation_max_drift: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Qualitative evaluations: [{id, evaluator_id, evaluator_name, evaluator_type, dimensions, overall_score, overall_justification, recommendation, stale, created_at}]
    evaluations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Spec Validation Gate — append-only history of validation records.
    # Each record: {id, spec_id, board_id, reviewer_id, reviewer_name,
    #  completeness, completeness_justification, assertiveness, assertiveness_justification,
    #  ambiguity, ambiguity_justification, general_justification, recommendation,
    #  outcome, threshold_violations, resolved_thresholds, created_at}
    validations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Pointer to the current active validation id — NULL when cleared by backward move.
    # Content lock is ACTIVE when this is non-NULL and the pointed record has outcome='success'.
    current_validation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Archive support
    archived: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    pre_archive_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Cancellation justification (ITEM 17): required when moving to 'cancelled';
    # reopening (cancelled -> any other status) clears all three fields.
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[SpecStatus] = mapped_column(
        SpecStatusType(), default=SpecStatus.DRAFT, nullable=False
    )
    # Human-facing lifecycle counter. The technical ``version`` below remains
    # the CAS/currentness token and may advance on individual content writes.
    edition: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    # Monotonic proof that this human lifecycle edition has executed.  It is
    # deliberately not cleared on a nominal backward move; a new edition makes
    # the older marker inapplicable without destroying audit meaning.
    last_started_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    assignee_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    labels: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Internal currentness token for embedded test-scenario policy subjects.
    # It advances once per caller UoW when any scenario fact changes.
    test_scenario_policy_epoch: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )

    # Relationships
    board: Mapped["Board"] = relationship("Board", back_populates="specs")
    ideation: Mapped["Ideation | None"] = relationship(
        "Ideation", back_populates="specs"
    )
    refinement: Mapped["Refinement | None"] = relationship(
        "Refinement", back_populates="specs"
    )
    cards: Mapped[list["Card"]] = relationship("Card", back_populates="spec")
    sprints: Mapped[list["Sprint"]] = relationship(
        "Sprint", back_populates="spec", cascade="all, delete-orphan"
    )
    knowledge_bases: Mapped[list["SpecKnowledgeBase"]] = relationship(
        "SpecKnowledgeBase", back_populates="spec", cascade="all, delete-orphan"
    )
    qa_items: Mapped[list["SpecQAItem"]] = relationship(
        "SpecQAItem", back_populates="spec", cascade="all, delete-orphan"
    )
    history: Mapped[list["SpecHistory"]] = relationship(
        "SpecHistory", back_populates="spec", cascade="all, delete-orphan"
    )
    architecture_designs: Mapped[list["ArchitectureDesign"]] = relationship(
        "ArchitectureDesign", back_populates="spec", cascade="all, delete-orphan"
    )


class SpecHistory(Base):
    """Detailed change history for a spec — tracks every modification with field-level diffs."""

    __tablename__ = "spec_history"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    # e.g. "created", "updated", "status_changed", "cards_derived",
    #      "knowledge_added", "knowledge_removed",
    #      "qa_added", "qa_answered"
    actor_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # "user" | "agent"
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Field-level changes: [{"field": "title", "old": "...", "new": "..."}, ...]
    changes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Optional summary/description of the change
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Version of the spec at this point
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    spec: Mapped["Spec"] = relationship("Spec", back_populates="history")


class SpecDependencyBoardLock(Base):
    """SQLite-safe serialization row for one board dependency graph.

    PostgreSQL locks the authoritative ``boards`` row with ``FOR UPDATE``.
    SQLite has no row locks, so an UPSERT against this deliberately inert
    table acquires the database writer lock without touching a product entity
    (and therefore without emitting discovery/currentness noise).
    """

    __tablename__ = "spec_dependency_board_locks"

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "boards.id",
            name="fk_spec_dependency_board_locks_board_id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        primary_key=True,
    )


class SpecDependency(Base):
    """Lifecycle record for one directed Spec prerequisite.

    ``dependent_spec_id`` is the Spec whose execution is gated and
    ``prerequisite_spec_id`` is the Spec that must be Done.  Removal keeps the
    row as an immutable tombstone.  The nullable prerequisite FK is cleared on
    removal so a historical edge never prevents later target deletion; the
    immutable ``prerequisite_spec_ref`` retains the audit identity.
    """

    __tablename__ = "spec_dependencies"
    __table_args__ = (
        CheckConstraint(
            "dependent_spec_id <> prerequisite_spec_ref",
            name="ck_spec_dependency_no_self_reference",
        ),
        CheckConstraint(
            "introduced_at_spec_version >= 1",
            name="ck_spec_dependency_introduced_version",
        ),
        CheckConstraint(
            "source_version_on_create >= 1 AND target_version_on_create >= 1",
            name="ck_spec_dependency_snapshot_versions",
        ),
        CheckConstraint(
            "source_status_on_create IN "
            "('draft', 'review', 'approved', 'validated', 'in_progress', "
            "'done', 'cancelled') AND target_status_on_create IN "
            "('draft', 'review', 'approved', 'validated', 'in_progress', "
            "'done', 'cancelled')",
            name="ck_spec_dependency_snapshot_statuses",
        ),
        CheckConstraint(
            "target_edition_on_create >= 1 "
            "AND length(trim(add_idempotency_key)) >= 1 "
            "AND length(add_request_digest) = 64",
            name="ck_spec_dependency_creation_evidence",
        ),
        CheckConstraint(
            "(active = true AND prerequisite_spec_id IS NOT NULL "
            "AND removed_at IS NULL AND removed_by_id IS NULL "
            "AND removed_by_type IS NULL AND removed_by_name IS NULL "
            "AND removal_reason IS NULL AND removed_at_spec_version IS NULL "
            "AND remove_idempotency_key IS NULL "
            "AND remove_request_digest IS NULL) OR "
            "(active = false AND prerequisite_spec_id IS NULL "
            "AND removed_at IS NOT NULL AND removed_by_id IS NOT NULL "
            "AND removed_by_type IS NOT NULL AND removed_by_name IS NOT NULL "
            "AND length(trim(removal_reason)) >= 1 "
            "AND length(trim(remove_idempotency_key)) >= 1 "
            "AND length(remove_request_digest) = 64 "
            "AND removed_at_spec_version >= introduced_at_spec_version)",
            name="ck_spec_dependency_lifecycle",
        ),
        Index(
            "uq_spec_dependency_active_edge",
            "board_id",
            "dependent_spec_id",
            "prerequisite_spec_ref",
            unique=True,
            sqlite_where=text("active = true"),
            postgresql_where=text("active = true"),
        ),
        Index(
            "ix_spec_dependency_outgoing_keyset",
            "board_id",
            "dependent_spec_id",
            "active",
            "created_at",
            "id",
        ),
        Index(
            "ix_spec_dependency_incoming_keyset",
            "board_id",
            "prerequisite_spec_ref",
            "active",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "boards.id",
            name="fk_spec_dependencies_board_id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    dependent_spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "specs.id",
            name="fk_spec_dependencies_dependent_spec_id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    prerequisite_spec_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "specs.id",
            name="fk_spec_dependencies_prerequisite_spec_id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=True,
    )
    prerequisite_spec_ref: Mapped[str] = mapped_column(String(36), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    resolved_on_create: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    retrospective: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    introduced_at_spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_version_on_create: Mapped[int] = mapped_column(Integer, nullable=False)
    source_status_on_create: Mapped[str] = mapped_column(String(50), nullable=False)
    target_status_on_create: Mapped[str] = mapped_column(String(50), nullable=False)
    target_version_on_create: Mapped[int] = mapped_column(Integer, nullable=False)
    target_title_on_create: Mapped[str] = mapped_column(String(500), nullable=False)
    target_edition_on_create: Mapped[int] = mapped_column(Integer, nullable=False)
    target_ideation_id_on_create: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    add_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    add_request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_type: Mapped[str] = mapped_column(String(50), nullable=False)
    created_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    removed_by_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    removed_by_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    removed_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    removal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    remove_idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    remove_request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    removed_at_spec_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Additive nullable audit snapshots stay physically last so ALTER ADD on
    # legacy SQLite/PostgreSQL tables converges to the same exact column order
    # as a fresh create.  Historical rows remain NULL; current mutable Specs
    # are never used to invent past facts.
    source_title_on_create: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    source_edition_on_create: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_title_on_remove: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    source_edition_on_remove: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_title_on_remove: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    target_edition_on_remove: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SpecDependencyOperation(Base):
    """Immutable idempotency/result ledger for dependency mutations."""

    __tablename__ = "spec_dependency_operations"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('add', 'remove')",
            name="ck_spec_dependency_operation_kind",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) >= 1 AND length(request_digest) = 64",
            name="ck_spec_dependency_operation_identity",
        ),
        CheckConstraint(
            "expected_spec_version >= 1 AND resulting_spec_version >= 1",
            name="ck_spec_dependency_operation_versions",
        ),
        UniqueConstraint(
            "board_id",
            "actor_id",
            "actor_type",
            "operation",
            "idempotency_key",
            name="uq_spec_dependency_operation_idempotency",
        ),
        Index(
            "ix_spec_dependency_operation_source",
            "board_id",
            "dependent_spec_ref",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "boards.id",
            name="fk_spec_dependency_operations_board_id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    dependent_spec_ref: Mapped[str] = mapped_column(String(36), nullable=False)
    dependency_ref: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


SPEC_DEPENDENCY_TRIGGER_PREFIX = "trg_spec_dependency_"


def spec_dependency_sqlite_trigger_manifest() -> dict[str, tuple[str, str]]:
    """Return the closed SQLite immutability-trigger contract for SK-M."""

    return {
        "trg_spec_dependency_board_boundary_insert": (
            "spec_dependencies",
            """CREATE TRIGGER trg_spec_dependency_board_boundary_insert
BEFORE INSERT ON spec_dependencies
WHEN NOT EXISTS (
         SELECT 1 FROM specs
         WHERE id = NEW.dependent_spec_id AND board_id = NEW.board_id
     ) OR
     (NEW.active = 1 AND NOT EXISTS (
         SELECT 1 FROM specs
         WHERE id = NEW.prerequisite_spec_ref AND board_id = NEW.board_id
     )) OR
     (NEW.active = 1 AND (
      NEW.prerequisite_spec_id IS NULL OR
      NEW.prerequisite_spec_id IS NOT NEW.prerequisite_spec_ref))
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_board_boundary_invalid');
END""",
        ),
        "trg_spec_dependency_board_boundary_update": (
            "spec_dependencies",
            """CREATE TRIGGER trg_spec_dependency_board_boundary_update
BEFORE UPDATE ON spec_dependencies
WHEN NOT EXISTS (
         SELECT 1 FROM specs
         WHERE id = NEW.dependent_spec_id AND board_id = NEW.board_id
     ) OR
     (NEW.active = 1 AND NOT EXISTS (
         SELECT 1 FROM specs
         WHERE id = NEW.prerequisite_spec_ref AND board_id = NEW.board_id
     )) OR
     (NEW.active = 1 AND (
      NEW.prerequisite_spec_id IS NULL OR
      NEW.prerequisite_spec_id IS NOT NEW.prerequisite_spec_ref))
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_board_boundary_invalid');
END""",
        ),
        "trg_spec_dependency_spec_board_update": (
            "specs",
            """CREATE TRIGGER trg_spec_dependency_spec_board_update
BEFORE UPDATE ON specs
WHEN NEW.board_id IS NOT OLD.board_id AND (
    EXISTS (
        SELECT 1 FROM spec_dependencies
        WHERE dependent_spec_id = OLD.id
    ) OR EXISTS (
        SELECT 1 FROM spec_dependencies
        WHERE active = 1 AND prerequisite_spec_id = OLD.id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_board_boundary_invalid');
END""",
        ),
        "trg_spec_dependency_started_edition_insert": (
            "specs",
            """CREATE TRIGGER trg_spec_dependency_started_edition_insert
BEFORE INSERT ON specs
WHEN NEW.edition < 1 OR (
    NEW.last_started_edition IS NOT NULL AND
    NEW.last_started_edition <> NEW.edition
)
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_started_edition_invalid');
END""",
        ),
        "trg_spec_dependency_started_edition_update": (
            "specs",
            """CREATE TRIGGER trg_spec_dependency_started_edition_update
BEFORE UPDATE OF edition, last_started_edition ON specs
WHEN NEW.edition < OLD.edition OR
     NEW.edition > OLD.edition + 1 OR
     (NEW.edition = OLD.edition + 1 AND
      NEW.last_started_edition IS NOT OLD.last_started_edition) OR
     (NEW.edition = OLD.edition AND (
      (OLD.last_started_edition IS NOT NULL AND
       NEW.last_started_edition IS NULL) OR
      (OLD.last_started_edition IS NULL AND
       NEW.last_started_edition IS NOT NULL AND
       NEW.last_started_edition <> NEW.edition) OR
      (OLD.last_started_edition IS NOT NULL AND
       NEW.last_started_edition IS NOT NULL AND
       NEW.last_started_edition <> OLD.last_started_edition AND
       NEW.last_started_edition <> NEW.edition)
     ))
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_started_edition_invalid');
END""",
        ),
        "trg_spec_dependency_tombstone_immutable_update": (
            "spec_dependencies",
            """CREATE TRIGGER trg_spec_dependency_tombstone_immutable_update
BEFORE UPDATE ON spec_dependencies
WHEN NOT (
    OLD.active = 1 AND NEW.active = 0 AND
    NEW.id IS OLD.id AND NEW.board_id IS OLD.board_id AND
    NEW.dependent_spec_id IS OLD.dependent_spec_id AND
    NEW.prerequisite_spec_id IS NULL AND
    NEW.prerequisite_spec_ref IS OLD.prerequisite_spec_ref AND
    NEW.resolved_on_create IS OLD.resolved_on_create AND
    NEW.retrospective IS OLD.retrospective AND
    NEW.introduced_at_spec_version IS OLD.introduced_at_spec_version AND
    NEW.source_version_on_create IS OLD.source_version_on_create AND
    NEW.source_status_on_create IS OLD.source_status_on_create AND
    NEW.source_title_on_create IS OLD.source_title_on_create AND
    NEW.source_edition_on_create IS OLD.source_edition_on_create AND
    NEW.target_status_on_create IS OLD.target_status_on_create AND
    NEW.target_version_on_create IS OLD.target_version_on_create AND
    NEW.target_title_on_create IS OLD.target_title_on_create AND
    NEW.target_edition_on_create IS OLD.target_edition_on_create AND
    NEW.target_ideation_id_on_create IS OLD.target_ideation_id_on_create AND
    NEW.add_idempotency_key IS OLD.add_idempotency_key AND
    NEW.add_request_digest IS OLD.add_request_digest AND
    NEW.created_at IS OLD.created_at AND
    NEW.created_by_id IS OLD.created_by_id AND
    NEW.created_by_type IS OLD.created_by_type AND
    NEW.created_by_name IS OLD.created_by_name
)
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_lifecycle_immutable');
END""",
        ),
        "trg_spec_dependency_immutable_delete": (
            "spec_dependencies",
            """CREATE TRIGGER trg_spec_dependency_immutable_delete
BEFORE DELETE ON spec_dependencies
WHEN NOT EXISTS (
         SELECT 1 FROM kg_board_erasure_permits
         WHERE board_id = OLD.board_id
     ) AND NOT EXISTS (
         SELECT 1 FROM artifact_deletion_tombstones
         WHERE board_id = OLD.board_id
           AND artifact_type = 'spec'
           AND artifact_id = OLD.dependent_spec_id
     )
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_delete_forbidden');
END""",
        ),
        "trg_spec_dependency_operation_immutable_update": (
            "spec_dependency_operations",
            """CREATE TRIGGER trg_spec_dependency_operation_immutable_update
BEFORE UPDATE ON spec_dependency_operations
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_operation_immutable');
END""",
        ),
        "trg_spec_dependency_operation_immutable_delete": (
            "spec_dependency_operations",
            """CREATE TRIGGER trg_spec_dependency_operation_immutable_delete
BEFORE DELETE ON spec_dependency_operations
WHEN NOT EXISTS (
    SELECT 1 FROM kg_board_erasure_permits
    WHERE board_id = OLD.board_id
)
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_operation_immutable');
END""",
        ),
    }


def spec_dependency_sqlite_trigger_predecessors() -> dict[str, tuple[str, ...]]:
    """Return exact, upgradeable predecessor DDL for the closed SK-M manifest."""

    return {
        "trg_spec_dependency_tombstone_immutable_update": (
            """CREATE TRIGGER trg_spec_dependency_tombstone_immutable_update
BEFORE UPDATE ON spec_dependencies
WHEN NOT (
    OLD.active = 1 AND NEW.active = 0 AND
    NEW.id IS OLD.id AND NEW.board_id IS OLD.board_id AND
    NEW.dependent_spec_id IS OLD.dependent_spec_id AND
    NEW.prerequisite_spec_id IS NULL AND
    NEW.prerequisite_spec_ref IS OLD.prerequisite_spec_ref AND
    NEW.resolved_on_create IS OLD.resolved_on_create AND
    NEW.retrospective IS OLD.retrospective AND
    NEW.introduced_at_spec_version IS OLD.introduced_at_spec_version AND
    NEW.source_version_on_create IS OLD.source_version_on_create AND
    NEW.source_status_on_create IS OLD.source_status_on_create AND
    NEW.target_status_on_create IS OLD.target_status_on_create AND
    NEW.target_version_on_create IS OLD.target_version_on_create AND
    NEW.target_title_on_create IS OLD.target_title_on_create AND
    NEW.target_edition_on_create IS OLD.target_edition_on_create AND
    NEW.target_ideation_id_on_create IS OLD.target_ideation_id_on_create AND
    NEW.add_idempotency_key IS OLD.add_idempotency_key AND
    NEW.add_request_digest IS OLD.add_request_digest AND
    NEW.created_at IS OLD.created_at AND
    NEW.created_by_id IS OLD.created_by_id AND
    NEW.created_by_type IS OLD.created_by_type AND
    NEW.created_by_name IS OLD.created_by_name
)
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_lifecycle_immutable');
END""",
        ),
        "trg_spec_dependency_started_edition_insert": (
            """CREATE TRIGGER trg_spec_dependency_started_edition_insert
BEFORE INSERT ON specs
WHEN NEW.last_started_edition IS NOT NULL AND
     (NEW.last_started_edition < 1 OR NEW.last_started_edition > NEW.edition)
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_started_edition_invalid');
END""",
        ),
        "trg_spec_dependency_started_edition_update": (
            """CREATE TRIGGER trg_spec_dependency_started_edition_update
BEFORE UPDATE OF edition, last_started_edition ON specs
WHEN NEW.last_started_edition IS NOT NULL AND
     (NEW.last_started_edition < 1 OR NEW.last_started_edition > NEW.edition)
BEGIN
    SELECT RAISE(ABORT, 'spec_dependency_started_edition_invalid');
END""",
        ),
    }


for _dependency_trigger_name, (
    _dependency_trigger_table,
    _dependency_trigger_ddl,
) in spec_dependency_sqlite_trigger_manifest().items():
    # The board-change guard is a Spec trigger whose body queries the SK-M
    # dependency authority.  Install it only when that authority is created:
    # a legitimate partial ``create_all(tables=(Spec, ...))`` must not leave
    # ``specs`` with a trigger that fails every UPDATE because
    # ``spec_dependencies`` does not exist yet.  Full create_all remains
    # deterministic because Spec is an FK predecessor of SpecDependency.
    _dependency_trigger_anchor = (
        SpecDependency.__table__
        if _dependency_trigger_name == "trg_spec_dependency_spec_board_update"
        else Base.metadata.tables[_dependency_trigger_table]
    )
    event.listen(
        _dependency_trigger_anchor,
        "after_create",
        DDL(_dependency_trigger_ddl).execute_if(dialect="sqlite"),
    )


class SpecQAItem(Base):
    """Q&A item on a spec — bidirectional communication between humans and agents during spec refinement.

    Supports three question types:
    - text: Free-text question with free-text answer (default)
    - choice: Single-select question with predefined options
    - multi_choice: Multi-select question with predefined options

    For choice/multi_choice, the answer is stored as JSON with selected option IDs.
    """

    __tablename__ = "spec_qa_items"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'text'")
    )  # "text" | "choice" | "multi_choice"
    # Format: [{"id": "opt_1", "label": "Option A"}, ...]
    choices: Mapped[list | None] = mapped_column(JSON, nullable=True)
    allow_free_text: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    # For choice answers: ["opt_1", "opt_2"]
    selected: Mapped[list | None] = mapped_column(JSON, nullable=True)
    asked_by: Mapped[str] = mapped_column(String(255), nullable=False)
    answered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    lifecycle: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'active'"),
    )
    tombstoned: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("false"),
    )

    # Relationships
    spec: Mapped["Spec"] = relationship("Spec", back_populates="qa_items")


class SpecKnowledgeBase(Base):
    """Knowledge base item attached to a spec — reference documents and context for AI agents."""

    __tablename__ = "spec_knowledge_bases"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="text/markdown"
    )
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_kb_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # R6-IMP4: multi-hop KB lineage. root_source_kb_id = the INITIAL canonical
    # origin (preserved across ideation->refinement->spec->card hops, never
    # overwritten by the immediate parent); immediate_parent_kb_id = the direct
    # parent. source_kb_id stays == immediate parent for back-compat.
    root_source_kb_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    immediate_parent_kb_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    governance_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    spec: Mapped["Spec"] = relationship("Spec", back_populates="knowledge_bases")


# ============================================================================
# SPRINT
# ============================================================================


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
    status: Mapped[SprintStatus] = mapped_column(
        SprintStatusType(), default=SprintStatus.DRAFT, nullable=False
    )
    lane_type: Mapped[SprintLaneType] = mapped_column(
        SprintLaneTypeType(),
        default=SprintLaneType.NORMAL,
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

    @property
    def normal_sprint_created(self) -> bool:
        """Derived response flag for creation surfaces."""
        return self.lane_type == SprintLaneType.NORMAL

    # Relationships
    spec: Mapped["Spec"] = relationship("Spec", back_populates="sprints")
    board: Mapped["Board"] = relationship("Board", back_populates="sprints")
    cards: Mapped[list["Card"]] = relationship(
        "Card",
        back_populates="sprint",
        foreign_keys="Card.sprint_id",
    )
    qa_items: Mapped[list["SprintQAItem"]] = relationship(
        "SprintQAItem", back_populates="sprint", cascade="all, delete-orphan"
    )
    history: Mapped[list["SprintHistory"]] = relationship(
        "SprintHistory", back_populates="sprint", cascade="all, delete-orphan"
    )


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

    sprint: Mapped["Sprint"] = relationship("Sprint", back_populates="history")


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

    sprint: Mapped["Sprint"] = relationship("Sprint", back_populates="qa_items")


# ============================================================================
# CARD
# ============================================================================


class Card(Base):
    """Card model - represents a task/item in the Kanban board."""

    __tablename__ = "cards"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    spec_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sprint_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("sprints.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)  # Rich text/HTML
    status: Mapped[CardStatus] = mapped_column(
        CardStatusType(), default=CardStatus.NOT_STARTED, nullable=False
    )
    priority: Mapped[CardPriority] = mapped_column(
        CardPriorityType(),
        default=CardPriority.NONE,
        nullable=False,
        server_default="none",
    )
    position: Mapped[int] = mapped_column(Integer, default=0)  # Order within column
    assignee_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    labels: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Test scenario IDs from the linked spec that this card addresses
    test_scenario_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Conclusions: [{text, author_id, created_at}] — required when moving to Done
    conclusions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Screen mockups: [{id, title, description, screen_type, html_content, annotations, order}]
    screen_mockups: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Knowledge bases: [{id, title, description, content, mime_type, source}]
    knowledge_bases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Task validations: append-only public assessment fields (including
    # reviewer_id + reviewer_name) plus private idempotency ledger fields
    # (idempotency_key, request_digest and exact response replay snapshot).
    validations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Append-only rejection-cause history for both Task Validation and completion
    # gates. Current always points to one record here; ``source_id`` on that
    # record identifies the immutable validation attempt that caused rejection.
    rejection_records: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    current_rejection_kind: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    current_rejection_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_rejection_code: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    current_rejection_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Bug card fields ---
    card_type: Mapped[CardType] = mapped_column(
        CardTypeType(), default=CardType.NORMAL, nullable=False, server_default="normal"
    )
    # ID of the task that originated this bug (required when card_type=bug)
    origin_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("cards.id", ondelete="SET NULL"), nullable=True
    )
    severity: Mapped[BugSeverity | None] = mapped_column(
        BugSeverityType(), nullable=True
    )
    expected_behavior: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_behavior: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps_to_reproduce: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    # IDs of test task cards linked to this bug for unblocking
    linked_test_task_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Human-controlled bypass for the task->requirement link gate.
    skip_task_requirement_link_gate: Mapped[bool] = mapped_column(
        default=False, nullable=False, server_default=text("false")
    )
    # Archive support
    archived: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    pre_archive_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Cancellation justification (ITEM 17): required when moving to 'cancelled';
    # reopening (cancelled -> any other status) clears all three fields.
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Card lacks the generic SDLC version used by other policy subjects.
    # This token also covers relational policy facts such as dependencies.
    policy_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )

    # Relationships
    board: Mapped["Board"] = relationship("Board", back_populates="cards")
    spec: Mapped["Spec | None"] = relationship("Spec", back_populates="cards")
    sprint: Mapped["Sprint | None"] = relationship(
        "Sprint",
        back_populates="cards",
        foreign_keys=[sprint_id],
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        "Attachment", back_populates="card", cascade="all, delete-orphan"
    )
    qa_items: Mapped[list["QAItem"]] = relationship(
        "QAItem", back_populates="card", cascade="all, delete-orphan"
    )
    comments: Mapped[list["Comment"]] = relationship(
        "Comment", back_populates="card", cascade="all, delete-orphan"
    )
    # Dependencies: cards this card depends on
    dependencies: Mapped[list["CardDependency"]] = relationship(
        "CardDependency",
        foreign_keys="CardDependency.card_id",
        back_populates="card",
        cascade="all, delete-orphan",
    )
    # Dependents: cards that depend on this card
    dependents: Mapped[list["CardDependency"]] = relationship(
        "CardDependency",
        foreign_keys="CardDependency.depends_on_id",
        back_populates="depends_on",
        cascade="all, delete-orphan",
    )
    architecture_designs: Mapped[list["ArchitectureDesign"]] = relationship(
        "ArchitectureDesign", back_populates="card", cascade="all, delete-orphan"
    )


# ============================================================================
# ARCHITECTURE DESIGN
# ============================================================================


class ArchitectureDesign(Base):
    """First-class architecture design attached to ideation, refinement, spec, or card."""

    __tablename__ = "architecture_designs"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN ideation_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN refinement_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN spec_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN card_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_architecture_design_exactly_one_parent",
        ),
        Index("ix_architecture_designs_board_parent", "board_id", "parent_type"),
        Index("ix_architecture_designs_source", "source_ref", "source_version"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    ideation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("ideations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    refinement_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("refinements.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    spec_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    card_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    global_description: Mapped[str] = mapped_column(Text, nullable=False)
    entities: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    interfaces: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    diagrams: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_design_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    stale: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    breaking_change_flag: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    requires_arch_review: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    ideation: Mapped["Ideation | None"] = relationship(
        "Ideation", back_populates="architecture_designs"
    )
    refinement: Mapped["Refinement | None"] = relationship(
        "Refinement", back_populates="architecture_designs"
    )
    spec: Mapped["Spec | None"] = relationship(
        "Spec", back_populates="architecture_designs"
    )
    card: Mapped["Card | None"] = relationship(
        "Card", back_populates="architecture_designs"
    )
    diagram_payloads: Mapped[list["ArchitectureDiagramPayload"]] = relationship(
        "ArchitectureDiagramPayload",
        back_populates="design",
        cascade="all, delete-orphan",
    )
    versions: Mapped[list["ArchitectureDesignVersion"]] = relationship(
        "ArchitectureDesignVersion",
        back_populates="design",
        cascade="all, delete-orphan",
    )
    finding_runs: Mapped[list["ArchitectureFindingRun"]] = relationship(
        "ArchitectureFindingRun", back_populates="design", cascade="all, delete-orphan"
    )
    warning_acknowledgements: Mapped[list["ArchitectureWarningAcknowledgement"]] = (
        relationship(
            "ArchitectureWarningAcknowledgement",
            back_populates="design",
            cascade="all, delete-orphan",
        )
    )

    @property
    def parent_id(self) -> str | None:
        """Return the concrete parent id for Pydantic summaries."""
        return {
            "ideation": self.ideation_id,
            "refinement": self.refinement_id,
            "spec": self.spec_id,
            "card": self.card_id,
        }.get(self.parent_type)

    @property
    def diagrams_count(self) -> int:
        return len(self.diagrams or [])

    @property
    def adapter_payload_refs(self) -> list[str]:
        return [
            diagram["adapter_payload_ref"]
            for diagram in (self.diagrams or [])
            if isinstance(diagram, dict) and diagram.get("adapter_payload_ref")
        ]


class ArchitectureDiagramPayload(Base):
    """Adapter-specific diagram payload stored outside the architecture envelope."""

    __tablename__ = "architecture_diagram_payloads"
    __table_args__ = (
        UniqueConstraint(
            "design_id", "diagram_id", name="uq_architecture_payload_design_diagram"
        ),
        Index("ix_architecture_payload_board", "board_id"),
        Index("ix_architecture_payload_storage_key", "storage_key"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    design_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("architecture_designs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    diagram_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    storage_backend: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="database"
    )
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    format: Mapped[str] = mapped_column(String(50), nullable=False)
    adapter_payload_json: Mapped[dict | list | None] = mapped_column(
        JSON, nullable=True
    )
    payload_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    design: Mapped["ArchitectureDesign"] = relationship(
        "ArchitectureDesign", back_populates="diagram_payloads"
    )


class ArchitectureDesignVersion(Base):
    """Immutable snapshot of an architecture design envelope and diagram refs."""

    __tablename__ = "architecture_design_versions"
    __table_args__ = (
        UniqueConstraint("design_id", "version", name="uq_architecture_design_version"),
        Index("ix_architecture_design_versions_design", "design_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    design_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("architecture_designs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    envelope_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    diagram_refs_snapshot: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    design: Mapped["ArchitectureDesign"] = relationship(
        "ArchitectureDesign", back_populates="versions"
    )


class ArchitectureFindingRun(Base):
    """Latest deterministic architecture critic run for one Architecture Design."""

    __tablename__ = "architecture_finding_runs"
    __table_args__ = (
        UniqueConstraint(
            "design_id",
            "critic_run_id",
            name="uq_architecture_finding_run_design_critic",
        ),
        Index("ix_architecture_finding_runs_board_design", "board_id", "design_id"),
        Index("ix_architecture_finding_runs_current", "design_id", "is_current"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    design_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("architecture_designs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    design_version: Mapped[int] = mapped_column(Integer, nullable=False)
    critic_run_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    is_current: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    active_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    resolved_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    superseded_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    validator_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="user"
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    design: Mapped["ArchitectureDesign"] = relationship(
        "ArchitectureDesign", back_populates="finding_runs"
    )
    findings: Mapped[list["ArchitectureFinding"]] = relationship(
        "ArchitectureFinding", back_populates="run", cascade="all, delete-orphan"
    )


class ArchitectureFinding(Base):
    """One warning finding from a successful ArchitectureFindingRun."""

    __tablename__ = "architecture_findings"
    __table_args__ = (
        UniqueConstraint(
            "design_id",
            "critic_run_id",
            "finding_key",
            name="uq_architecture_finding_run_key",
        ),
        Index("ix_architecture_findings_design_lifecycle", "design_id", "lifecycle"),
        Index("ix_architecture_findings_board_code", "board_id", "warning_code"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("architecture_finding_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    design_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("architecture_designs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    design_version: Mapped[int] = mapped_column(Integer, nullable=False)
    critic_run_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    finding_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    warning_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="warning"
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_fix: Mapped[str | None] = mapped_column(Text, nullable=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    target_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_target_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    diagram_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    diagram_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    raw_warning: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    run: Mapped["ArchitectureFindingRun"] = relationship(
        "ArchitectureFindingRun", back_populates="findings"
    )


class ArchitectureWarningAcknowledgement(Base):
    """Audit-only acknowledgement for warning-bearing architecture saves."""

    __tablename__ = "architecture_warning_acknowledgements"
    __table_args__ = (
        UniqueConstraint(
            "design_id",
            "critic_run_id",
            "finding_key",
            "actor_id",
            name="uq_architecture_warning_ack_actor",
        ),
        Index("ix_architecture_warning_ack_design_run", "design_id", "critic_run_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    design_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("architecture_designs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    design_version: Mapped[int] = mapped_column(Integer, nullable=False)
    critic_run_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    finding_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="user"
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    design: Mapped["ArchitectureDesign"] = relationship(
        "ArchitectureDesign", back_populates="warning_acknowledgements"
    )


class CardDependency(Base):
    """Junction table for card dependencies."""

    __tablename__ = "card_dependencies"
    __table_args__ = (
        UniqueConstraint("card_id", "depends_on_id", name="uq_card_dependency"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    card_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    depends_on_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    card: Mapped["Card"] = relationship(
        "Card", foreign_keys=[card_id], back_populates="dependencies"
    )
    depends_on: Mapped["Card"] = relationship(
        "Card", foreign_keys=[depends_on_id], back_populates="dependents"
    )


class Attachment(Base):
    """Attachment model - files attached to cards."""

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    card_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)  # bytes
    path: Mapped[str] = mapped_column(String(1000), nullable=False)  # Storage path
    uploaded_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    card: Mapped["Card"] = relationship("Card", back_populates="attachments")


class QAItem(Base):
    """Q&A item model - questions and answers within a card."""

    __tablename__ = "qa_items"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    card_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    asked_by: Mapped[str] = mapped_column(String(255), nullable=False)
    answered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    card: Mapped["Card"] = relationship("Card", back_populates="qa_items")


class Comment(Base):
    """Comment model - comments on cards.

    Supports three types:
    - text: Free-text comment (default, backward compatible)
    - choice: Single-select choice board (poll)
    - multi_choice: Multi-select choice board
    """

    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    card_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[str] = mapped_column(String(255), nullable=False)
    comment_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'text'")
    )
    # Choice board data (null for text comments)
    # Format: [{"id": "opt_1", "label": "Option A"}, ...]
    choices: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Responses to choice boards
    # Format: [{"responder_id": "...", "responder_name": "...", "selected": ["opt_1"], "free_text": ""}]
    responses: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Whether the choice board accepts a free-text response in addition to selections
    allow_free_text: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    card: Mapped["Card"] = relationship("Card", back_populates="comments")


class Agent(Base):
    """Agent model - AI agents with API keys for MCP access."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Legacy NOT NULL column kept for schema compatibility. New writes store a
    # non-recoverable marker; authentication uses api_key_hash.
    api_key: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    api_key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    permissions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Granular permission flags (new system) — JSON dict with nested flags
    permission_flags: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Preset ID — FK to permission_presets (nullable, agent may have custom flags without preset)
    preset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    board_grants: Mapped[list["AgentBoard"]] = relationship(
        "AgentBoard", back_populates="agent", cascade="all, delete-orphan"
    )


class AgentBoard(Base):
    """Junction table for agent-board access (N:N)."""

    __tablename__ = "agent_boards"
    __table_args__ = (UniqueConstraint("agent_id", "board_id", name="uq_agent_board"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    granted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Board-scoped permission overrides (AND with agent flags — can only restrict)
    permission_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Relationships
    agent: Mapped["Agent"] = relationship("Agent", back_populates="board_grants")
    board: Mapped["Board"] = relationship("Board", back_populates="agent_grants")


class PermissionPreset(Base):
    """Permission preset — reusable set of permission flags."""

    __tablename__ = "permission_presets"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    owner_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_builtin: Mapped[bool] = mapped_column(default=False)
    base_preset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    flags: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PermissionIntroductionAudit(Base):
    """Append-only evidence for each ordered permission-introduction manifest."""

    __tablename__ = "permission_introduction_audit"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    manifest_version: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    phase: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    classification: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_preset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    before_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    after_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    introduced_true_count: Mapped[int] = mapped_column(Integer, nullable=False)
    introduced_false_count: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_review_required: Mapped[bool] = mapped_column(nullable=False)
    mutation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentSeenItem(Base):
    """Tracks which items an agent has marked as seen."""

    __tablename__ = "agent_seen_items"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "agent_id",
            "item_type",
            "item_id",
            name="uq_agent_seen_item",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # "comment", "qa", "activity", "card"
    item_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BoardShare(Base):
    """Board sharing - grants other users access to a board."""

    __tablename__ = "board_shares"
    __table_args__ = (UniqueConstraint("board_id", "user_id", name="uq_board_share"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    realm_id: Mapped[str] = mapped_column(String(255), nullable=False)
    permission: Mapped[str] = mapped_column(
        String(50), nullable=False, default="viewer"
    )  # "viewer" | "editor" | "admin"
    shared_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    board: Mapped["Board"] = relationship("Board", back_populates="shares")


class Guideline(Base):
    """Reusable guideline — can be global or board-scoped."""

    __tablename__ = "guidelines"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'global'")
    )
    board_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    board_links: Mapped[list["BoardGuideline"]] = relationship(
        "BoardGuideline", back_populates="guideline", cascade="all, delete-orphan"
    )


class BoardGuideline(Base):
    """Association table linking guidelines to boards."""

    __tablename__ = "board_guidelines"
    __table_args__ = (
        UniqueConstraint("board_id", "guideline_id", name="uq_board_guideline"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    guideline_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("guidelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Default-template provenance (spec 8a2fad91 / FR3). Nullable: links created
    # manually or before the umbrella keep NULL (legacy/forward-only, TR5).
    template_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    guideline_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Relationships
    board: Mapped["Board"] = relationship("Board")
    guideline: Mapped["Guideline"] = relationship(
        "Guideline", back_populates="board_links"
    )


class GuidelineRevisionRow(Base):
    """Immutable content/ruleset revision for one stable legacy Guideline identity.

    The existing ``guidelines`` row remains the stable identity during the
    register-before-remove migration.  Published content lives here and is
    protected by dialect-specific UPDATE/DELETE guards installed by the
    Community schema lifecycle.
    """

    __tablename__ = "guideline_revisions"
    __table_args__ = (
        UniqueConstraint(
            "guideline_id",
            "revision_number",
            name="uq_guideline_revision_number",
        ),
        UniqueConstraint(
            "guideline_id",
            "semantic_version",
            name="uq_guideline_revision_semantic_version",
        ),
        # Enables composite same-guideline FKs from heads and bindings.
        UniqueConstraint(
            "guideline_id",
            "revision_id",
            name="uq_guideline_revision_identity",
        ),
        UniqueConstraint(
            "guideline_id",
            "revision_id",
            "revision_number",
            "semantic_version",
            name="uq_guideline_revision_head_snapshot",
        ),
        UniqueConstraint(
            "guideline_id",
            "revision_id",
            "semantic_version",
            "content_digest",
            name="uq_guideline_revision_binding_snapshot",
        ),
        UniqueConstraint(
            "guideline_id",
            "idempotency_key",
            name="uq_guideline_revision_idempotency",
        ),
        CheckConstraint(
            "revision_number >= 1",
            name="ck_guideline_revision_positive_number",
        ),
        CheckConstraint(
            "published_head_revision >= 1",
            name="ck_guideline_revision_positive_published_head",
        ),
        CheckConstraint(
            "length(content_digest) = 64",
            name="ck_guideline_revision_digest_length",
        ),
        CheckConstraint(
            "(idempotency_key IS NULL AND request_digest IS NULL) OR "
            "(idempotency_key IS NOT NULL AND length(idempotency_key) >= 1 "
            "AND request_digest IS NOT NULL "
            "AND length(request_digest) = 64)",
            name="ck_guideline_revision_idempotency_shape",
        ),
        CheckConstraint(
            "(revision_number = 1 AND parent_revision_id IS NULL) OR "
            "(revision_number > 1 AND parent_revision_id IS NOT NULL)",
            name="ck_guideline_revision_parent_shape",
        ),
        ForeignKeyConstraint(
            ["guideline_id", "parent_revision_id"],
            ["guideline_revisions.guideline_id", "guideline_revisions.revision_id"],
            name="fk_guideline_revision_parent",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "ix_guideline_revisions_guideline_created",
            "guideline_id",
            "created_at",
            "revision_id",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    guideline_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "guidelines.id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    tags: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    rules: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        server_default=func.now(),
    )
    # Materialized result of the successful CAS.  It lets an idempotent replay
    # return the exact original head even after later revisions advance the
    # mutable guideline_heads pointer.
    published_head_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    published_head_updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    parent_revision_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )
    # Honest legacy bridge: one 1.0.0 baseline captures the observed row.
    # A counter above one is retained but never expanded into invented history.
    legacy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    legacy_version_unresolvable: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    legacy_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Exact textual provenance for legacy v1 imports. ``legacy_version`` stays
    # integer-compatible for the historical B03 bridge; values such as
    # ``"draft"`` or ``"1.5"`` remain lossless here.
    legacy_version_text: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )


class GuidelineHeadRow(Base):
    """Mutable compare-and-swap pointer to a guideline's current revision."""

    __tablename__ = "guideline_heads"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "guideline_id",
                "revision_id",
                "revision_number",
                "semantic_version",
            ],
            [
                "guideline_revisions.guideline_id",
                "guideline_revisions.revision_id",
                "guideline_revisions.revision_number",
                "guideline_revisions.semantic_version",
            ],
            name="fk_guideline_head_exact_revision",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "revision_number >= 1",
            name="ck_guideline_head_positive_revision_number",
        ),
        CheckConstraint(
            "head_revision >= 1",
            name="ck_guideline_head_positive_head_revision",
        ),
    )

    guideline_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    head_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        server_default=func.now(),
    )


class GuidelineRevisionNoopReplayRow(Base):
    """Immutable idempotency ledger for revision commands that changed nothing."""

    __tablename__ = "guideline_revision_noop_replays"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "guideline_id",
                "revision_id",
                "revision_number",
                "semantic_version",
            ],
            [
                "guideline_revisions.guideline_id",
                "guideline_revisions.revision_id",
                "guideline_revisions.revision_number",
                "guideline_revisions.semantic_version",
            ],
            name="fk_guideline_revision_noop_exact_revision",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "revision_number >= 1 AND original_head_revision >= 1",
            name="ck_guideline_revision_noop_positive_revisions",
        ),
        CheckConstraint(
            "length(idempotency_key) >= 1 AND length(request_digest) = 64",
            name="ck_guideline_revision_noop_idempotency_shape",
        ),
        Index(
            "ix_guideline_revision_noop_revision",
            "guideline_id",
            "revision_id",
        ),
    )

    guideline_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    original_head_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    original_head_updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        server_default=func.now(),
    )


class GuidelineBoardBindingRow(Base):
    """Append-only board adoption of one exact guideline revision.

    ``binding_id`` is the stable adoption identity.  Re-adoption appends a row
    with the same identity and the next ``binding_revision``; it never mutates
    the prior pin.
    """

    __tablename__ = "guideline_board_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "guideline_id",
                "revision_id",
                "semantic_version",
                "revision_digest",
            ],
            [
                "guideline_revisions.guideline_id",
                "guideline_revisions.revision_id",
                "guideline_revisions.semantic_version",
                "guideline_revisions.content_digest",
            ],
            name="fk_guideline_binding_exact_source_revision",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "board_id",
            "guideline_id",
            "binding_revision",
            name="uq_guideline_board_binding_revision",
        ),
        Index(
            "uq_guideline_binding_exact_authority",
            "binding_id",
            "binding_revision",
            "board_id",
            "guideline_id",
            "revision_id",
            unique=True,
        ),
        UniqueConstraint(
            "board_id",
            "guideline_id",
            "idempotency_key",
            name="uq_guideline_binding_idempotency",
        ),
        UniqueConstraint(
            "legacy_source_id",
            name="uq_guideline_binding_legacy_source",
        ),
        CheckConstraint(
            "binding_revision >= 1",
            name="ck_guideline_binding_positive_revision",
        ),
        CheckConstraint(
            "priority >= 0",
            name="ck_guideline_binding_non_negative_priority",
        ),
        CheckConstraint(
            "length(revision_digest) = 64",
            name="ck_guideline_binding_digest_length",
        ),
        CheckConstraint(
            "(idempotency_key IS NULL AND request_digest IS NULL) OR "
            "(idempotency_key IS NOT NULL AND length(idempotency_key) >= 1 "
            "AND request_digest IS NOT NULL "
            "AND length(request_digest) = 64)",
            name="ck_guideline_binding_idempotency_shape",
        ),
        CheckConstraint(
            "enforcement IN ('advisory', 'blocking')",
            name="ck_guideline_binding_enforcement",
        ),
        CheckConstraint(
            "state IN ('active', 'unlinked')",
            name="ck_guideline_binding_state",
        ),
        CheckConstraint(
            "source_kind IN "
            "('native', 'legacy_board_guideline', 'legacy_inline_guideline')",
            name="ck_guideline_binding_source_kind",
        ),
        CheckConstraint(
            "binding_origin IN ('native', 'default_materialization')",
            name="ck_guideline_binding_origin",
        ),
        Index(
            "ix_guideline_board_bindings_current",
            "board_id",
            "guideline_id",
            "binding_revision",
        ),
    )

    binding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    binding_revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    guideline_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("guidelines.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    adopted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    adopted_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        server_default=func.now(),
    )
    enforcement: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="advisory",
        server_default=text("'advisory'"),
    )
    source_kind: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="native",
        server_default=text("'native'"),
    )
    legacy_source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    legacy_guideline_version: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    legacy_template_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    legacy_template_version: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    legacy_version_unresolvable: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Kept last deliberately: B04 upgrades append this column to the exact B03
    # SQLite table, and the strict owned-schema audit includes column order.
    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
        server_default=text("'active'"),
    )
    # Present only for an explicit global adoption.  Inline/default/legacy
    # materialization has its own structurally verified provenance.
    impact_receipt_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "guideline_impact_receipts.impact_receipt_id",
            name="fk_guideline_binding_impact_receipt",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=True,
    )
    binding_origin: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="native",
        server_default=text("'native'"),
    )
    impact_adoption_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "guideline_impact_adoptions.adoption_id",
            name="fk_guideline_binding_impact_adoption",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    impact_unlink_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "guideline_impact_unlinks.unlink_id",
            name="fk_guideline_binding_impact_unlink",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )


class GuidelineImportBindingCandidateRow(Base):
    """Inert, re-exportable binding history from ``guideline-export/v2``.

    Imported bindings are evidence, never board-policy authority.  Activating
    a candidate must go through the ordinary impact-preview and adoption path,
    which writes ``guideline_board_bindings`` together with its sealed ledgers.
    This table therefore preserves the source package exactly while remaining
    outside every effective-binding query.
    """

    __tablename__ = "guideline_import_binding_candidates"
    __table_args__ = (
        UniqueConstraint(
            "target_board_id",
            "guideline_id",
            "source_binding_id",
            "source_binding_revision",
            name="uq_guideline_import_binding_candidate_stable",
        ),
        ForeignKeyConstraint(
            [
                "guideline_id",
                "resolved_revision_id",
                "revision_digest",
            ],
            [
                "semantic_guideline_revisions.guideline_id",
                "semantic_guideline_revisions.revision_id",
                "semantic_guideline_revisions.revision_digest",
            ],
            name="fk_guideline_import_candidate_exact_semantic_revision",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "length(package_digest) = 64 "
            "AND length(import_digest) = 64 "
            "AND length(revision_digest) = 64 "
            "AND length(source_payload_digest) = 64",
            name="ck_guideline_import_candidate_digests",
        ),
        CheckConstraint(
            "source_binding_revision >= 1",
            name="ck_guideline_import_candidate_positive_revision",
        ),
        CheckConstraint(
            "source_binding_state IN ('active', 'unlinked')",
            name="ck_guideline_import_candidate_state",
        ),
        CheckConstraint(
            "source_enforcement IN ('advisory', 'blocking')",
            name="ck_guideline_import_candidate_enforcement",
        ),
        CheckConstraint(
            "disposition IN ('store_inert_history', 'pending_adoption')",
            name="ck_guideline_import_candidate_disposition",
        ),
        Index(
            "ix_guideline_import_candidates_target",
            "target_board_id",
            "guideline_id",
            "disposition",
            "candidate_id",
        ),
    )

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    import_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    resolved_revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_binding_state: Mapped[str] = mapped_column(String(20), nullable=False)
    source_enforcement: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    source_payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    imported_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class GuidelineImpactReceiptRow(Base):
    """Sealed immutable header for one persisted board impact preview."""

    __tablename__ = "guideline_impact_receipts"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_guideline_impact_board_idempotency",
        ),
        UniqueConstraint(
            "impact_receipt_id",
            "board_id",
            "guideline_id",
            name="uq_guideline_impact_scope",
        ),
        ForeignKeyConstraint(
            [
                "guideline_id",
                "to_revision_id",
                "to_revision_digest",
            ],
            [
                "semantic_guideline_revisions.guideline_id",
                "semantic_guideline_revisions.revision_id",
                "semantic_guideline_revisions.revision_digest",
            ],
            name="fk_guideline_impact_to_semantic_revision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "guideline_id",
                "from_revision_id",
                "from_revision_digest",
            ],
            [
                "semantic_guideline_revisions.guideline_id",
                "semantic_guideline_revisions.revision_id",
                "semantic_guideline_revisions.revision_digest",
            ],
            name="fk_guideline_impact_from_semantic_revision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "(from_revision_id IS NULL "
            "AND from_semantic_version IS NULL "
            "AND from_revision_digest IS NULL "
            "AND expected_binding_revision IS NULL "
            "AND expected_binding_state IS NULL) OR "
            "(from_revision_id IS NOT NULL "
            "AND from_semantic_version IS NOT NULL "
            "AND from_revision_digest IS NOT NULL "
            "AND length(from_revision_digest) = 64 "
            "AND expected_binding_revision IS NOT NULL "
            "AND expected_binding_revision >= 1 "
            "AND expected_binding_state IS NOT NULL "
            "AND expected_binding_state IN ('active', 'unlinked'))",
            name="ck_guideline_impact_source_fence_shape",
        ),
        CheckConstraint(
            "to_revision_number >= 1 AND expected_head_revision >= 1",
            name="ck_guideline_impact_positive_revisions",
        ),
        CheckConstraint(
            "proposed_priority >= 0 "
            "AND proposed_minimum_confidence >= 0 "
            "AND proposed_minimum_confidence <= 100 "
            "AND item_count >= 0",
            name="ck_guideline_impact_non_negative_counts",
        ),
        CheckConstraint(
            "proposed_enforcement IN ('advisory', 'blocking')",
            name="ck_guideline_impact_enforcement",
        ),
        CheckConstraint(
            "requires_explicit_adoption = true",
            name="ck_guideline_impact_explicit_adoption",
        ),
        CheckConstraint(
            "length(binding_digest) = 64 "
            "AND length(binding_head_digest_before) = 64 "
            "AND length(binding_head_digest_after) = 64 "
            "AND length(policy_set_digest_before) = 64 "
            "AND length(policy_set_digest_after) = 64 "
            "AND length(artifact_snapshot_digest) = 64 "
            "AND length(waiver_snapshot_digest) = 64 "
            "AND length(impact_digest) = 64 "
            "AND length(request_digest) = 64",
            name="ck_guideline_impact_digest_lengths",
        ),
        Index(
            "ix_guideline_impact_receipts_board_created",
            "board_id",
            "created_at",
            "impact_receipt_id",
        ),
        Index(
            "ix_guideline_impact_receipts_guideline_created",
            "board_id",
            "guideline_id",
            "created_at",
            "impact_receipt_id",
        ),
    )

    impact_receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    guideline_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("guidelines.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    from_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    from_semantic_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    from_revision_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    to_revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    to_semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    to_revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_head_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_binding_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    expected_binding_state: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_head_digest_before: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_head_digest_after: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_set_digest_before: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_set_digest_after: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    waiver_snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_enforcement: Mapped[str] = mapped_column(String(20), nullable=False)
    proposed_minimum_confidence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    proposed_metric_threshold_overrides: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
    )
    affected_entity_types: Mapped[list] = mapped_column(JSON, nullable=False)
    added_metric_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    changed_metric_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    removed_metric_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    impact_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    requires_explicit_adoption: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    sealed: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=text("false"),
    )


class GuidelineImpactItemRow(Base):
    """Immutable child item ordered by policy-keyset/v1."""

    __tablename__ = "guideline_impact_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["impact_receipt_id", "board_id", "guideline_id"],
            [
                "guideline_impact_receipts.impact_receipt_id",
                "guideline_impact_receipts.board_id",
                "guideline_impact_receipts.guideline_id",
            ],
            name="fk_guideline_impact_item_receipt",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "item_kind IN ('binding', 'target', 'artifact', 'waiver')",
            name="ck_guideline_impact_item_kind",
        ),
        CheckConstraint(
            "entity_version IS NULL OR entity_version >= 0",
            name="ck_guideline_impact_item_version",
        ),
        CheckConstraint(
            "length(details_digest) = 64",
            name="ck_guideline_impact_item_digest",
        ),
        Index(
            "ix_guideline_impact_items_keyset",
            "impact_receipt_id",
            "entity_type",
            "entity_id",
            "impact_item_id",
        ),
    )

    impact_receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    impact_item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    item_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    related_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    entity_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GuidelineImpactAdoptionRow(Base):
    """Immutable operation ledger joining receipt, binding, event and Activity."""

    __tablename__ = "guideline_impact_adoptions"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_guideline_impact_adoption_idempotency",
        ),
        UniqueConstraint(
            "impact_receipt_id",
            name="uq_guideline_impact_adoption_receipt",
        ),
        UniqueConstraint(
            "binding_id",
            "binding_revision",
            name="uq_guideline_impact_adoption_binding",
        ),
        ForeignKeyConstraint(
            ["impact_receipt_id", "board_id", "guideline_id"],
            [
                "guideline_impact_receipts.impact_receipt_id",
                "guideline_impact_receipts.board_id",
                "guideline_impact_receipts.guideline_id",
            ],
            name="fk_guideline_impact_adoption_receipt",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["binding_id", "binding_revision"],
            [
                "guideline_board_bindings.binding_id",
                "guideline_board_bindings.binding_revision",
            ],
            name="fk_guideline_impact_adoption_binding",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "length(impact_digest) = 64 "
            "AND length(binding_digest) = 64 "
            "AND length(request_digest) = 64 "
            "AND length(adoption_digest) = 64",
            name="ck_guideline_impact_adoption_digests",
        ),
    )

    adoption_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    impact_receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_binding_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    impact_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    adopted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    adopted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("domain_events.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    activity_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("activity_logs.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    adoption_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GuidelineImpactUnlinkRow(Base):
    """Immutable operation ledger for one explicit binding unlink."""

    __tablename__ = "guideline_impact_unlinks"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_guideline_impact_unlink_idempotency",
        ),
        UniqueConstraint(
            "binding_id",
            "binding_revision",
            name="uq_guideline_impact_unlink_binding",
        ),
        ForeignKeyConstraint(
            ["binding_id", "binding_revision"],
            [
                "guideline_board_bindings.binding_id",
                "guideline_board_bindings.binding_revision",
            ],
            name="fk_guideline_impact_unlink_binding",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "previous_binding_revision >= 1 "
            "AND binding_revision = previous_binding_revision + 1",
            name="ck_guideline_impact_unlink_sequence",
        ),
        CheckConstraint(
            "actor_type IN ('agent', 'user', 'system')",
            name="ck_guideline_impact_unlink_actor_type",
        ),
        CheckConstraint(
            "length(binding_digest_before) = 64 "
            "AND length(binding_head_digest_before) = 64 "
            "AND length(binding_head_digest_after) = 64 "
            "AND length(policy_set_digest_before) = 64 "
            "AND length(policy_set_digest_after) = 64 "
            "AND length(request_digest) = 64 "
            "AND length(unlink_digest) = 64",
            name="ck_guideline_impact_unlink_digests",
        ),
    )

    unlink_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    guideline_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "guidelines.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_binding_revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    binding_digest_before: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    binding_head_digest_before: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    binding_head_digest_after: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    policy_set_digest_before: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    policy_set_digest_after: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    removed_metric_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    unlinked_by: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    unlinked_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("domain_events.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    activity_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("activity_logs.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    request_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    unlink_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )


class GuidelineRetirementImpactRow(Base):
    """Immutable per-board retirement lineage for later KG tombstones."""

    __tablename__ = "guideline_retirement_impacts"
    __table_args__ = (
        UniqueConstraint(
            "retirement_id",
            "board_id",
            name="uq_guideline_retirement_impact_board",
        ),
        UniqueConstraint(
            "binding_id",
            "binding_revision",
            name="uq_guideline_retirement_impact_binding",
        ),
        ForeignKeyConstraint(
            ["binding_id", "binding_revision"],
            [
                "guideline_board_bindings.binding_id",
                "guideline_board_bindings.binding_revision",
            ],
            name="fk_guideline_retirement_impact_binding",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "retirement_status IN ('retired', 'superseded')",
            name="ck_guideline_retirement_impact_status",
        ),
        CheckConstraint(
            "(retirement_status = 'retired' "
            "AND superseded_by_guideline_id IS NULL) OR "
            "(retirement_status = 'superseded' "
            "AND superseded_by_guideline_id IS NOT NULL "
            "AND superseded_by_guideline_id <> guideline_id)",
            name="ck_guideline_retirement_impact_successor",
        ),
        CheckConstraint(
            "binding_revision >= 1 AND revision_number >= 1",
            name="ck_guideline_retirement_impact_versions",
        ),
        CheckConstraint(
            "actor_type IN ('agent', 'user', 'system')",
            name="ck_guideline_retirement_impact_actor_type",
        ),
        CheckConstraint(
            "length(revision_digest) = 64 "
            "AND length(binding_digest_before) = 64 "
            "AND length(binding_head_digest_before) = 64 "
            "AND length(binding_head_digest_after) = 64 "
            "AND length(policy_set_digest_before) = 64 "
            "AND length(policy_set_digest_after) = 64 "
            "AND length(request_digest) = 64 "
            "AND length(impact_digest) = 64",
            name="ck_guideline_retirement_impact_digests",
        ),
    )

    impact_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    retirement_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "guideline_retirements.retirement_id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    guideline_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("guidelines.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    retirement_status: Mapped[str] = mapped_column(String(20), nullable=False)
    superseded_by_guideline_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("guidelines.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=True,
    )
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_digest_before: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    binding_head_digest_before: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    binding_head_digest_after: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    policy_set_digest_before: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    policy_set_digest_after: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    removed_metric_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    retired_by: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    retired_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("domain_events.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    activity_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("activity_logs.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    impact_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GuidelineRetirementRow(Base):
    """Immutable terminal tombstone for one guideline aggregate.

    Retirement and supersedence preserve the stable identity, every immutable
    revision, and every board-binding revision.  Ordinary lifecycle operations
    may only append this row; right-to-erasure may physically remove the row
    for a board-inline guideline while the scoped erasure permit is present.
    """

    __tablename__ = "guideline_retirements"
    __table_args__ = (
        UniqueConstraint(
            "guideline_id",
            name="uq_guideline_retirement_guideline",
        ),
        UniqueConstraint(
            "guideline_id",
            "idempotency_key",
            name="uq_guideline_retirement_idempotency",
        ),
        CheckConstraint(
            "status IN ('retired', 'superseded')",
            name="ck_guideline_retirement_status",
        ),
        CheckConstraint(
            "retired_revision_number >= 1 AND retired_head_revision >= 1",
            name="ck_guideline_retirement_positive_head_revision",
        ),
        CheckConstraint(
            "length(retired_revision_digest) = 64",
            name="ck_guideline_retirement_digest_length",
        ),
        CheckConstraint(
            "(status = 'retired' AND superseded_by_guideline_id IS NULL) OR "
            "(status = 'superseded' "
            "AND superseded_by_guideline_id IS NOT NULL "
            "AND superseded_by_guideline_id <> guideline_id)",
            name="ck_guideline_retirement_successor_shape",
        ),
        CheckConstraint(
            "length(reason) >= 1 AND length(retired_by) >= 1",
            name="ck_guideline_retirement_actor_reason",
        ),
        CheckConstraint(
            "(idempotency_key IS NULL AND request_digest IS NULL) OR "
            "(idempotency_key IS NOT NULL AND length(idempotency_key) >= 1 "
            "AND request_digest IS NOT NULL "
            "AND length(request_digest) = 64)",
            name="ck_guideline_retirement_idempotency_shape",
        ),
        ForeignKeyConstraint(
            [
                "guideline_id",
                "retired_revision_id",
                "retired_revision_number",
                "retired_semantic_version",
            ],
            [
                "guideline_revisions.guideline_id",
                "guideline_revisions.revision_id",
                "guideline_revisions.revision_number",
                "guideline_revisions.semantic_version",
            ],
            name="fk_guideline_retirement_exact_revision_number",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "guideline_id",
                "retired_revision_id",
                "retired_revision_digest",
            ],
            [
                "semantic_guideline_revisions.guideline_id",
                "semantic_guideline_revisions.revision_id",
                "semantic_guideline_revisions.revision_digest",
            ],
            name="fk_guideline_retirement_exact_semantic_revision_digest",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "ix_guideline_retirements_status_time",
            "status",
            "retired_at",
            "retirement_id",
        ),
    )

    retirement_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    guideline_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "guidelines.id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    retired_revision_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
    )
    retired_revision_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    retired_semantic_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    retired_revision_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    retired_head_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    retired_by: Mapped[str] = mapped_column(String(255), nullable=False)
    retired_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        server_default=func.now(),
    )
    superseded_by_guideline_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "guidelines.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PolicyComplianceReceiptRow(Base):
    """Immutable aggregate evidence produced by ``policy-compliance/v1``."""

    __tablename__ = "policy_compliance_receipts"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_id",
            name="uq_policy_compliance_receipt_evaluation",
        ),
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_policy_compliance_receipt_idempotency",
        ),
        UniqueConstraint(
            "receipt_id",
            "board_id",
            "entity_type",
            "subject_id",
            "subject_version",
            name="uq_policy_compliance_receipt_subject_snapshot",
        ),
        CheckConstraint(
            "subject_version >= 1",
            name="ck_policy_compliance_receipt_subject_version",
        ),
        CheckConstraint(
            "entity_type IN "
            "('ideation', 'refinement', 'spec', 'card', 'sprint', "
            "'test_scenario')",
            name="ck_policy_compliance_receipt_entity_type",
        ),
        CheckConstraint(
            "outcome IN ('pass', 'fail', 'not_applicable', 'error')",
            name="ck_policy_compliance_receipt_outcome",
        ),
        CheckConstraint(
            "state IN ('ready', 'blocked', 'ready_with_waivers', 'not_applicable')",
            name="ck_policy_compliance_receipt_state",
        ),
        CheckConstraint(
            "recorded_currentness IN ('current', 'stale')",
            name="ck_policy_compliance_receipt_currentness",
        ),
        CheckConstraint(
            "length(subject_content_digest) = 64 "
            "AND length(input_digest) = 64 "
            "AND length(policy_set_digest) = 64 "
            "AND length(binding_head_digest) = 64 "
            "AND length(receipt_digest) = 64 "
            "AND length(request_digest) = 64",
            name="ck_policy_compliance_receipt_digests",
        ),
        CheckConstraint(
            "rule_count >= 0 AND failed_rule_count >= 0 "
            "AND error_rule_count >= 0 "
            "AND failed_rule_count + error_rule_count <= rule_count "
            "AND finding_count >= 0 AND blocking_finding_count >= 0 "
            "AND waived_finding_count >= 0 "
            "AND blocking_finding_count <= finding_count "
            "AND waived_finding_count <= finding_count",
            name="ck_policy_compliance_receipt_counts",
        ),
        Index(
            "ix_policy_compliance_receipts_board_time",
            "board_id",
            "evaluated_at",
            "receipt_id",
        ),
        Index(
            "ix_policy_compliance_receipts_subject_time",
            "board_id",
            "entity_type",
            "subject_id",
            "evaluated_at",
            "receipt_id",
        ),
        Index(
            "ix_policy_compliance_receipts_outcome_time",
            "board_id",
            "outcome",
            "evaluated_at",
            "receipt_id",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_content_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_set_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_head_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    catalog_version: Mapped[str] = mapped_column(String(128), nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    recorded_currentness: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )
    rule_results: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    reason_codes: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    evaluator_version: Mapped[str] = mapped_column(String(128), nullable=False)
    evaluated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_rule_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_rule_count: Mapped[int] = mapped_column(Integer, nullable=False)
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    blocking_finding_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    waived_finding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    sealed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )


class PolicyComplianceAdoptedRevisionRow(Base):
    """Normalized immutable pin of every revision used by a receipt."""

    __tablename__ = "policy_compliance_adopted_revisions"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id",
            "binding_id",
            name="uq_policy_compliance_adopted_binding",
        ),
        UniqueConstraint(
            "receipt_id",
            "guideline_id",
            "revision_id",
            name="uq_policy_compliance_adopted_revision",
        ),
        ForeignKeyConstraint(
            ["binding_id", "binding_revision"],
            [
                "guideline_board_bindings.binding_id",
                "guideline_board_bindings.binding_revision",
            ],
            name="fk_policy_compliance_adopted_binding",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "guideline_id",
                "revision_id",
                "semantic_version",
                "revision_digest",
            ],
            [
                "guideline_revisions.guideline_id",
                "guideline_revisions.revision_id",
                "guideline_revisions.semantic_version",
                "guideline_revisions.content_digest",
            ],
            name="fk_policy_compliance_adopted_exact_revision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "binding_revision >= 1 AND length(revision_digest) = 64",
            name="ck_policy_compliance_adopted_snapshot",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "policy_compliance_receipts.receipt_id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        primary_key=True,
    )
    guideline_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class PolicyComplianceFindingRow(Base):
    """Immutable per-rule finding attached to one exact receipt snapshot."""

    __tablename__ = "policy_compliance_findings"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "receipt_id",
                "board_id",
                "entity_type",
                "subject_id",
                "subject_version",
            ],
            [
                "policy_compliance_receipts.receipt_id",
                "policy_compliance_receipts.board_id",
                "policy_compliance_receipts.entity_type",
                "policy_compliance_receipts.subject_id",
                "policy_compliance_receipts.subject_version",
            ],
            name="fk_policy_compliance_finding_subject_snapshot",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["receipt_id", "guideline_id", "revision_id"],
            [
                "policy_compliance_adopted_revisions.receipt_id",
                "policy_compliance_adopted_revisions.guideline_id",
                "policy_compliance_adopted_revisions.revision_id",
            ],
            name="fk_policy_compliance_finding_adopted_revision",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "receipt_id",
            "guideline_id",
            "rule_id",
            name="uq_policy_compliance_finding_rule",
        ),
        CheckConstraint(
            "subject_version >= 1 AND severity_rank >= 0",
            name="ck_policy_compliance_finding_rank_version",
        ),
        CheckConstraint(
            "outcome IN ('fail', 'error') AND enforcement IN ('advisory', 'blocking')",
            name="ck_policy_compliance_finding_enums",
        ),
        CheckConstraint(
            "waiver_id IS NULL OR outcome = 'fail'",
            name="ck_policy_compliance_finding_waiver",
        ),
        Index(
            "ix_policy_compliance_findings_board_severity",
            "board_id",
            "severity_rank",
            "rule_id",
            "finding_id",
        ),
        Index(
            "ix_policy_compliance_findings_receipt_severity",
            "board_id",
            "receipt_id",
            "severity_rank",
            "rule_id",
            "finding_id",
        ),
        Index(
            "ix_policy_compliance_findings_subject_severity",
            "board_id",
            "subject_id",
            "severity_rank",
            "rule_id",
            "finding_id",
        ),
        Index(
            "ix_policy_compliance_findings_rule_severity",
            "board_id",
            "guideline_id",
            "rule_id",
            "severity_rank",
            "finding_id",
        ),
    )

    finding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    enforcement: Mapped[str] = mapped_column(String(20), nullable=False)
    severity_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    waiver_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )


class PolicyWaiverRow(Base):
    """Exact immutable source facts plus CAS-derived current waiver head."""

    __tablename__ = "policy_waivers"
    __table_args__ = (
        UniqueConstraint(
            "waiver_id",
            "board_id",
            name="uq_policy_waiver_board_identity",
        ),
        UniqueConstraint(
            "last_event_id",
            name="uq_policy_waiver_last_event",
        ),
        ForeignKeyConstraint(
            [
                "last_event_id",
                "waiver_id",
                "board_id",
                "waiver_revision",
            ],
            [
                "policy_waiver_events.event_id",
                "policy_waiver_events.waiver_id",
                "policy_waiver_events.board_id",
                "policy_waiver_events.waiver_revision",
            ],
            name="fk_policy_waiver_head_last_event",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "subject_version >= 1 AND length(scope_digest) = 64 "
            "AND length(request_digest) = 64 "
            "AND length(head_digest) = 64 "
            "AND waiver_revision >= 1",
            name="ck_policy_waiver_request_shape",
        ),
        CheckConstraint(
            "entity_type IN "
            "('ideation', 'refinement', 'spec', 'card', 'sprint', "
            "'test_scenario')",
            name="ck_policy_waiver_entity_type",
        ),
        CheckConstraint(
            "status IN "
            "('requested', 'approved', 'rejected', 'revoked', 'expired') "
            "AND last_event_type IN "
            "('request', 'approve', 'reject', 'revoke', 'expire', "
            "'revalidate')",
            name="ck_policy_waiver_head_enums",
        ),
        CheckConstraint(
            "(status = 'requested' AND reviewed_by IS NULL "
            "AND reviewed_at IS NULL AND review_reason IS NULL) "
            "OR (status <> 'requested' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL "
            "AND reviewed_by <> requested_by)",
            name="ck_policy_waiver_head_review",
        ),
        CheckConstraint(
            "(status = 'revoked' AND revoked_by IS NOT NULL "
            "AND revoked_at IS NOT NULL) "
            "OR (status <> 'revoked' AND revoked_by IS NULL "
            "AND revoked_at IS NULL)",
            name="ck_policy_waiver_head_revocation",
        ),
        CheckConstraint(
            "(status = 'expired' AND expire_reason_code IN "
            "('scheduled_expiry', 'subject_scope_changed', "
            "'guideline_revision_changed', 'guideline_rule_changed')) "
            "OR (status <> 'expired' AND expire_reason_code IS NULL)",
            name="ck_policy_waiver_head_expire_reason",
        ),
        Index(
            "ix_policy_waivers_board_requested",
            "board_id",
            "requested_at",
            "waiver_id",
        ),
        Index(
            "ix_policy_waivers_exact_scope",
            "board_id",
            "guideline_id",
            "revision_id",
            "rule_id",
            "entity_type",
            "subject_id",
            "subject_version",
        ),
        Index(
            "ix_policy_waivers_board_status_requested",
            "board_id",
            "status",
            "requested_at",
            "waiver_id",
        ),
        Index(
            "ix_policy_waivers_active_scope",
            "board_id",
            "guideline_id",
            "revision_id",
            "rule_id",
            "entity_type",
            "subject_id",
            "subject_version",
            "status",
            "expires_at",
        ),
    )

    waiver_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    finding_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "policy_compliance_findings.finding_id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "policy_compliance_receipts.receipt_id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    original_expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    waiver_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    last_event_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    expire_reason_code: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    head_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class PolicyWaiverEventRow(Base):
    """Append-only waiver event and complete derived head at that revision."""

    __tablename__ = "policy_waiver_events"
    __table_args__ = (
        UniqueConstraint(
            "waiver_id",
            "waiver_revision",
            name="uq_policy_waiver_event_revision",
        ),
        UniqueConstraint(
            "event_id",
            "waiver_id",
            "board_id",
            "waiver_revision",
            name="uq_policy_waiver_event_head_identity",
        ),
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_policy_waiver_event_idempotency",
        ),
        UniqueConstraint(
            "predecessor_event_id",
            name="uq_policy_waiver_event_predecessor",
        ),
        ForeignKeyConstraint(
            ["waiver_id", "board_id"],
            ["policy_waivers.waiver_id", "policy_waivers.board_id"],
            name="fk_policy_waiver_event_lineage",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["predecessor_event_id"],
            ["policy_waiver_events.event_id"],
            name="fk_policy_waiver_event_predecessor",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "waiver_revision >= 1 AND length(scope_digest) = 64 "
            "AND length(waiver_digest) = 64 "
            "AND length(request_digest) = 64",
            name="ck_policy_waiver_event_shape",
        ),
        CheckConstraint(
            "event_type IN "
            "('request', 'approve', 'reject', 'revoke', 'expire', "
            "'revalidate') "
            "AND to_status IN "
            "('requested', 'approved', 'rejected', 'revoked', 'expired') "
            "AND (from_status IS NULL OR from_status IN "
            "('requested', 'approved', 'rejected', 'revoked', 'expired'))",
            name="ck_policy_waiver_event_enums",
        ),
        CheckConstraint(
            "(event_type = 'request' AND from_status IS NULL "
            "AND to_status = 'requested' AND waiver_revision = 1) "
            "OR (event_type = 'approve' AND from_status = 'requested' "
            "AND to_status = 'approved' AND waiver_revision > 1) "
            "OR (event_type = 'reject' AND from_status = 'requested' "
            "AND to_status = 'rejected' AND waiver_revision > 1) "
            "OR (event_type = 'revoke' AND from_status = 'approved' "
            "AND to_status = 'revoked' AND waiver_revision > 1) "
            "OR (event_type = 'expire' AND from_status = 'approved' "
            "AND to_status = 'expired' AND waiver_revision > 1) "
            "OR (event_type = 'revalidate' "
            "AND from_status IN ('approved', 'expired') "
            "AND to_status = 'approved' AND waiver_revision > 1)",
            name="ck_policy_waiver_event_transition",
        ),
        CheckConstraint(
            "(event_type = 'expire' "
            "AND expire_reason_code IN "
            "('scheduled_expiry', 'subject_scope_changed', "
            "'guideline_revision_changed', 'guideline_rule_changed')) "
            "OR (event_type <> 'expire' AND expire_reason_code IS NULL)",
            name="ck_policy_waiver_event_expire_reason",
        ),
        CheckConstraint(
            "(event_type = 'expire' "
            "AND expire_reason_code = 'scheduled_expiry' "
            "AND occurred_at >= expires_at) "
            "OR (event_type = 'expire' "
            "AND expire_reason_code <> 'scheduled_expiry' "
            "AND occurred_at < expires_at) "
            "OR (event_type IN "
            "('request', 'approve', 'revoke', 'revalidate') "
            "AND occurred_at < expires_at) "
            "OR event_type = 'reject'",
            name="ck_policy_waiver_event_expiry",
        ),
        CheckConstraint(
            "(to_status = 'requested' AND reviewed_by IS NULL "
            "AND reviewed_at IS NULL AND review_reason IS NULL) "
            "OR (to_status <> 'requested' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL)",
            name="ck_policy_waiver_event_review",
        ),
        CheckConstraint(
            "(to_status = 'revoked' AND revoked_by IS NOT NULL "
            "AND revoked_at IS NOT NULL) "
            "OR (to_status <> 'revoked' AND revoked_by IS NULL "
            "AND revoked_at IS NULL)",
            name="ck_policy_waiver_event_revocation",
        ),
        Index(
            "ix_policy_waiver_events_board_time",
            "board_id",
            "occurred_at",
            "event_id",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    predecessor_event_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    waiver_id: Mapped[str] = mapped_column(String(64), nullable=False)
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    waiver_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    waiver_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    expire_reason_code: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)


# ============================================================================
# SEMANTIC GUIDELINE GOVERNANCE (SK-B3)
# ============================================================================


class SemanticGuidelineRevisionRow(Base):
    """Append-only semantic authority layered over one immutable revision.

    The unreleased ``policy/v1`` payload remains on ``guideline_revisions`` as
    inert audit evidence.  This row is the only executable semantic authority:
    it stores the ordered metric definitions and their v3 digest without
    pretending that legacy predicates were equivalent metrics.
    """

    __tablename__ = "semantic_guideline_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guideline_id", "revision_id"],
            [
                "guideline_revisions.guideline_id",
                "guideline_revisions.revision_id",
            ],
            name="fk_sg_revision_legacy_revision",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "guideline_id",
            "revision_id",
            "revision_digest",
            name="uq_sg_revision_digest",
        ),
        CheckConstraint(
            "length(revision_digest) = 64 AND length(source_revision_digest) = 64",
            name="ck_sg_revision_digests",
        ),
        CheckConstraint(
            "contract_version = 'guideline-revision-digest/v2'",
            name="ck_sg_revision_contract",
        ),
        CheckConstraint(
            "authority_state IN "
            "('native', 'legacy_context_only', 'legacy_incompatible')",
            name="ck_sg_revision_authority_state",
        ),
        CheckConstraint(
            "(authority_state = 'native' AND legacy_rules_digest IS NULL) "
            "OR (authority_state <> 'native' "
            "AND legacy_rules_digest IS NOT NULL "
            "AND length(legacy_rules_digest) = 64)",
            name="ck_sg_revision_legacy_digest",
        ),
        Index(
            "ix_sg_revision_guideline_created",
            "guideline_id",
            "created_at",
            "revision_id",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    contract_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="guideline-revision-digest/v2",
        server_default=text("'guideline-revision-digest/v2'"),
    )
    # Metric order is semantic and therefore preserved exactly.
    metrics: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Digest of the historical GuidelineRevisionRow snapshot used as the
    # migration source fence.  It is never reused as semantic authority.
    source_revision_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    authority_state: Mapped[str] = mapped_column(String(32), nullable=False)
    legacy_rules_digest: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SemanticGuidelineBindingConfigurationRow(Base):
    """Append-only board configuration for one exact semantic revision."""

    __tablename__ = "semantic_guideline_binding_configurations"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "binding_id",
                "binding_revision",
                "board_id",
                "guideline_id",
                "revision_id",
            ],
            [
                "guideline_board_bindings.binding_id",
                "guideline_board_bindings.binding_revision",
                "guideline_board_bindings.board_id",
                "guideline_board_bindings.guideline_id",
                "guideline_board_bindings.revision_id",
            ],
            name="fk_sg_binding_exact_legacy_authority",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["guideline_id", "revision_id", "revision_digest"],
            [
                "semantic_guideline_revisions.guideline_id",
                "semantic_guideline_revisions.revision_id",
                "semantic_guideline_revisions.revision_digest",
            ],
            name="fk_sg_binding_revision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "board_id",
            "guideline_id",
            "binding_revision",
            name="uq_sg_binding_board_revision",
        ),
        UniqueConstraint(
            "binding_id",
            "binding_revision",
            "board_id",
            "guideline_id",
            "revision_id",
            "revision_digest",
            "configuration_digest",
            name="uq_sg_binding_exact_configuration",
        ),
        CheckConstraint(
            "binding_revision >= 1 "
            "AND minimum_confidence >= 0 "
            "AND minimum_confidence <= 100",
            name="ck_sg_binding_ranges",
        ),
        CheckConstraint(
            "enforcement IN ('advisory', 'blocking')",
            name="ck_sg_binding_enforcement",
        ),
        CheckConstraint(
            "length(revision_digest) = 64 AND length(configuration_digest) = 64",
            name="ck_sg_binding_digests",
        ),
        Index(
            "ix_sg_binding_board_guideline",
            "board_id",
            "guideline_id",
            "binding_revision",
        ),
    )

    binding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    binding_revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    enforcement: Mapped[str] = mapped_column(String(20), nullable=False)
    minimum_confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    # Canonical map keyed by stable metric.code, never metric_id.
    metric_threshold_overrides: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    configuration_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    configured_by: Mapped[str] = mapped_column(String(255), nullable=False)
    configured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SemanticSubjectVersionEventRow(Base):
    """Append-only subject-version/editor evidence used for separation."""

    __tablename__ = "semantic_subject_version_events"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "subject_type",
            "subject_id",
            "head_revision",
            name="uq_sem_subject_event_revision",
        ),
        UniqueConstraint(
            "event_id",
            "board_id",
            "subject_type",
            "subject_id",
            "head_revision",
            name="uq_sem_subject_event_head",
        ),
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_sem_subject_event_idempotency",
        ),
        UniqueConstraint(
            "predecessor_event_id",
            name="uq_sem_subject_event_predecessor",
        ),
        ForeignKeyConstraint(
            ["predecessor_event_id"],
            ["semantic_subject_version_events.event_id"],
            name="fk_sem_subject_event_predecessor",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "subject_type IN "
            "('ideation', 'refinement', 'spec', 'card', 'sprint', "
            "'test_scenario')",
            name="ck_sem_subject_event_type",
        ),
        CheckConstraint(
            "subject_version >= 1 AND head_revision >= 1",
            name="ck_sem_subject_event_versions",
        ),
        CheckConstraint(
            "editor_source IN ('authoritative', 'legacy_unknown')",
            name="ck_sem_subject_event_editor_source",
        ),
        CheckConstraint(
            "(editor_source = 'legacy_unknown' "
            "AND last_semantic_editor_id = 'legacy_unknown') "
            "OR (editor_source = 'authoritative' "
            "AND last_semantic_editor_id <> 'legacy_unknown')",
            name="ck_sem_subject_event_editor",
        ),
        CheckConstraint(
            "event_type IN ('semantic_mutation', 'legacy_bootstrap')",
            name="ck_sem_subject_event_kind",
        ),
        CheckConstraint(
            "length(content_digest) = 64 AND length(request_digest) = 64",
            name="ck_sem_subject_event_digests",
        ),
        Index(
            "ix_sem_subject_event_subject",
            "board_id",
            "subject_type",
            "subject_id",
            "head_revision",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    predecessor_event_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    last_semantic_editor_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    editor_source: Mapped[str] = mapped_column(String(24), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    head_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class SemanticSubjectVersionRow(Base):
    """CAS-protected current semantic subject/editor fence."""

    __tablename__ = "semantic_subject_versions"
    __table_args__ = (
        UniqueConstraint(
            "last_event_id",
            name="uq_sem_subject_head_event",
        ),
        ForeignKeyConstraint(
            [
                "last_event_id",
                "board_id",
                "subject_type",
                "subject_id",
                "head_revision",
            ],
            [
                "semantic_subject_version_events.event_id",
                "semantic_subject_version_events.board_id",
                "semantic_subject_version_events.subject_type",
                "semantic_subject_version_events.subject_id",
                "semantic_subject_version_events.head_revision",
            ],
            name="fk_sem_subject_head_event",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "subject_type IN "
            "('ideation', 'refinement', 'spec', 'card', 'sprint', "
            "'test_scenario')",
            name="ck_sem_subject_head_type",
        ),
        CheckConstraint(
            "subject_version >= 1 AND head_revision >= 1",
            name="ck_sem_subject_head_versions",
        ),
        CheckConstraint(
            "editor_source IN ('authoritative', 'legacy_unknown')",
            name="ck_sem_subject_head_editor_source",
        ),
        CheckConstraint(
            "(editor_source = 'legacy_unknown' "
            "AND last_semantic_editor_id = 'legacy_unknown') "
            "OR (editor_source = 'authoritative' "
            "AND last_semantic_editor_id <> 'legacy_unknown')",
            name="ck_sem_subject_head_editor",
        ),
        CheckConstraint(
            "length(content_digest) = 64",
            name="ck_sem_subject_head_digest",
        ),
    )

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        primary_key=True,
    )
    subject_type: Mapped[str] = mapped_column(String(24), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    last_semantic_editor_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    editor_source: Mapped[str] = mapped_column(String(24), nullable=False)
    head_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    last_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SemanticGuidelineValidationScopeRow(Base):
    """Immutable policy-governance scope pinned to a lifecycle edition."""

    __tablename__ = "semantic_guideline_validation_scopes"
    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('ideation', 'refinement', 'spec')",
            name="ck_sg_validation_scope_subject_type",
        ),
        CheckConstraint(
            "validation_edition >= 1",
            name="ck_sg_validation_scope_edition",
        ),
        CheckConstraint(
            "length(policy_set_digest) = 64 AND length(binding_head_digest) = 64",
            name="ck_sg_validation_scope_digests",
        ),
        Index(
            "ix_sg_validation_scope_subject",
            "board_id",
            "subject_type",
            "subject_id",
            "validation_edition",
        ),
    )

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        primary_key=True,
    )
    subject_type: Mapped[str] = mapped_column(String(24), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    validation_edition: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Ordered exact binding/revision identities. Historical binding and
    # revision rows are immutable authority and can be reconstructed lazily.
    scope_json: Mapped[list] = mapped_column(JSON, nullable=False)
    policy_set_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_head_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SemanticGuidelineAssessmentReceiptRow(Base):
    """Immutable aggregate assessment evidence, sealed only after all results."""

    __tablename__ = "semantic_guideline_assessment_receipts"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_sg_assessment_idempotency",
        ),
        UniqueConstraint(
            "receipt_id",
            "board_id",
            "subject_type",
            "subject_id",
            "subject_version",
            name="uq_sg_assessment_subject",
        ),
        UniqueConstraint(
            "receipt_id",
            "board_id",
            "subject_type",
            "subject_id",
            "subject_version",
            "subject_content_digest",
            "receipt_digest",
            "guideline_id",
            "revision_id",
            "revision_digest",
            "binding_id",
            "binding_revision",
            "configuration_digest",
            name="uq_sg_assessment_exact",
        ),
        UniqueConstraint(
            "receipt_id",
            "receipt_digest",
            name="uq_sg_assessment_receipt_digest",
        ),
        ForeignKeyConstraint(
            [
                "binding_id",
                "binding_revision",
                "board_id",
                "guideline_id",
                "revision_id",
                "revision_digest",
                "configuration_digest",
            ],
            [
                "semantic_guideline_binding_configurations.binding_id",
                "semantic_guideline_binding_configurations.binding_revision",
                "semantic_guideline_binding_configurations.board_id",
                "semantic_guideline_binding_configurations.guideline_id",
                "semantic_guideline_binding_configurations.revision_id",
                "semantic_guideline_binding_configurations.revision_digest",
                "semantic_guideline_binding_configurations.configuration_digest",
            ],
            name="fk_sg_assessment_binding",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["guideline_id", "revision_id", "revision_digest"],
            [
                "semantic_guideline_revisions.guideline_id",
                "semantic_guideline_revisions.revision_id",
                "semantic_guideline_revisions.revision_digest",
            ],
            name="fk_sg_assessment_revision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "subject_type IN "
            "('ideation', 'refinement', 'spec', 'card', 'sprint', "
            "'test_scenario')",
            name="ck_sg_assessment_subject_type",
        ),
        CheckConstraint(
            "subject_version >= 1 "
            "AND binding_revision >= 1 "
            "AND confidence >= 0 AND confidence <= 100 "
            "AND minimum_confidence >= 0 AND minimum_confidence <= 100",
            name="ck_sg_assessment_ranges",
        ),
        CheckConstraint(
            "validation_edition IS NULL OR validation_edition >= 1",
            name="ck_sg_assessment_validation_edition",
        ),
        CheckConstraint(
            "enforcement IN ('advisory', 'blocking') "
            "AND state IN ('passed', 'metric_threshold_failed') "
            "AND recorded_currentness = 'current'",
            name="ck_sg_assessment_enums",
        ),
        CheckConstraint(
            "confidence >= minimum_confidence AND confidence_admissible",
            name="ck_sg_assessment_confidence",
        ),
        CheckConstraint(
            "metric_result_count >= 0 "
            "AND failed_metric_count >= 0 "
            "AND failed_metric_count <= metric_result_count",
            name="ck_sg_assessment_counts",
        ),
        CheckConstraint(
            "(state = 'passed' AND confidence_admissible "
            "AND failed_metric_count = 0) "
            "OR (state = 'metric_threshold_failed' "
            "AND confidence_admissible AND failed_metric_count > 0)",
            name="ck_sg_assessment_outcome",
        ),
        CheckConstraint(
            "(enforcement = 'advisory') OR "
            "(last_semantic_editor_id <> 'legacy_unknown' "
            "AND assessor_independent)",
            name="ck_sg_assessment_separation",
        ),
        CheckConstraint(
            "(assessor_agent_id <> last_semantic_editor_id "
            "AND assessor_independent) "
            "OR (assessor_agent_id = last_semantic_editor_id "
            "AND NOT assessor_independent)",
            name="ck_sg_assessment_independence",
        ),
        CheckConstraint(
            "length(subject_content_digest) = 64 "
            "AND length(revision_digest) = 64 "
            "AND length(configuration_digest) = 64 "
            "AND length(policy_set_digest) = 64 "
            "AND length(binding_head_digest) = 64 "
            "AND length(input_digest) = 64 "
            "AND length(receipt_digest) = 64 "
            "AND length(request_digest) = 64",
            name="ck_sg_assessment_digests",
        ),
        Index(
            "ix_sg_assessment_subject_time",
            "board_id",
            "subject_type",
            "subject_id",
            "assessed_at",
            "receipt_id",
        ),
        Index(
            "ix_sg_assessment_subject_edition_time",
            "board_id",
            "subject_type",
            "subject_id",
            "validation_edition",
            "assessed_at",
            "receipt_id",
        ),
        Index(
            "ix_sg_assessment_binding_time",
            "board_id",
            "binding_id",
            "assessed_at",
            "receipt_id",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Only Spec assessments use lifecycle validation editions.  NULL remains
    # valid for other subject types and for legacy evidence.
    validation_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subject_content_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    last_semantic_editor_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_set_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_head_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    enforcement: Mapped[str] = mapped_column(String(20), nullable=False)
    minimum_confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_admissible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    assessor_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    assessor_model_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    assessor_independent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    recorded_currentness: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="current",
        server_default=text("'current'"),
    )
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_metric_count: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    sealed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )


class SemanticGuidelineMetricResultRow(Base):
    """One lossless, immutable result for every metric in an assessment."""

    __tablename__ = "semantic_guideline_metric_results"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "receipt_id",
                "board_id",
                "subject_type",
                "subject_id",
                "subject_version",
                "subject_content_digest",
                "receipt_digest",
                "guideline_id",
                "revision_id",
                "revision_digest",
                "binding_id",
                "binding_revision",
                "configuration_digest",
            ],
            [
                "semantic_guideline_assessment_receipts.receipt_id",
                "semantic_guideline_assessment_receipts.board_id",
                "semantic_guideline_assessment_receipts.subject_type",
                "semantic_guideline_assessment_receipts.subject_id",
                "semantic_guideline_assessment_receipts.subject_version",
                "semantic_guideline_assessment_receipts.subject_content_digest",
                "semantic_guideline_assessment_receipts.receipt_digest",
                "semantic_guideline_assessment_receipts.guideline_id",
                "semantic_guideline_assessment_receipts.revision_id",
                "semantic_guideline_assessment_receipts.revision_digest",
                "semantic_guideline_assessment_receipts.binding_id",
                "semantic_guideline_assessment_receipts.binding_revision",
                "semantic_guideline_assessment_receipts.configuration_digest",
            ],
            name="fk_sg_metric_result_receipt",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "receipt_id",
            "metric_id",
            name="uq_sg_metric_result_metric",
        ),
        UniqueConstraint(
            "receipt_id",
            "metric_code",
            name="uq_sg_metric_result_code",
        ),
        UniqueConstraint(
            "result_id",
            "receipt_id",
            "board_id",
            "subject_type",
            "subject_id",
            "subject_version",
            "subject_content_digest",
            "receipt_digest",
            "guideline_id",
            "revision_id",
            "revision_digest",
            "binding_id",
            "binding_revision",
            "configuration_digest",
            "metric_id",
            "result_digest",
            name="uq_sg_metric_result_exact",
        ),
        CheckConstraint(
            "subject_version >= 1 "
            "AND score >= 0 AND score <= 100 "
            "AND default_threshold >= 0 AND default_threshold <= 100 "
            "AND effective_threshold >= 0 "
            "AND effective_threshold <= 100",
            name="ck_sg_metric_result_ranges",
        ),
        CheckConstraint(
            "direction IN ('minimum', 'maximum') "
            "AND threshold_source IN ('default', 'override') "
            "AND outcome IN ('pass', 'fail')",
            name="ck_sg_metric_result_enums",
        ),
        CheckConstraint(
            "(direction = 'minimum' "
            "AND ((score >= effective_threshold AND outcome = 'pass') "
            "OR (score < effective_threshold AND outcome = 'fail'))) "
            "OR (direction = 'maximum' "
            "AND ((score <= effective_threshold AND outcome = 'pass') "
            "OR (score > effective_threshold AND outcome = 'fail')))",
            name="ck_sg_metric_result_outcome",
        ),
        CheckConstraint(
            "length(configuration_digest) = 64 "
            "AND length(subject_content_digest) = 64 "
            "AND length(receipt_digest) = 64 "
            "AND length(revision_digest) = 64 "
            "AND length(metric_definition_digest) = 64 "
            "AND length(result_digest) = 64",
            name="ck_sg_metric_result_digests",
        ),
        Index(
            "ix_sg_metric_result_subject",
            "board_id",
            "subject_type",
            "subject_id",
            "outcome",
            "metric_code",
        ),
    )

    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_content_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_code: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_definition_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    default_threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold_source: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    pinpoints: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SemanticGuidelineFindingRow(Base):
    """One immutable, addressable finding for every failed metric result."""

    __tablename__ = "semantic_guideline_findings"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "metric_result_id",
                "receipt_id",
                "board_id",
                "subject_type",
                "subject_id",
                "subject_version",
                "subject_content_digest",
                "receipt_digest",
                "guideline_id",
                "revision_id",
                "revision_digest",
                "binding_id",
                "binding_revision",
                "configuration_digest",
                "metric_id",
                "metric_result_digest",
            ],
            [
                "semantic_guideline_metric_results.result_id",
                "semantic_guideline_metric_results.receipt_id",
                "semantic_guideline_metric_results.board_id",
                "semantic_guideline_metric_results.subject_type",
                "semantic_guideline_metric_results.subject_id",
                "semantic_guideline_metric_results.subject_version",
                "semantic_guideline_metric_results.subject_content_digest",
                "semantic_guideline_metric_results.receipt_digest",
                "semantic_guideline_metric_results.guideline_id",
                "semantic_guideline_metric_results.revision_id",
                "semantic_guideline_metric_results.revision_digest",
                "semantic_guideline_metric_results.binding_id",
                "semantic_guideline_metric_results.binding_revision",
                "semantic_guideline_metric_results.configuration_digest",
                "semantic_guideline_metric_results.metric_id",
                "semantic_guideline_metric_results.result_digest",
            ],
            name="fk_sg_finding_metric_result",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "metric_result_id",
            name="uq_sg_finding_metric_result",
        ),
        UniqueConstraint(
            "finding_id",
            "metric_result_id",
            "receipt_id",
            "board_id",
            "subject_type",
            "subject_id",
            "subject_version",
            "subject_content_digest",
            "receipt_digest",
            "guideline_id",
            "revision_id",
            "revision_digest",
            "binding_id",
            "binding_revision",
            "configuration_digest",
            "metric_id",
            "metric_result_digest",
            "finding_digest",
            name="uq_sg_finding_exact",
        ),
        CheckConstraint(
            "subject_version >= 1 AND binding_revision >= 1",
            name="ck_sg_finding_versions",
        ),
        CheckConstraint(
            "length(metric_result_digest) = 64 "
            "AND length(finding_digest) = 64 "
            "AND length(subject_content_digest) = 64 "
            "AND length(receipt_digest) = 64 "
            "AND length(revision_digest) = 64 "
            "AND length(configuration_digest) = 64 "
            "AND length(trim(rationale)) > 0",
            name="ck_sg_finding_shape",
        ),
        Index(
            "ix_sg_finding_queue",
            "board_id",
            "subject_type",
            "subject_id",
            "binding_id",
            "metric_id",
            "created_at",
            "finding_id",
        ),
    )

    finding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    metric_result_id: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_content_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_code: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    pinpoints: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    finding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SemanticGuidelineWaiverRow(Base):
    """Exact semantic metric-result exception with a CAS-derived head."""

    __tablename__ = "semantic_guideline_waivers"
    __table_args__ = (
        UniqueConstraint(
            "waiver_id",
            "board_id",
            name="uq_sg_waiver_board",
        ),
        UniqueConstraint("last_event_id", name="uq_sg_waiver_last_event"),
        ForeignKeyConstraint(
            [
                "metric_result_id",
                "receipt_id",
                "board_id",
                "subject_type",
                "subject_id",
                "subject_version",
                "subject_content_digest",
                "receipt_digest",
                "guideline_id",
                "revision_id",
                "revision_digest",
                "binding_id",
                "binding_revision",
                "configuration_digest",
                "metric_id",
                "metric_result_digest",
            ],
            [
                "semantic_guideline_metric_results.result_id",
                "semantic_guideline_metric_results.receipt_id",
                "semantic_guideline_metric_results.board_id",
                "semantic_guideline_metric_results.subject_type",
                "semantic_guideline_metric_results.subject_id",
                "semantic_guideline_metric_results.subject_version",
                "semantic_guideline_metric_results.subject_content_digest",
                "semantic_guideline_metric_results.receipt_digest",
                "semantic_guideline_metric_results.guideline_id",
                "semantic_guideline_metric_results.revision_id",
                "semantic_guideline_metric_results.revision_digest",
                "semantic_guideline_metric_results.binding_id",
                "semantic_guideline_metric_results.binding_revision",
                "semantic_guideline_metric_results.configuration_digest",
                "semantic_guideline_metric_results.metric_id",
                "semantic_guideline_metric_results.result_digest",
            ],
            name="fk_sg_waiver_metric_result",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "finding_id",
                "metric_result_id",
                "receipt_id",
                "board_id",
                "subject_type",
                "subject_id",
                "subject_version",
                "subject_content_digest",
                "receipt_digest",
                "guideline_id",
                "revision_id",
                "revision_digest",
                "binding_id",
                "binding_revision",
                "configuration_digest",
                "metric_id",
                "metric_result_digest",
                "finding_digest",
            ],
            [
                "semantic_guideline_findings.finding_id",
                "semantic_guideline_findings.metric_result_id",
                "semantic_guideline_findings.receipt_id",
                "semantic_guideline_findings.board_id",
                "semantic_guideline_findings.subject_type",
                "semantic_guideline_findings.subject_id",
                "semantic_guideline_findings.subject_version",
                "semantic_guideline_findings.subject_content_digest",
                "semantic_guideline_findings.receipt_digest",
                "semantic_guideline_findings.guideline_id",
                "semantic_guideline_findings.revision_id",
                "semantic_guideline_findings.revision_digest",
                "semantic_guideline_findings.binding_id",
                "semantic_guideline_findings.binding_revision",
                "semantic_guideline_findings.configuration_digest",
                "semantic_guideline_findings.metric_id",
                "semantic_guideline_findings.metric_result_digest",
                "semantic_guideline_findings.finding_digest",
            ],
            name="fk_sg_waiver_finding",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "last_event_id",
                "waiver_id",
                "board_id",
                "waiver_revision",
            ],
            [
                "semantic_guideline_waiver_events.event_id",
                "semantic_guideline_waiver_events.waiver_id",
                "semantic_guideline_waiver_events.board_id",
                "semantic_guideline_waiver_events.waiver_revision",
            ],
            name="fk_sg_waiver_last_event",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "subject_version >= 1 AND binding_revision >= 1 AND waiver_revision >= 1",
            name="ck_sg_waiver_versions",
        ),
        CheckConstraint(
            "validation_edition IS NULL OR validation_edition >= 1",
            name="ck_sg_waiver_validation_edition",
        ),
        CheckConstraint(
            "subject_type IN "
            "('ideation', 'refinement', 'spec', 'card', 'sprint', "
            "'test_scenario')",
            name="ck_sg_waiver_subject_type",
        ),
        CheckConstraint(
            "status IN "
            "('requested', 'approved', 'rejected', 'revoked', 'expired') "
            "AND last_event_type IN "
            "('request', 'approve', 'reject', 'revoke', 'expire', "
            "'revalidate')",
            name="ck_sg_waiver_enums",
        ),
        CheckConstraint(
            "(status = 'requested' AND reviewed_by IS NULL "
            "AND reviewed_at IS NULL AND review_reason IS NULL) "
            "OR (status <> 'requested' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL "
            "AND reviewed_by <> requested_by)",
            name="ck_sg_waiver_review",
        ),
        CheckConstraint(
            "(status = 'revoked' AND revoked_by IS NOT NULL "
            "AND revoked_at IS NOT NULL) "
            "OR (status <> 'revoked' AND revoked_by IS NULL "
            "AND revoked_at IS NULL)",
            name="ck_sg_waiver_revocation",
        ),
        CheckConstraint(
            "(status = 'expired' AND expire_reason_code IN "
            "('scheduled_expiry', 'subject_scope_changed', "
            "'guideline_revision_changed', "
            "'binding_configuration_changed', 'metric_result_changed')) "
            "OR (status <> 'expired' AND expire_reason_code IS NULL)",
            name="ck_sg_waiver_expire_reason",
        ),
        CheckConstraint(
            "("
            "last_revalidation_status IS NULL "
            "AND last_revalidation_current IS NULL "
            "AND last_revalidation_reason_code IS NULL "
            "AND last_revalidation_evaluated_at IS NULL "
            "AND last_revalidation_scheduled_expiry_observed = false"
            ") OR ("
            "last_revalidation_status IN "
            "('approved', 'expired', 'anchor_stale', 'revoked') "
            "AND last_revalidation_current IS NOT NULL "
            "AND last_revalidation_reason_code IN "
            "('current', 'scheduled_expiry', 'anchor_missing', "
            "'subject_scope_changed', 'guideline_revision_changed', "
            "'binding_configuration_changed', 'metric_result_changed', "
            "'revoked') "
            "AND last_revalidation_evaluated_at IS NOT NULL"
            ")",
            name="ck_sg_waiver_revalidation_shape",
        ),
        CheckConstraint(
            "last_revalidation_status IS NULL "
            "OR (last_revalidation_status = 'approved' "
            "AND last_revalidation_current "
            "AND last_revalidation_reason_code = 'current') "
            "OR (last_revalidation_status = 'expired' "
            "AND NOT last_revalidation_current "
            "AND last_revalidation_reason_code IN "
            "('scheduled_expiry', 'subject_scope_changed', "
            "'guideline_revision_changed', "
            "'binding_configuration_changed', 'metric_result_changed')) "
            "OR (last_revalidation_status = 'anchor_stale' "
            "AND NOT last_revalidation_current "
            "AND last_revalidation_reason_code IN "
            "('anchor_missing', 'subject_scope_changed', "
            "'guideline_revision_changed', "
            "'binding_configuration_changed', 'metric_result_changed')) "
            "OR (last_revalidation_status = 'revoked' "
            "AND NOT last_revalidation_current "
            "AND last_revalidation_reason_code = 'revoked')",
            name="ck_sg_waiver_revalidation_decision",
        ),
        CheckConstraint(
            "length(subject_content_digest) = 64 "
            "AND length(revision_digest) = 64 "
            "AND length(configuration_digest) = 64 "
            "AND length(metric_result_digest) = 64 "
            "AND length(finding_digest) = 64 "
            "AND length(receipt_digest) = 64 "
            "AND length(scope_digest) = 64 "
            "AND length(head_digest) = 64 "
            "AND length(request_digest) = 64",
            name="ck_sg_waiver_digests",
        ),
        Index(
            "ix_sg_waiver_scope",
            "board_id",
            "binding_id",
            "metric_id",
            "subject_type",
            "subject_id",
            "subject_version",
            "status",
        ),
        Index(
            "ix_sg_waiver_edition_scope",
            "board_id",
            "subject_type",
            "subject_id",
            "validation_edition",
            "status",
        ),
    )

    waiver_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    metric_result_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    finding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "semantic_guideline_assessment_receipts.receipt_id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subject_content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_code: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    finding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    original_expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    waiver_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    last_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    last_event_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    expire_reason_code: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    head_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Append-only v3 additions.  Keep this physical order aligned with the
    # predecessor migration's PostgreSQL ``ALTER TABLE ADD COLUMN`` sequence.
    assessment_assessor_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    last_event_idempotency_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    last_revalidation_status: Mapped[str | None] = mapped_column(
        String(24),
        nullable=True,
    )
    last_revalidation_current: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )
    last_revalidation_reason_code: Mapped[str | None] = mapped_column(
        String(48),
        nullable=True,
    )
    last_revalidation_evaluated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    last_revalidation_currentness_reasons: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    last_revalidation_scheduled_expiry_observed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )


class SemanticGuidelineWaiverEventRow(Base):
    """Append-only lifecycle event containing the complete semantic waiver head."""

    __tablename__ = "semantic_guideline_waiver_events"
    __table_args__ = (
        UniqueConstraint(
            "waiver_id",
            "waiver_revision",
            name="uq_sg_waiver_event_revision",
        ),
        UniqueConstraint(
            "event_id",
            "waiver_id",
            "board_id",
            "waiver_revision",
            name="uq_sg_waiver_event_head",
        ),
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_sg_waiver_event_idempotency",
        ),
        UniqueConstraint(
            "predecessor_event_id",
            name="uq_sg_waiver_event_predecessor",
        ),
        ForeignKeyConstraint(
            ["waiver_id", "board_id"],
            [
                "semantic_guideline_waivers.waiver_id",
                "semantic_guideline_waivers.board_id",
            ],
            name="fk_sg_waiver_event_lineage",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["predecessor_event_id"],
            ["semantic_guideline_waiver_events.event_id"],
            name="fk_sg_waiver_event_predecessor",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "waiver_revision >= 1 "
            "AND length(scope_digest) = 64 "
            "AND length(waiver_digest) = 64 "
            "AND length(request_digest) = 64",
            name="ck_sg_waiver_event_shape",
        ),
        CheckConstraint(
            "validation_edition IS NULL OR validation_edition >= 1",
            name="ck_sg_waiver_event_validation_edition",
        ),
        CheckConstraint(
            "event_type IN "
            "('request', 'approve', 'reject', 'revoke', 'expire', "
            "'revalidate') "
            "AND to_status IN "
            "('requested', 'approved', 'rejected', 'revoked', 'expired') "
            "AND (from_status IS NULL OR from_status IN "
            "('requested', 'approved', 'rejected', 'revoked', 'expired'))",
            name="ck_sg_waiver_event_enums",
        ),
        CheckConstraint(
            "(event_type = 'request' AND from_status IS NULL "
            "AND to_status = 'requested' AND waiver_revision = 1) "
            "OR (event_type = 'approve' AND from_status = 'requested' "
            "AND to_status = 'approved' AND waiver_revision > 1) "
            "OR (event_type = 'reject' AND from_status = 'requested' "
            "AND to_status = 'rejected' AND waiver_revision > 1) "
            "OR (event_type = 'revoke' AND from_status = 'approved' "
            "AND to_status = 'revoked' AND waiver_revision > 1) "
            "OR (event_type = 'expire' AND from_status = 'approved' "
            "AND to_status = 'expired' AND waiver_revision > 1) "
            "OR (event_type = 'revalidate' "
            "AND from_status IN ('approved', 'expired', 'revoked') "
            "AND (to_status = from_status OR to_status = 'expired') "
            "AND NOT (from_status = 'expired' AND to_status <> 'expired') "
            "AND NOT (from_status = 'revoked' AND to_status <> 'revoked') "
            "AND waiver_revision > 1)",
            name="ck_sg_waiver_event_transition",
        ),
        CheckConstraint(
            "((event_type = 'expire' OR "
            "(event_type = 'revalidate' AND to_status = 'expired')) "
            "AND expire_reason_code IN "
            "('scheduled_expiry', 'subject_scope_changed', "
            "'guideline_revision_changed', "
            "'binding_configuration_changed', 'metric_result_changed')) "
            "OR ((event_type <> 'expire' AND NOT "
            "(event_type = 'revalidate' AND to_status = 'expired')) "
            "AND expire_reason_code IS NULL)",
            name="ck_sg_waiver_event_expire",
        ),
        CheckConstraint(
            "(event_type <> 'revalidate' AND "
            "revalidation_status IS NULL "
            "AND revalidation_current IS NULL "
            "AND revalidation_reason_code IS NULL "
            "AND evaluated_at IS NULL "
            "AND scheduled_expiry_observed = false"
            ") OR (event_type = 'revalidate' AND "
            "revalidation_status IN "
            "('approved', 'expired', 'anchor_stale', 'revoked') "
            "AND revalidation_current IS NOT NULL "
            "AND revalidation_reason_code IN "
            "('current', 'scheduled_expiry', 'anchor_missing', "
            "'subject_scope_changed', 'guideline_revision_changed', "
            "'binding_configuration_changed', 'metric_result_changed', "
            "'revoked') "
            "AND evaluated_at IS NOT NULL"
            ")",
            name="ck_sg_waiver_event_revalidation_shape",
        ),
        CheckConstraint(
            "revalidation_status IS NULL "
            "OR (revalidation_status = 'approved' "
            "AND revalidation_current "
            "AND revalidation_reason_code = 'current') "
            "OR (revalidation_status = 'expired' "
            "AND NOT revalidation_current "
            "AND revalidation_reason_code IN "
            "('scheduled_expiry', 'subject_scope_changed', "
            "'guideline_revision_changed', "
            "'binding_configuration_changed', 'metric_result_changed')) "
            "OR (revalidation_status = 'anchor_stale' "
            "AND NOT revalidation_current "
            "AND revalidation_reason_code IN "
            "('anchor_missing', 'subject_scope_changed', "
            "'guideline_revision_changed', "
            "'binding_configuration_changed', 'metric_result_changed')) "
            "OR (revalidation_status = 'revoked' "
            "AND NOT revalidation_current "
            "AND revalidation_reason_code = 'revoked')",
            name="ck_sg_waiver_event_revalidation_decision",
        ),
        Index(
            "ix_sg_waiver_event_time",
            "board_id",
            "occurred_at",
            "event_id",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    predecessor_event_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    waiver_id: Mapped[str] = mapped_column(String(64), nullable=False)
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    validation_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    waiver_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    waiver_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    expire_reason_code: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Append-only v3 additions.  Keep this physical order aligned with the
    # predecessor migration's PostgreSQL ``ALTER TABLE ADD COLUMN`` sequence.
    evaluated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    revalidation_status: Mapped[str | None] = mapped_column(
        String(24),
        nullable=True,
    )
    revalidation_current: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )
    revalidation_reason_code: Mapped[str | None] = mapped_column(
        String(48),
        nullable=True,
    )
    currentness_reasons: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    scheduled_expiry_observed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )


class SemanticGuidelineSkipRow(Base):
    """Append-only human skip lifecycle bound to exact semantic fences."""

    __tablename__ = "semantic_guideline_skips"
    __table_args__ = (
        UniqueConstraint(
            "skip_id",
            "skip_revision",
            name="uq_sg_skip_revision",
        ),
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_sg_skip_idempotency",
        ),
        UniqueConstraint(
            "predecessor_event_id",
            name="uq_sg_skip_predecessor",
        ),
        ForeignKeyConstraint(
            ["predecessor_event_id"],
            ["semantic_guideline_skips.event_id"],
            name="fk_sg_skip_predecessor",
            ondelete="CASCADE",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "binding_id",
                "binding_revision",
                "board_id",
                "guideline_id",
                "revision_id",
                "revision_digest",
                "configuration_digest",
            ],
            [
                "semantic_guideline_binding_configurations.binding_id",
                "semantic_guideline_binding_configurations.binding_revision",
                "semantic_guideline_binding_configurations.board_id",
                "semantic_guideline_binding_configurations.guideline_id",
                "semantic_guideline_binding_configurations.revision_id",
                "semantic_guideline_binding_configurations.revision_digest",
                "semantic_guideline_binding_configurations.configuration_digest",
            ],
            name="fk_sg_skip_binding",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["guideline_id", "revision_id", "revision_digest"],
            [
                "semantic_guideline_revisions.guideline_id",
                "semantic_guideline_revisions.revision_id",
                "semantic_guideline_revisions.revision_digest",
            ],
            name="fk_sg_skip_revision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "subject_type IN "
            "('ideation', 'refinement', 'spec', 'card', 'sprint', "
            "'test_scenario')",
            name="ck_sg_skip_subject_type",
        ),
        CheckConstraint(
            "subject_version >= 1 AND binding_revision >= 1 AND skip_revision >= 1",
            name="ck_sg_skip_versions",
        ),
        CheckConstraint(
            "validation_edition IS NULL OR validation_edition >= 1",
            name="ck_sg_skip_validation_edition",
        ),
        CheckConstraint(
            "event_type IN ('create', 'revoke') "
            "AND status IN ('active', 'revoked') "
            "AND actor_kind = 'human'",
            name="ck_sg_skip_enums",
        ),
        CheckConstraint(
            "(event_type = 'create' AND status = 'active' "
            "AND from_status IS NULL AND skip_revision = 1 "
            "AND predecessor_event_id IS NULL "
            "AND revoked_by IS NULL AND revoked_at IS NULL "
            "AND revocation_reason IS NULL) "
            "OR (event_type = 'revoke' AND status = 'revoked' "
            "AND from_status = 'active' AND skip_revision > 1 "
            "AND predecessor_event_id IS NOT NULL "
            "AND revoked_by IS NOT NULL AND revoked_at IS NOT NULL "
            "AND revocation_reason IS NOT NULL)",
            name="ck_sg_skip_transition",
        ),
        CheckConstraint(
            "length(subject_content_digest) = 64 "
            "AND length(revision_digest) = 64 "
            "AND length(configuration_digest) = 64 "
            "AND length(scope_digest) = 64 "
            "AND length(event_id) = 64 "
            "AND length(skip_digest) = 64 "
            "AND length(request_digest) = 64 "
            "AND length(trim(reason)) > 0 "
            "AND length(trim(actor_id)) > 0",
            name="ck_sg_skip_digests",
        ),
        Index(
            "ix_sg_skip_scope",
            "board_id",
            "binding_id",
            "subject_type",
            "subject_id",
            "subject_version",
            "status",
            "skip_revision",
        ),
        Index(
            "ix_sg_skip_edition_scope",
            "board_id",
            "binding_id",
            "subject_type",
            "subject_id",
            "validation_edition",
            "status",
            "skip_revision",
        ),
        Index(
            "ix_sg_skip_lifecycle",
            "board_id",
            "skip_id",
            "skip_revision",
            "event_id",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    predecessor_event_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    skip_id: Mapped[str] = mapped_column(String(64), nullable=False)
    skip_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subject_content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="human",
        server_default=text("'human'"),
    )
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    skip_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class SemanticGuidelineLegacyMigrationRow(Base):
    """Append-only audit of the non-semantic policy/v1 retirement migration."""

    __tablename__ = "semantic_guideline_legacy_migrations"
    __table_args__ = (
        UniqueConstraint(
            "source_type",
            "source_id",
            name="uq_sg_legacy_migration_source",
        ),
        CheckConstraint(
            "source_type IN ('revision', 'binding', 'receipt', 'waiver')",
            name="ck_sg_legacy_migration_source_type",
        ),
        CheckConstraint(
            "migration_state IN "
            "('context_only', 'legacy_incompatible', 'inert_binding', "
            "'stale_receipt', 'ineffective_waiver')",
            name="ck_sg_legacy_migration_state",
        ),
        CheckConstraint(
            "length(source_digest) = 64",
            name="ck_sg_legacy_migration_digest",
        ),
        Index(
            "ix_sg_legacy_migration_board",
            "board_id",
            "source_type",
            "migrated_at",
            "migration_id",
        ),
    )

    migration_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    board_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    guideline_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    migration_state: Mapped[str] = mapped_column(String(32), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    migrated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class CardRejectedLifecycleMigrationRow(Base):
    """Append-only audit of legacy Validation -> Rejected convergence.

    The source digest freezes the evidence inspected by the migration.  A
    repeated startup must reproduce the same decision, while any subsequent
    human-authored validation produces a distinct audit fact instead of
    rewriting history.
    """

    __tablename__ = "card_rejected_lifecycle_migrations"
    __table_args__ = (
        UniqueConstraint(
            "card_id",
            "source_digest",
            name="uq_card_rejected_migration_source",
        ),
        CheckConstraint(
            "migration_state IN "
            "('migrated', 'already_rejected', 'not_rejected', "
            "'ambiguous_evidence', 'excluded_test', 'quarantined')",
            name="ck_card_rejected_migration_state",
        ),
        CheckConstraint(
            "length(source_digest) = 64",
            name="ck_card_rejected_migration_digest",
        ),
        Index(
            "ix_card_rejected_migration_board",
            "board_id",
            "migration_state",
            "migrated_at",
            "migration_id",
        ),
    )

    migration_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    card_id: Mapped[str] = mapped_column(String(36), nullable=False)
    migration_state: Mapped[str] = mapped_column(String(32), nullable=False)
    latest_validation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    migrated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class DesignSystem(Base):
    """Reusable Design System — a global catalog entry or a board-inline artifact
    (spec 3a006f65 / card 1392f59d). Versioned catalog row: ``version`` bumps on a
    title/payload change (including inline) so the mockup gate can compare a stable
    persisted version/snapshot. Inline systems require ``board_id`` and are never
    eligible as a global default."""

    __tablename__ = "design_systems"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'global'")
    )
    board_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'active'")
    )
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BoardDesignSystem(Base):
    """The single effective Design System linked to a board (spec 3a006f65 / card
    1392f59d). One row per board (UniqueConstraint) — singular effective cardinality;
    link/unlink upserts/deletes it. Captures ``design_system_version`` at link time so
    the gate can compare a stable identity."""

    __tablename__ = "board_design_systems"
    __table_args__ = (UniqueConstraint("board_id", name="uq_board_design_system"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    design_system_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("design_systems.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    design_system_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DesignSystemGateAudit(Base):
    """Structured, queryable audit of MockupDesignSystemGate ADVISORY outcomes (spec
    3a006f65 / card 0192f58d). Advisory persists the mockup but records a warning row
    so the gate decision is reconstituible by query (mockup_id, board_id, expected
    Design System identity + reason). Blocking failures abort the transaction and are
    surfaced as a structured error instead — no row here."""

    __tablename__ = "design_system_gate_audit"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mockup_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_design_system_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    expected_design_system_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    provided_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ActivityLog(Base):
    """Activity log for board actions."""

    __tablename__ = "activity_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    card_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # "user" or "agent"
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DefaultBoardConfiguration(Base):
    """Versioned GLOBAL template of default board configuration (spec 9df814bc /
    card d86f4f96, FR1).

    Snapshot-at-creation source: ``DefaultBoardConfigurationService`` is the single
    provider that resolves the active template and applies it to a new board's
    effective settings. The applied snapshot metadata lives on
    ``Board.default_config_snapshot`` (OUTSIDE ``Board.settings``); future template
    changes never mutate existing boards (TR5). New table — created by
    ``Base.metadata.create_all`` on init (no Alembic here).
    """

    __tablename__ = "default_board_configurations"
    __table_args__ = (
        CheckConstraint(
            "spec_checklist_mode IS NULL OR "
            "spec_checklist_mode IN ('off', 'advisory', 'blocking')",
            name="ck_default_board_config_spec_checklist_mode",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Lifecycle status (API contract): draft | active | inactive.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    is_active: Mapped[bool] = mapped_column(nullable=False, default=False, index=True)
    scope: Mapped[str] = mapped_column(
        String(50), nullable=False, default="global", index=True
    )
    # Effective default settings, validated as BoardSettings by the service (TR1).
    settings_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Guideline refs consumed by the guidelines adapter (card #3) — stored here,
    # materialized there within the same create_board transaction (TR10).
    guideline_default_refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Design System default ref consumed by the design-system adapter (card #4).
    # Shape: {design_system_id, version, snapshot, gate_mode}.
    design_system_default_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Kept outside settings_payload because the enforcement source of truth is
    # the versioned ChecklistBinding materialized for each new board. NULL
    # identifies historical template rows and resolves to Advisory.
    spec_checklist_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DefaultBoardConfigurationAudit(Base):
    """Dedicated audit trail for GLOBAL default-board-configuration template events
    (spec 9df814bc / card d86f4f96, FR9).

    Templates are global (no ``board_id``), so they cannot use the board-scoped
    ``ActivityLog``. Board-scoped events (template applied to a board, no-template
    fallback) stay in ``ActivityLog``. New table — created by ``create_all``.
    """

    __tablename__ = "default_board_configuration_audit"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    template_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Stable event types: default_board_configuration_created / _activated / _deactivated.
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str] = mapped_column(String(50), nullable=False, default="global")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AmendmentHotfixRevision(Base):
    """Path B first-class lineage artifact (spec 7ea1e4be, fr_e64e2b28).

    A persisted amendment/hotfix revision that links a bug on a done/locked spec
    to a revision spec OR regression artifact WITHOUT mutating the original spec
    (AC1). Eligibility is decided by the pure policy in
    ``core.domain.amendment_eligibility`` (status x lineage_state); this row only
    stores the durable lineage + lifecycle state + audit-relevant metadata. New
    table — created by ``Base.metadata.create_all`` on init (no Alembic here).
    """

    __tablename__ = "amendment_hotfix_revisions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The original done/locked spec the amendment corrects. Plain ref (no FK
    # cascade) so the amendment record is durable and the original spec is never
    # mutated or coupled to the amendment's lifecycle.
    original_spec_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    origin_bug_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Exact-membership lineage sets (G1: membership is exact, never loose match).
    origin_task_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    affected_task_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    revision_spec_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    regression_scenario_ids: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True
    )
    regression_test_task_ids: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True
    )
    # Automated regression artifacts (e.g. a pytest node id) — first-class so a
    # tooling/test-infra regression counts as evidence, not only a product scenario.
    automated_regression_refs: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True
    )
    status: Mapped[AmendmentRevisionStatus] = mapped_column(
        AmendmentRevisionStatusType(),
        default=AmendmentRevisionStatus.DRAFT,
        nullable=False,
    )
    lineage_state: Mapped[AmendmentLineageState] = mapped_column(
        AmendmentLineageStateType(),
        default=AmendmentLineageState.INCOMPLETE,
        nullable=False,
    )
    validation_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Knowledge Graph Foundation (MVP Fase 0)
# ---------------------------------------------------------------------------
# Four operational tables that bridge SQLite state to the per-board Kùzu
# graphs: consolidation_queue (pending triggers), consolidation_audit (session
# history + undo), kuzu_node_refs (back-references for compensating delete),
# global_update_outbox (transactional outbox for the global discovery layer).


class ConsolidationQueue(Base):
    """Pending consolidation triggers — populated by state transitions,
    consumed by the agent on-demand via the primitives MCP."""

    __tablename__ = "consolidation_queue"
    __table_args__ = (
        CheckConstraint(
            "work_kind IN ('consolidate', 'stale_reconcile', 'stale_sweep')",
            name="ck_consolidation_queue_work_kind",
        ),
        Index(
            "uq_queue_consolidate_board_artifact",
            "board_id",
            "artifact_type",
            "artifact_id",
            unique=True,
            sqlite_where=text("work_kind = 'consolidate'"),
        ),
        Index(
            "uq_queue_stale_reconcile_generation",
            "board_id",
            "artifact_type",
            "artifact_id",
            "work_kind",
            "generation",
            unique=True,
            sqlite_where=text("work_kind = 'stale_reconcile'"),
        ),
        Index(
            "uq_queue_stale_sweep_board",
            "board_id",
            "work_kind",
            unique=True,
            sqlite_where=text("work_kind = 'stale_sweep'"),
        ),
        Index(
            "ix_queue_drain_work",
            "status",
            "work_kind",
            "next_retry_at",
            "priority",
            "triggered_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    work_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="consolidate", server_default="consolidate"
    )
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    delete_event_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    priority: Mapped[str] = mapped_column(
        String(10), nullable=False, default="high"
    )  # "high" (runtime trigger) | "low" (historical backfill)
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="state_transition"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        index=True,
    )  # pending | claimed | done | paused | failed
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    triggered_by_event: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claimed_by_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Error message from failed processing

    # Spec bdcda842 (Consolidation Queue resilience) — v0.2.0 columns.
    # Added by _migrate_add_consolidation_resilience_columns; ORM model
    # mirrors the schema so newly-created rows from the model stay in sync.
    worker_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # Worker pool worker UUID that holds the claim
    claim_timeout_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Recovery scan re-pendings rows past this timestamp
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )  # Consecutive failure count for dead-letter routing
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Exp-backoff scheduling: claim ignores rows with next_retry_at > now()


class ArtifactDeletionTombstone(Base):
    """Permanent generation fence for a governed artifact deletion."""

    __tablename__ = "artifact_deletion_tombstones"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "artifact_type",
            "artifact_id",
            name="uq_artifact_deletion_tombstone_artifact",
        ),
        UniqueConstraint(
            "delete_event_id",
            name="uq_artifact_deletion_tombstone_event",
        ),
        CheckConstraint(
            "generation >= 1",
            name="ck_artifact_deletion_tombstone_generation",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    delete_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CanonicalDebt(Base):
    """Items that could not be promoted to the canonical KG yet.

    This ledger is intentionally separate from ConsolidationQueue. Queue rows
    represent executable work; canonical debt records describe why an artifact
    is not canonical and how/when an agent should retry cognitive promotion.
    """

    __tablename__ = "canonical_debt"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "artifact_type",
            "artifact_id",
            "target_status",
            "content_hash",
            name="uq_canonical_debt_artifact_target_hash",
        ),
        Index("ix_canonical_debt_board_state", "board_id", "canonical_state"),
        Index(
            "ix_canonical_debt_board_artifact",
            "board_id",
            "artifact_type",
            "artifact_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    target_status: Mapped[str] = mapped_column(String(50), nullable=False)
    canonical_state: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending", server_default="pending"
    )  # pending | retry_scheduled | deferred | failed | blocked | committed | not_applicable | superseded
    graph_layer: Mapped[str] = mapped_column(
        String(20), nullable=False, default="working", server_default="working"
    )
    maturity_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    owner_agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    queue_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dlq_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConsolidationDeadLetter(Base):
    """Dead-letter queue for items that exceeded ``kg_queue_max_attempts``
    consecutive failures. Spec bdcda842 (TR2) — items move here after the
    last failed attempt and are removed from ConsolidationQueue.

    The ``errors`` JSON array preserves the full attempt history so an
    operator can inspect what went wrong without scrubbing logs. Each entry
    follows the schema defined in TR16/AC17:
        {attempt: int (1-based),
         occurred_at: ISO8601 UTC string,
         error_type: str (exc class name),
         message: str (str(exc) truncated to 500 chars),
         traceback: str|None (traceback.format_exc() truncated to 2000 chars,
                              or null when logging level > DEBUG)}
    """

    __tablename__ = "consolidation_dead_letter"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    original_queue_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )  # The ConsolidationQueue.id the item came from (kept for traceability)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    errors: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    dead_lettered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ConsolidationAudit(Base):
    """Per-session audit trail — primary log of every consolidation commit.
    session_id is the PK because everything else (kuzu_node_refs, undo chain)
    joins back here."""

    __tablename__ = "consolidation_audit"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    nodes_added: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    nodes_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    nodes_superseded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    edges_added: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    undo_status: Mapped[str] = mapped_column(
        String(20), default="none", nullable=False
    )  # none | undone | undo_blocked
    undone_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class KuzuNodeRef(Base):
    """Back-reference from SQLite to Kùzu nodes created by a session.
    Powers compensating delete on abort and undo on demand."""

    __tablename__ = "kuzu_node_refs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("consolidation_audit.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    board_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kuzu_node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kuzu_node_type: Mapped[str] = mapped_column(String(50), nullable=False)
    operation: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # add | update | supersede
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GlobalUpdateOutbox(Base):
    """Transactional outbox for the global discovery layer sync worker.
    Events are INSERTed in the same SQLite transaction as the audit row;
    a background worker later drains them into the global Kùzu meta-graph
    with retry + dead-letter semantics."""

    __tablename__ = "global_update_outbox"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # Governed delivery attempts use the literal physical key
    # ``{delivery_key}:attempt:{n}``; UUID-sized storage would make that
    # durable idempotency identity implicit or lossy.
    event_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    board_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class GlobalDiscoveryDeliveryLedger(Base):
    """Durable ownership ledger for Global Discovery parity delivery."""

    __tablename__ = "global_discovery_delivery_ledger"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "artifact_type",
            "artifact_id",
            "generation",
            name="uq_gd_delivery_ledger_artifact_generation",
        ),
        UniqueConstraint(
            "delete_event_id",
            name="uq_gd_delivery_ledger_delete_event",
        ),
        UniqueConstraint(
            "attempt_event_key",
            name="uq_gd_delivery_ledger_attempt_event",
        ),
        CheckConstraint(
            "generation >= 1",
            name="ck_gd_delivery_ledger_generation",
        ),
        CheckConstraint(
            "state IN ('outbox_persisted', 'delivered', 'delivery_debt')",
            name="ck_gd_delivery_ledger_state",
        ),
        CheckConstraint(
            "attempt >= 0",
            name="ck_gd_delivery_ledger_attempt",
        ),
        CheckConstraint(
            "state != 'outbox_persisted' OR attempt_event_key IS NOT NULL",
            name="ck_gd_delivery_ledger_persisted_attempt_key",
        ),
        Index(
            "ix_gd_delivery_ledger_state_retry",
            "state",
            "next_retry_at",
            "updated_at",
            "delivery_key",
        ),
        Index(
            "ix_gd_delivery_ledger_board_state",
            "board_id",
            "state",
        ),
    )

    delivery_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    delete_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    attempt_event_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class KGTakedownStateEvent(Base):
    """Append-only governed-deletion timeline.

    The mutable delivery ledger owns the current state.  These immutable rows
    preserve every transition across retries/redrives so operational queries
    never have to infer history from logs or from a later ledger snapshot.
    """

    __tablename__ = "kg_takedown_state_events"
    __table_args__ = (
        CheckConstraint(
            "state IN ('intent_created', 'graph_demoted', "
            "'outbox_persisted', 'delivered', 'delivery_debt')",
            name="ck_kg_takedown_state",
        ),
        CheckConstraint(
            "generation >= 1",
            name="ck_kg_takedown_generation",
        ),
        CheckConstraint(
            "attempt IS NULL OR attempt >= 0",
            name="ck_kg_takedown_attempt",
        ),
        CheckConstraint(
            "state = 'intent_created' OR delivery_key IS NOT NULL",
            name="ck_kg_takedown_delivery_identity",
        ),
        CheckConstraint(
            "state IN ('intent_created', 'graph_demoted') OR attempt IS NOT NULL",
            name="ck_kg_takedown_attempt_state",
        ),
        Index(
            "ix_kg_takedown_delete_time",
            "delete_event_id",
            "occurred_at",
            "transition_key",
        ),
        Index(
            "ix_kg_takedown_delivery_time",
            "delivery_key",
            "occurred_at",
            "transition_key",
        ),
        Index(
            "ix_kg_takedown_state_time",
            "state",
            "occurred_at",
        ),
    )

    transition_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    delete_event_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    delivery_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class GlobalDiscoveryDeliveryRedriveControl(Base):
    """Singleton cursor for bounded, fair Global Discovery debt redrive."""

    __tablename__ = "global_discovery_delivery_redrive_control"
    __table_args__ = (
        CheckConstraint(
            "id = '_global'",
            name="ck_gd_delivery_redrive_control_singleton",
        ),
        CheckConstraint(
            "checkpoint_version >= 0",
            name="ck_gd_delivery_redrive_checkpoint_version",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        default="_global",
        server_default="_global",
    )
    cursor_board_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )
    cursor_oldest_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cursor_delivery_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    checkpoint_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class GlobalDiscoveryDeliveryWatchdogControl(Base):
    """Durable board-local cursor for bounded delivery watchdog scans."""

    __tablename__ = "global_discovery_delivery_watchdog_control"
    __table_args__ = (
        CheckConstraint(
            "checkpoint_version >= 0",
            name="ck_gd_delivery_watchdog_checkpoint_version",
        ),
    )

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    cursor_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cursor_delivery_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    checkpoint_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class DomainEventRow(Base):
    """Append-only log of domain events (outbox pattern).

    Every state change in card/spec/sprint/ideation/refinement publishes a
    row here inside the same transaction as the data change. Readers
    (EventDispatcher worker) consume via domain_event_handler_executions.
    """

    __tablename__ = "domain_events"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'user'")
    )
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )


class DomainEventHandlerExecution(Base):
    """One row per (event, handler) pair. Tracks retry state for the
    async dispatcher; events with multiple handlers get multiple executions.
    """

    __tablename__ = "domain_event_handler_executions"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "handler_name",
            name="uq_deh_event_handler",
        ),
        Index("ix_deh_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("domain_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    handler_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'pending'"),
    )  # pending | processing | done | failed | dlq
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


# ============================================================================
# Discovery — user-facing intent catalog, saved searches, search history
# ============================================================================


class DiscoveryIntent(Base):
    """Catalog of user-facing "pre-built questions" surfaced on the Global
    Discovery screen. Each row binds a human-friendly label to an existing
    backend tool (MCP or REST) so clicking an intent card runs a canned
    query. Managed via an admin UI (deferred to a follow-up card).
    """

    __tablename__ = "discovery_intents"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    tool_binding: Mapped[str] = mapped_column(String(120), nullable=False)
    params_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    renderer: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default=text("'table'")
    )
    min_permission: Mapped[str | None] = mapped_column(String(100), nullable=True)
    active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("1")
    )
    is_seed: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("0")
    )
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DiscoverySavedSearch(Base):
    """A named search saved on a board. Shared with all members of the
    board — per-user private saved searches are a v2 concern.
    """

    __tablename__ = "discovery_saved_searches"
    __table_args__ = (
        UniqueConstraint("board_id", "name", name="uq_saved_search_board_name"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("discovery_intents.id", ondelete="SET NULL"),
        nullable=True,
    )
    filters_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DiscoverySearchHistory(Base):
    """Per-user search history. Capped at 50 most-recent entries per
    (board_id, user_id) via on-INSERT DELETE in the endpoint handler.
    """

    __tablename__ = "discovery_search_history"
    __table_args__ = (
        Index(
            "ix_search_history_board_user_time",
            "board_id",
            "user_id",
            "searched_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("discovery_intents.id", ondelete="SET NULL"),
        nullable=True,
    )
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ============================================================================
# KG operational telemetry — daily decay tick run log (spec 28583299, IMPL-F)
# ============================================================================


class KGTickRun(Base):
    """Operational log of the daily KG relevance recompute tick.

    One row per execution (success or failure). The kg_health endpoint reads
    the most recent ``completed_at IS NOT NULL`` row to surface
    ``last_decay_tick_at`` and ``nodes_recomputed_in_last_tick``. Distinct
    from KG Decision nodes (which audit BUSINESS-meaningful boost changes
    on individual nodes) — this table is purely operational.

    Created via Base.metadata.create_all on first server startup, no Alembic
    migration required (the codebase uses a create_all-based bootstrap).
    """

    __tablename__ = "kg_tick_runs"
    __table_args__ = (Index("idx_kg_tick_runs_completed_at", "completed_at"),)

    tick_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    nodes_recomputed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    boards_processed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    boards_failed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )


class KGCognitiveSource(Base):
    """Durable append-only source of truth for canonical COGNITIVE nodes
    (Learning / Alternative / Assumption) — spec MKG-A-S1.

    One immutable row per committed cognitive node generation. Written by
    the consolidation commit BEFORE it reports success (fail-closed) and
    replayed literally by the KG rebuild when the pre-purge graph snapshot
    is unreadable — cognitive knowledge no longer dies with the graph
    (incident 2026-07-10). Rows are never UPDATEd or DELETEd; idempotency
    is enforced by UNIQUE(node_id, generation).

    Created via Base.metadata.create_all on first server startup, no ledger
    step required (new table, no ALTER/backfill).
    """

    __tablename__ = "kg_cognitive_sources"
    __table_args__ = (
        UniqueConstraint(
            "node_id",
            "generation",
            name="uq_kg_cognitive_sources_node_generation",
        ),
        Index("idx_kg_cognitive_sources_board", "board_id"),
        Index("idx_kg_cognitive_sources_node", "node_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(50), nullable=False)
    generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_session_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class KGCognitiveSourceRevision(Base):
    """Additive immutable revisions for one durable cognitive source row.

    The parent :class:`KGCognitiveSource` remains revision zero so existing
    databases and identifiers stay byte-for-byte intact.  Every later full
    semantic snapshot is appended here; no parent row or revision row is
    rewritten in place.
    """

    __tablename__ = "kg_cognitive_source_revisions"
    __table_args__ = (
        CheckConstraint(
            "source_revision >= 1",
            name="ck_kg_cognitive_source_revisions_positive_revision",
        ),
        CheckConstraint(
            "length(record_fingerprint) = 64",
            name="ck_kg_cognitive_source_revisions_fingerprint_length",
        ),
        UniqueConstraint(
            "cognitive_source_id",
            "source_revision",
            name="uq_kg_cognitive_source_revisions_source_revision",
        ),
        Index(
            "idx_kg_cognitive_source_revisions_source_revision",
            "cognitive_source_id",
            "source_revision",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    cognitive_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "kg_cognitive_sources.id",
            name="fk_kg_cognitive_source_revisions_source",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    record_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_session_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class KGCognitiveSourceFingerprintEpochPermit(Base):
    """Ephemeral exact authority for one governed fingerprint rewrite."""

    __tablename__ = "kg_cognitive_source_fingerprint_epoch_permits"
    __table_args__ = (
        CheckConstraint(
            "length(old_fingerprint) = 64",
            name="ck_kg_cognitive_source_fingerprint_permit_old_length",
        ),
        CheckConstraint(
            "length(new_fingerprint) = 64",
            name="ck_kg_cognitive_source_fingerprint_permit_new_length",
        ),
    )

    revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "kg_cognitive_source_revisions.id",
            name="fk_kg_cognitive_source_fingerprint_permit_revision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        primary_key=True,
    )
    epoch: Mapped[str] = mapped_column(String(64), nullable=False)
    old_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    new_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class KGCognitiveSourceFingerprintEpochReceipt(Base):
    """Immutable proof that one fingerprint-contract epoch completed."""

    __tablename__ = "kg_cognitive_source_fingerprint_epoch_receipts"
    __table_args__ = (
        CheckConstraint(
            "rows_scanned >= 0",
            name="ck_kg_cognitive_source_fingerprint_receipt_scanned",
        ),
        CheckConstraint(
            "rows_rewritten >= 0 AND rows_rewritten <= rows_scanned",
            name="ck_kg_cognitive_source_fingerprint_receipt_rewritten",
        ),
        CheckConstraint(
            "length(before_digest) = 64",
            name="ck_kg_cognitive_source_fingerprint_receipt_before_length",
        ),
        CheckConstraint(
            "length(after_digest) = 64",
            name="ck_kg_cognitive_source_fingerprint_receipt_after_length",
        ),
    )

    epoch: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint_contract: Mapped[str] = mapped_column(String(64), nullable=False)
    rows_scanned: Mapped[int] = mapped_column(Integer, nullable=False)
    rows_rewritten: Mapped[int] = mapped_column(Integer, nullable=False)
    before_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    after_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class KGEquivalenceLedger(Base):
    """Off-graph, append-only ledger of node-equivalence decisions
    (merges) — spec MKG-C-S1.

    One row per merge decision: ``merged_ids`` fold into ``survivor_id``.
    ``evidence`` carries the COMPLETE pre-operation snapshot (node attrs +
    every incident edge with every property) written BEFORE the first
    graph write (BR1). Rows are never DELETEd; un-merge stamps
    ``revoked_at``/``revoke_reason`` and preserves the record for audit.

    Created via Base.metadata.create_all on first startup (new table, no
    ALTER/backfill).
    """

    __tablename__ = "kg_equivalence_ledger"
    __table_args__ = (
        Index("idx_kg_equivalence_ledger_board", "board_id"),
        Index("idx_kg_equivalence_ledger_survivor", "survivor_id"),
    )

    record_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: f"eqv_{uuid.uuid4().hex[:16]}"
    )
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    node_type: Mapped[str] = mapped_column(String(50), nullable=False)
    survivor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    merged_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class KGCurationProposal(Base):
    """Persisted curation proposal (spec MKG-C-S1 FR7): canonical plan +
    deterministic proposal_hash; approval re-validates the hash against the
    current state before any write (same contract as the rebuild
    preflight_hash)."""

    __tablename__ = "kg_curation_proposals"
    __table_args__ = (Index("idx_kg_curation_proposals_board", "board_id"),)

    proposal_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: f"prop_{uuid.uuid4().hex[:16]}",
    )
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    plan: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class KGNodeSubtype(Base):
    """Declarative subtype vocabulary (spec MKG-E-S1): kind_of under one of
    the 11 closed physical node types. Data, not schema — declarations
    never bump SCHEMA_VERSION (D6)."""

    __tablename__ = "kg_node_subtypes"
    __table_args__ = (
        UniqueConstraint("node_type", "kind_of", name="uq_kg_node_subtypes_type_kind"),
        Index("idx_kg_node_subtypes_type", "node_type"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    node_type: Mapped[str] = mapped_column(String(50), nullable=False)
    kind_of: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class BoardErasurePermit(Base):
    """Transaction-local authorization for immutable board-data erasure.

    Rows are inserted and removed by ``purge_board_metadata`` in the same
    uncommitted transaction. SQLite immutable-history triggers consult this
    table only for DELETE operations belonging to the authorized board. A row
    that ever survives a transaction is treated as a conflict, never reused.
    """

    __tablename__ = "kg_board_erasure_permits"

    board_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    permit_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class BoardErasureJob(Base):
    """Durable physical-erasure continuation independent of the Board row."""

    __tablename__ = "kg_board_erasure_jobs"
    __table_args__ = (
        CheckConstraint(
            "status = 'pending'",
            name="ck_kg_board_erasure_job_status",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_kg_board_erasure_job_attempts",
        ),
        Index(
            "ix_kg_board_erasure_jobs_due",
            "status",
            "next_attempt_at",
            "board_id",
        ),
    )

    # Deliberately no FK to boards.id: this row is committed atomically with
    # the Board DELETE and must remain available after every source row is gone.
    board_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class GlobalDiscoveryRecoveryAttempt(Base):
    """Durable fenced attempt history for Global Discovery recovery.

    This is a new v0.3.0 table, so the Community schema lifecycle creates it
    at its single ``Base.metadata.create_all`` boundary. The recovery adapter
    deliberately owns no independent metadata or schema bootstrap path.
    """

    __tablename__ = "global_discovery_recovery_attempts"

    __table_args__ = (
        Index(
            "uq_global_discovery_recovery_attempt_identity",
            "attempt_id",
            unique=True,
        ),
    )

    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    epoch: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(512), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    requester_actor_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'[]'")
    )
    request_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    replay_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    requester_actor_overflow_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    first_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmation_fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    manifest_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    preflight_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    binding_reason: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    progress_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(128), nullable=False)
    preparation_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'queued'")
    )
    confirmation_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'unconfirmed'")
    )
    boards_total: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    boards_scanned: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    sources_total: Mapped[int] = mapped_column(Integer, nullable=False)
    sources_processed: Mapped[int] = mapped_column(Integer, nullable=False)
    nodes_written: Mapped[int] = mapped_column(Integer, nullable=False)
    edges_written: Mapped[int] = mapped_column(Integer, nullable=False)
    outbox_events_drained: Mapped[int] = mapped_column(Integer, nullable=False)
    errors: Mapped[int] = mapped_column(Integer, nullable=False)
    heartbeat_at: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    active_elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    active_deadline_at: Mapped[str] = mapped_column(String(64), nullable=False)
    cumulative_active_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_budget_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("600000")
    )
    prepared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    snapshot_fingerprint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_by_actor_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    confirmation_consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    audit_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cancel_requested_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancel_requested_by_actor_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    cancel_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resume_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resume_requested_by_actor_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    resume_audit_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    terminal_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    retryable: Mapped[bool] = mapped_column(nullable=False)
    supersedes_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    superseded_by_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    physical_journal_phase: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    physical_pointer_replaced: Mapped[bool | None] = mapped_column(nullable=True)
    physical_rollback_performed: Mapped[bool | None] = mapped_column(nullable=True)
    physical_evidence_ref: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )


class GlobalDiscoverySourceRevision(Base):
    """O(1) relational freshness fence for preparation inputs.

    SQLite triggers owned by the Community schema lifecycle advance the one
    ``_global`` row inside the same transaction as every relevant mutation.
    A check constraint makes the table structurally singleton-shaped, while
    protective triggers make removal or re-keying of the fence fail closed.
    """

    __tablename__ = "global_discovery_source_revision"
    __table_args__ = (
        CheckConstraint(
            "scope_id = '_global'",
            name="ck_global_discovery_source_revision_global_scope",
        ),
        CheckConstraint(
            "revision >= 0",
            name="ck_global_discovery_source_revision_nonnegative",
        ),
        Index(
            "uq_global_discovery_source_revision_scope",
            "scope_id",
            unique=True,
        ),
    )

    scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fence_version: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_manifest_version: Mapped[str] = mapped_column(String(64), nullable=False)
    incarnation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    mutation_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GlobalDiscoveryRecoverySlot(Base):
    """Database-enforced owner of the one global recovery lifecycle slot."""

    __tablename__ = "global_discovery_recovery_slots"
    __table_args__ = (
        CheckConstraint(
            "slot_id = '_global'",
            name="ck_global_discovery_recovery_slot_global_scope",
        ),
        CheckConstraint(
            "epoch >= 1",
            name="ck_global_discovery_recovery_slot_epoch_positive",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_global_discovery_recovery_slot_version_positive",
        ),
    )

    slot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(512), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class GlobalDiscoveryRecoveryDispatch(Base):
    """Durable preparation/recovery dispatch with expiring claim fencing."""

    __tablename__ = "global_discovery_recovery_dispatches"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "attempt_id",
            "epoch",
            "stage",
            name="uq_global_discovery_recovery_dispatch_attempt_stage",
        ),
        Index(
            "idx_global_discovery_recovery_dispatch_claim",
            "stage",
            "state",
            "available_at",
            "claim_expires_at",
        ),
    )

    dispatch_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(512), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    transition_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transition_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transition_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class GlobalDiscoveryRecoveryTransition(Base):
    """Transactional structured event and exactly-once metric ledger."""

    __tablename__ = "global_discovery_recovery_transitions"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "attempt_id",
            "epoch",
            "progress_seq",
            name="uq_global_discovery_recovery_transition_progress",
        ),
        CheckConstraint(
            "epoch >= 1",
            name="ck_global_discovery_recovery_transition_epoch_positive",
        ),
        CheckConstraint(
            "progress_seq >= 0",
            name="ck_global_discovery_recovery_transition_progress_nonnegative",
        ),
        Index(
            "idx_global_discovery_recovery_transition_metrics",
            "operation",
            "outcome",
            "phase",
            "reason_code",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(512), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    progress_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    metric_labels: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


# ---------------------------------------------------------------------------
# Selective Knowledge Base propagation v2
# ---------------------------------------------------------------------------
#
# These tables are additive beside the legacy ``*_knowledge_bases`` storage
# and ``cards.knowledge_bases`` JSON.  Legacy content remains the durable
# physical/history source; the v2 records only govern target selection,
# temporal assignment state, immutable snapshots and mutation evidence.


class KnowledgePropagationScopeRecord(Base):
    """Revision fence and tri-state selection state for one spec/card target."""

    __tablename__ = "knowledge_propagation_scopes"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "target_type",
            "target_id",
            name="uq_knowledge_propagation_scope_target",
        ),
        CheckConstraint(
            "target_type IN ('spec', 'card')",
            name="ck_knowledge_propagation_scope_target_type",
        ),
        CheckConstraint(
            "scope_revision >= 0",
            name="ck_knowledge_propagation_scope_revision",
        ),
        CheckConstraint(
            "v2_active IN (0, 1)",
            name="ck_knowledge_propagation_scope_v2_active",
        ),
        CheckConstraint(
            "(v2_active = 0 AND selection_state IS NULL) OR "
            "(v2_active = 1 AND selection_state IN "
            "('omitted', 'explicit_empty', 'explicit_ids'))",
            name="ck_knowledge_propagation_scope_selection_state",
        ),
        Index(
            "ix_knowledge_propagation_scope_board_target",
            "board_id",
            "target_type",
            "target_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # Audit identity, intentionally not an FK. Ordinary Board cascades must not
    # silently erase append-only history. The explicit right-to-erasure path
    # authorizes and verifies deletion of the complete propagation cluster in
    # the same transaction. The write adapter revalidates the live
    # board-scoped spec/card immediately before its CAS.
    board_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scope_revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    v2_active: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    selection_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # First durable transition into selective propagation v2.  It remains
    # immutable across governed re-links so physical Spec KB rows can always
    # be classified against the original v2 boundary.
    v2_activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class KnowledgeAssignmentRecord(Base):
    """Typed append-history assignment with an explicit effective-time window."""

    __tablename__ = "knowledge_propagation_assignments"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('reference', 'snapshot', 'drop')",
            name="ck_knowledge_assignment_mode",
        ),
        CheckConstraint(
            "state IN ('active', 'stale', 'source_deleted', 'dropped', 'inactive')",
            name="ck_knowledge_assignment_state",
        ),
        CheckConstraint(
            "origin_class IN "
            "('v2', 'legacy_all', 'selected_legacy', 'legacy_unresolved')",
            name="ck_knowledge_assignment_origin_class",
        ),
        CheckConstraint(
            "(mode = 'drop') = (state = 'dropped')",
            name="ck_knowledge_assignment_drop_state",
        ),
        CheckConstraint(
            "state != 'stale' OR mode = 'snapshot'",
            name="ck_knowledge_assignment_stale_mode",
        ),
        CheckConstraint(
            "revision >= 0",
            name="ck_knowledge_assignment_revision",
        ),
        CheckConstraint(
            "source_content_sha256 IS NULL OR length(source_content_sha256) = 64",
            name="ck_knowledge_assignment_source_hash",
        ),
        CheckConstraint(
            "origin_class != 'v2' OR "
            "(source_revision IS NOT NULL AND "
            "source_content_sha256 IS NOT NULL AND justification IS NOT NULL)",
            name="ck_knowledge_assignment_v2_evidence",
        ),
        CheckConstraint(
            "mode != 'drop' OR justification IS NOT NULL",
            name="ck_knowledge_assignment_drop_justification",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_knowledge_assignment_effective_window",
        ),
        CheckConstraint(
            "(effective_to IS NULL AND superseded_by_id IS NULL) OR "
            "effective_to IS NOT NULL",
            name="ck_knowledge_assignment_supersession_window",
        ),
        Index(
            "uq_knowledge_assignment_current_root",
            "scope_id",
            "root_id",
            unique=True,
            sqlite_where=text("effective_to IS NULL"),
        ),
        Index(
            "ix_knowledge_assignment_scope_history",
            "scope_id",
            "effective_from",
            "assignment_id",
        ),
        Index(
            "ix_knowledge_assignment_source",
            "source_knowledge_id",
            "root_id",
        ),
    )

    assignment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "knowledge_propagation_scopes.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    source_knowledge_id: Mapped[str] = mapped_column(String(64), nullable=False)
    root_id: Mapped[str] = mapped_column(String(64), nullable=False)
    immediate_parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_revision: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    origin_class: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance_links: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    superseded_by_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "knowledge_propagation_assignments.assignment_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )


class KnowledgeSnapshotRecord(Base):
    """Immutable canonical bytes captured for a snapshot-mode assignment."""

    __tablename__ = "knowledge_propagation_snapshots"
    __table_args__ = (
        CheckConstraint(
            "length(source_content_sha256) = 64",
            name="ck_knowledge_snapshot_source_hash",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_knowledge_snapshot_effective_window",
        ),
        CheckConstraint(
            "(effective_to IS NULL AND superseded_by_id IS NULL) OR "
            "effective_to IS NOT NULL",
            name="ck_knowledge_snapshot_supersession_window",
        ),
        Index(
            "uq_knowledge_snapshot_current_assignment",
            "assignment_id",
            unique=True,
            sqlite_where=text("effective_to IS NULL"),
        ),
        Index(
            "ix_knowledge_snapshot_scope_history",
            "scope_id",
            "effective_from",
            "snapshot_id",
        ),
    )

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "knowledge_propagation_scopes.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    assignment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "knowledge_propagation_assignments.assignment_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    root_id: Mapped[str] = mapped_column(String(64), nullable=False)
    immediate_parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_revision: Mapped[str] = mapped_column(String(255), nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    superseded_by_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "knowledge_propagation_snapshots.snapshot_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    governance_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class KnowledgeTombstoneRecord(Base):
    """Durable per-root/global DROP marker preventing legacy resurrection."""

    __tablename__ = "knowledge_propagation_tombstones"
    __table_args__ = (
        CheckConstraint(
            "length(trim(actor_id)) > 0",
            name="ck_knowledge_tombstone_actor",
        ),
        CheckConstraint(
            "length(trim(justification)) > 0",
            name="ck_knowledge_tombstone_justification",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_knowledge_tombstone_effective_window",
        ),
        CheckConstraint(
            "(effective_to IS NULL AND superseded_by_id IS NULL) OR "
            "effective_to IS NOT NULL",
            name="ck_knowledge_tombstone_supersession_window",
        ),
        Index(
            "uq_knowledge_tombstone_current_root",
            "scope_id",
            "root_id",
            unique=True,
            sqlite_where=text("effective_to IS NULL AND root_id IS NOT NULL"),
        ),
        Index(
            "uq_knowledge_tombstone_current_global",
            "scope_id",
            unique=True,
            sqlite_where=text("effective_to IS NULL AND root_id IS NULL"),
        ),
        Index(
            "ix_knowledge_tombstone_scope_history",
            "scope_id",
            "effective_from",
            "tombstone_id",
        ),
    )

    tombstone_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "knowledge_propagation_scopes.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    root_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    superseded_by_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "knowledge_propagation_tombstones.tombstone_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )


class KnowledgeMutationLedgerRecord(Base):
    """Canonical immutable terminal result for a target/idempotency key."""

    __tablename__ = "knowledge_mutation_ledger"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "target_type",
            "target_id",
            "idempotency_key",
            name="uq_knowledge_mutation_ledger_target_key",
        ),
        UniqueConstraint(
            "scope_id",
            "idempotency_key",
            name="uq_knowledge_mutation_ledger_scope_key",
        ),
        CheckConstraint(
            "target_type IN ('spec', 'card')",
            name="ck_knowledge_mutation_ledger_target_type",
        ),
        CheckConstraint(
            "operation_kind IN "
            "('replace_omitted', 'replace', 'drop_delta', 'replace_empty', "
            "'refresh_snapshot', 'grandfather', 'relink_reset')",
            name="ck_knowledge_mutation_ledger_operation_kind",
        ),
        CheckConstraint(
            "outcome IN ('applied', 'noop', 'rejected', 'grandfathered')",
            name="ck_knowledge_mutation_ledger_outcome",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_knowledge_mutation_ledger_request_hash",
        ),
        CheckConstraint(
            "previous_revision >= 0 AND "
            "((outcome IN ('applied', 'grandfathered') "
            "AND revision = previous_revision + 1) OR "
            "(outcome IN ('noop', 'rejected') "
            "AND revision = previous_revision))",
            name="ck_knowledge_mutation_ledger_revision",
        ),
        CheckConstraint(
            "outcome != 'rejected' OR "
            "(reason_code IS NOT NULL AND reason_detail IS NOT NULL)",
            name="ck_knowledge_mutation_ledger_rejection_reason",
        ),
        Index(
            "ix_knowledge_mutation_ledger_target_time",
            "board_id",
            "target_type",
            "target_id",
            "recorded_at",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "knowledge_propagation_scopes.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class KnowledgeMutationAttemptRecord(Base):
    """Append-only replay/rejection observation beside the canonical ledger."""

    __tablename__ = "knowledge_mutation_attempts"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('spec', 'card')",
            name="ck_knowledge_mutation_attempt_target_type",
        ),
        CheckConstraint(
            "operation_kind IN "
            "('replace_omitted', 'replace', 'drop_delta', 'replace_empty', "
            "'refresh_snapshot', 'grandfather', 'relink_reset')",
            name="ck_knowledge_mutation_attempt_operation_kind",
        ),
        CheckConstraint(
            "outcome IN ('replayed', 'rejected')",
            name="ck_knowledge_mutation_attempt_outcome",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_knowledge_mutation_attempt_request_hash",
        ),
        CheckConstraint(
            "(outcome = 'replayed' AND original_operation_id IS NOT NULL "
            "AND reason_code IS NULL AND reason_detail IS NULL) OR "
            "(outcome = 'rejected' AND reason_code IS NOT NULL "
            "AND reason_detail IS NOT NULL)",
            name="ck_knowledge_mutation_attempt_evidence",
        ),
        Index(
            "ix_knowledge_mutation_attempt_target_time",
            "board_id",
            "target_type",
            "target_id",
            "recorded_at",
        ),
        Index(
            "ix_knowledge_mutation_attempt_idempotency",
            "board_id",
            "target_type",
            "target_id",
            "idempotency_key",
            "recorded_at",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "knowledge_propagation_scopes.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    original_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


# ============================================================================
# QUALITY ASSESSMENTS (SK-A D0)
# ============================================================================


class RequirementLintValidationSnapshotRow(Base):
    """Immutable external-lint authority and anchors pinned per Spec edition."""

    __tablename__ = "requirement_lint_validation_snapshots"
    __table_args__ = (
        CheckConstraint(
            "spec_edition >= 1",
            name="ck_requirement_lint_validation_snapshot_edition",
        ),
        CheckConstraint(
            "length(ruleset_digest) = 64 AND length(taxonomy_digest) = 64",
            name="ck_requirement_lint_validation_snapshot_digests",
        ),
    )

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    spec_edition: Mapped[int] = mapped_column(Integer, primary_key=True)
    ruleset_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    taxonomy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    anchors_json: Mapped[list] = mapped_column(JSON, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class QualityAssessmentReceiptRow(Base):
    """Immutable persisted assessment receipt and replay identity."""

    __tablename__ = "quality_assessment_receipts"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_quality_receipt_board_idempotency",
        ),
        UniqueConstraint(
            "board_id",
            "subject_type",
            "subject_id",
            "assessment_kind",
            "id",
            name="uq_quality_receipt_subject_identity",
        ),
        UniqueConstraint("event_id", name="uq_quality_receipt_event"),
        UniqueConstraint("history_id", name="uq_quality_receipt_history"),
        UniqueConstraint("outbox_id", name="uq_quality_receipt_outbox"),
        CheckConstraint(
            "subject_type IN ('ideation', 'refinement', 'spec')",
            name="ck_quality_receipt_subject_type",
        ),
        CheckConstraint(
            "assessment_kind IN ('ambiguity', 'spec_validation', 'requirement_lint')",
            name="ck_quality_receipt_kind",
        ),
        CheckConstraint(
            "origin IN "
            "('human_or_agent', 'spec_validation', 'semantic_writer', "
            "'legacy_import')",
            name="ck_quality_receipt_origin",
        ),
        CheckConstraint(
            "source IN ('native', 'legacy_migration')",
            name="ck_quality_receipt_source",
        ),
        CheckConstraint(
            "outcome IN ('recorded', 'advisory')",
            name="ck_quality_receipt_outcome",
        ),
        CheckConstraint(
            "scale_kind IN ('ambiguity_score', 'percentage', 'finding_count')",
            name="ck_quality_receipt_scale_kind",
        ),
        CheckConstraint(
            "scale_direction IN ('lower_better', 'higher_better')",
            name="ck_quality_receipt_scale_direction",
        ),
        CheckConstraint(
            "subject_version >= 1",
            name="ck_quality_receipt_subject_version",
        ),
        CheckConstraint(
            "subject_edition IS NULL OR subject_edition >= 1",
            name="ck_quality_receipt_subject_edition",
        ),
        CheckConstraint(
            "head_revision >= 1",
            name="ck_quality_receipt_head_revision",
        ),
        CheckConstraint(
            "scale_minimum < scale_maximum",
            name="ck_quality_receipt_scale_bounds",
        ),
        CheckConstraint(
            "score >= scale_minimum AND score <= scale_maximum",
            name="ck_quality_receipt_score_bounds",
        ),
        CheckConstraint(
            "length(content_digest) = 64 "
            "AND length(clarification_digest) = 64 "
            "AND length(ruleset_digest) = 64 "
            "AND length(taxonomy_digest) = 64 "
            "AND length(policy_digest) = 64 "
            "AND length(input_digest) = 64 "
            "AND length(run_identity_digest) = 64 "
            "AND length(authority_digest) = 64 "
            "AND length(request_digest) = 64",
            name="ck_quality_receipt_digest_lengths",
        ),
        Index(
            "ix_quality_receipt_subject_created",
            "board_id",
            "subject_type",
            "subject_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_quality_receipt_subject_kind_created",
            "board_id",
            "subject_type",
            "subject_id",
            "assessment_kind",
            "created_at",
            "id",
        ),
        Index(
            "ix_quality_receipt_subject_edition_kind_created",
            "board_id",
            "subject_type",
            "subject_id",
            "subject_edition",
            "assessment_kind",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL is reserved for evidence recorded before lifecycle editions existed.
    subject_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assessment_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    scale_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scale_minimum: Mapped[float] = mapped_column(Float, nullable=False)
    scale_maximum: Mapped[float] = mapped_column(Float, nullable=False)
    scale_direction: Mapped[str] = mapped_column(String(24), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    clarification_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    ruleset_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    taxonomy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    canonicalization_version: Mapped[str] = mapped_column(String(64), nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    run_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    authority_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    predecessor_receipt_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("quality_assessment_receipts.id", ondelete="SET NULL"),
        nullable=True,
    )
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    history_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outbox_id: Mapped[str] = mapped_column(String(64), nullable=False)
    head_revision: Mapped[int] = mapped_column(Integer, nullable=False)


class QualityFindingRow(Base):
    """Lossless finding projection with stable structured anchors."""

    __tablename__ = "quality_findings"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id",
            "finding_key",
            name="uq_quality_finding_receipt_key",
        ),
        UniqueConstraint(
            "receipt_id",
            "id",
            name="uq_quality_finding_receipt_id",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_quality_finding_confidence",
        ),
        CheckConstraint(
            "anchor_subject_version >= 1",
            name="ck_quality_finding_anchor_version",
        ),
        CheckConstraint(
            "length(anchor_input_digest) = 64",
            name="ck_quality_finding_input_digest",
        ),
        CheckConstraint(
            "subject_type IN ('ideation', 'refinement', 'spec') "
            "AND anchor_subject_type IN ('ideation', 'refinement', 'spec')",
            name="ck_quality_finding_subject_types",
        ),
        CheckConstraint(
            "assessment_kind IN ('ambiguity', 'spec_validation', 'requirement_lint')",
            name="ck_quality_finding_kind",
        ),
        CheckConstraint(
            "severity IN ('info', 'low', 'medium', 'high', 'critical')",
            name="ck_quality_finding_severity",
        ),
        CheckConstraint(
            "anchor_type IN ('whole_artifact', 'field', 'structured_child', 'qa')",
            name="ck_quality_finding_anchor_type",
        ),
        CheckConstraint(
            "lifecycle IN ('open', 'resolved', 'superseded')",
            name="ck_quality_finding_lifecycle",
        ),
        ForeignKeyConstraint(
            (
                "board_id",
                "subject_type",
                "subject_id",
                "assessment_kind",
                "receipt_id",
            ),
            (
                "quality_assessment_receipts.board_id",
                "quality_assessment_receipts.subject_type",
                "quality_assessment_receipts.subject_id",
                "quality_assessment_receipts.assessment_kind",
                "quality_assessment_receipts.id",
            ),
            ondelete="CASCADE",
            name="fk_quality_finding_receipt_subject",
        ),
        Index(
            "ix_quality_finding_subject_created",
            "board_id",
            "subject_type",
            "subject_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    assessment_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    finding_key: Mapped[str] = mapped_column(String(512), nullable=False)
    category_code: Mapped[str] = mapped_column(String(128), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    deterministic: Mapped[bool] = mapped_column(nullable=False)
    blocking_eligible: Mapped[bool] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    anchor_subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    anchor_subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    anchor_subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    anchor_input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    anchor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    anchor_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    excerpt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    lifecycle: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    remediation: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_code: Mapped[str | None] = mapped_column(String(128), nullable=True)


class QualityProposedQuestionRow(Base):
    """Receipt-owned proposal linked to the server-issued subject Q&A id."""

    __tablename__ = "quality_proposed_questions"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id",
            "client_key",
            name="uq_quality_question_receipt_client",
        ),
        UniqueConstraint(
            "receipt_id",
            "qa_id",
            name="uq_quality_question_receipt_qa",
        ),
        Index(
            "ix_quality_question_receipt_created",
            "receipt_id",
            "created_at",
            "qa_id",
        ),
    )

    qa_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("quality_assessment_receipts.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_key: Mapped[str] = mapped_column(String(512), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[str] = mapped_column(String(32), nullable=False)
    choices: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    allow_free_text: Mapped[bool] = mapped_column(nullable=False)
    category_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class QualityFindingQaLinkRow(Base):
    """Receipt-scoped many-to-many link between findings and proposed Q&A."""

    __tablename__ = "quality_finding_qa_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ("receipt_id", "finding_id"),
            ("quality_findings.receipt_id", "quality_findings.id"),
            ondelete="CASCADE",
            name="fk_quality_link_finding",
        ),
        ForeignKeyConstraint(
            ("receipt_id", "qa_id"),
            (
                "quality_proposed_questions.receipt_id",
                "quality_proposed_questions.qa_id",
            ),
            ondelete="CASCADE",
            name="fk_quality_link_question",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    finding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    qa_id: Mapped[str] = mapped_column(String(64), primary_key=True)


class QualityAssessmentHeadRow(Base):
    """CAS-protected current receipt pointer per subject and assessment kind."""

    __tablename__ = "quality_assessment_heads"
    __table_args__ = (
        UniqueConstraint("receipt_id", name="uq_quality_head_receipt"),
        CheckConstraint("revision >= 1", name="ck_quality_head_revision"),
        CheckConstraint(
            "subject_type IN ('ideation', 'refinement', 'spec')",
            name="ck_quality_head_subject_type",
        ),
        CheckConstraint(
            "assessment_kind IN ('ambiguity', 'spec_validation', 'requirement_lint')",
            name="ck_quality_head_kind",
        ),
        ForeignKeyConstraint(
            (
                "board_id",
                "subject_type",
                "subject_id",
                "assessment_kind",
                "receipt_id",
            ),
            (
                "quality_assessment_receipts.board_id",
                "quality_assessment_receipts.subject_type",
                "quality_assessment_receipts.subject_id",
                "quality_assessment_receipts.assessment_kind",
                "quality_assessment_receipts.id",
            ),
            ondelete="CASCADE",
            name="fk_quality_head_receipt_subject",
        ),
        Index(
            "ix_quality_head_subject",
            "board_id",
            "subject_type",
            "subject_id",
        ),
    )

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    subject_type: Mapped[str] = mapped_column(String(24), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assessment_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class QualityAssessmentOutboxRow(Base):
    """Verifiable outbox identity paired with the canonical domain event."""

    __tablename__ = "quality_assessment_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_quality_outbox_event"),
        UniqueConstraint("receipt_id", name="uq_quality_outbox_receipt"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'failed')",
            name="ck_quality_outbox_status",
        ),
        Index(
            "ix_quality_outbox_status_created",
            "status",
            "occurred_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("domain_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("quality_assessment_receipts.id", ondelete="CASCADE"),
        nullable=False,
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        server_default=text("'pending'"),
    )
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


# ============================================================================
# SK-A RESEARCH DECISION LEDGER
# ============================================================================


class ResearchDecisionEntryRow(Base):
    """Immutable RDL entry; evolution is represented only by a successor."""

    __tablename__ = "research_decision_entries"
    __table_args__ = (
        UniqueConstraint(
            "refinement_id",
            "refinement_version",
            name="uq_rdl_entry_refinement_version",
        ),
        UniqueConstraint(
            "predecessor_entry_id",
            name="uq_rdl_entry_predecessor",
        ),
        CheckConstraint(
            "refinement_version >= 1",
            name="ck_rdl_entry_refinement_version",
        ),
        CheckConstraint(
            "status IN ('open', 'investigating', 'resolved', 'deferred')",
            name="ck_rdl_entry_status",
        ),
        CheckConstraint(
            "anchor_type IN "
            "('functional_requirement', 'acceptance_criterion', "
            "'technical_requirement', 'qa')",
            name="ck_rdl_entry_anchor_type",
        ),
        Index(
            "ix_rdl_entry_refinement_created",
            "board_id",
            "refinement_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_rdl_entry_ledger_created",
            "ledger_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ledger_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id"),
        nullable=False,
    )
    refinement_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("refinements.id"),
        nullable=False,
    )
    refinement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    predecessor_entry_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("research_decision_entries.id"),
        nullable=True,
    )
    unknown: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    anchor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    anchor_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    alternatives: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_absence_justification: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ResearchDecisionHeadRow(Base):
    """CAS pointer to the current immutable entry of one ledger thread."""

    __tablename__ = "research_decision_heads"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_rdl_head_revision"),
        CheckConstraint(
            "refinement_version >= 1",
            name="ck_rdl_head_refinement_version",
        ),
        CheckConstraint(
            "status IN ('open', 'investigating', 'resolved', 'deferred')",
            name="ck_rdl_head_status",
        ),
        UniqueConstraint(
            "current_entry_id",
            name="uq_rdl_head_current_entry",
        ),
        Index(
            "ix_rdl_head_refinement",
            "board_id",
            "refinement_id",
            "ledger_id",
        ),
    )

    ledger_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id"),
        nullable=False,
    )
    refinement_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("refinements.id"),
        nullable=False,
    )
    current_entry_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("research_decision_entries.id"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    refinement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ResearchDecisionHistoryRow(Base):
    """Insert-only audit row paired one-to-one with an RDL entry."""

    __tablename__ = "research_decision_history"
    __table_args__ = (
        CheckConstraint(
            "action IN ('append', 'supersede')",
            name="ck_rdl_history_action",
        ),
        UniqueConstraint("entry_id", name="uq_rdl_history_entry"),
        Index(
            "ix_rdl_history_refinement_created",
            "board_id",
            "refinement_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id"),
        nullable=False,
    )
    refinement_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("refinements.id"),
        nullable=False,
    )
    ledger_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("research_decision_entries.id"),
        nullable=False,
    )
    predecessor_entry_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    from_refinement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    to_refinement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ResearchDecisionOutboxRow(Base):
    """Pending dispatch paired with the canonical domain event."""

    __tablename__ = "research_decision_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_rdl_outbox_event"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'failed')",
            name="ck_rdl_outbox_status",
        ),
        Index(
            "ix_rdl_outbox_status_available",
            "status",
            "available_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("domain_events.id"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    partition_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        server_default=text("'pending'"),
    )
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ResearchDecisionIdempotencyRow(Base):
    """Stable request binding used to return the original write receipt."""

    __tablename__ = "research_decision_idempotency"
    __table_args__ = (
        CheckConstraint(
            "length(request_digest) = 64",
            name="ck_rdl_idempotency_digest",
        ),
        Index(
            "ix_rdl_idempotency_entry",
            "entry_id",
        ),
    )

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id"),
        primary_key=True,
    )
    refinement_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("refinements.id"),
        primary_key=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    ledger_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("research_decision_entries.id"),
        nullable=False,
    )
    head_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    refinement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    history_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("research_decision_history.id"),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("domain_events.id"),
        nullable=False,
    )
    outbox_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("research_decision_outbox.id"),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ResearchDecisionSnapshotRow(Base):
    """Frozen head identities captured at one Refinement version."""

    __tablename__ = "research_decision_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "refinement_id",
            "refinement_version",
            name="uq_rdl_snapshot_refinement_version",
        ),
        Index(
            "ix_rdl_snapshot_refinement_created",
            "board_id",
            "refinement_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id"),
        nullable=False,
    )
    refinement_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("refinements.id"),
        nullable=False,
    )
    refinement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    heads_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ResearchDecisionDerivationRow(Base):
    """Immutable resolved RDL references bound to one derived Spec version."""

    __tablename__ = "research_decision_derivations"
    __table_args__ = (
        UniqueConstraint(
            "spec_id",
            "spec_version",
            name="uq_rdl_derivation_spec_version",
        ),
        CheckConstraint(
            "spec_version >= 1",
            name="ck_rdl_derivation_spec_version",
        ),
        CheckConstraint(
            "source_refinement_version >= 1",
            name="ck_rdl_derivation_refinement_version",
        ),
        Index(
            "ix_rdl_derivation_source",
            "board_id",
            "source_refinement_id",
            "source_snapshot_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: f"rdld_{uuid.uuid4().hex}",
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id"),
        nullable=False,
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id"),
        nullable=False,
    )
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_refinement_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("refinements.id"),
        nullable=False,
    )
    source_refinement_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    source_snapshot_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("research_decision_snapshots.id"),
        nullable=False,
    )
    references_json: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


for _rdl_immutable_table in (
    ResearchDecisionEntryRow.__table__,
    ResearchDecisionHistoryRow.__table__,
    ResearchDecisionSnapshotRow.__table__,
    ResearchDecisionDerivationRow.__table__,
):
    event.listen(
        _rdl_immutable_table,
        "after_create",
        DDL(
            "CREATE TRIGGER IF NOT EXISTS "
            f"trg_{_rdl_immutable_table.name}_immutable_update "
            f"BEFORE UPDATE ON {_rdl_immutable_table.name} "
            "BEGIN SELECT RAISE(ABORT, "
            "'research_decision_entry_immutable'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        _rdl_immutable_table,
        "after_create",
        DDL(
            "CREATE TRIGGER IF NOT EXISTS "
            f"trg_{_rdl_immutable_table.name}_immutable_delete "
            f"BEFORE DELETE ON {_rdl_immutable_table.name} "
            "BEGIN SELECT RAISE(ABORT, "
            "'research_decision_entry_immutable'); END"
        ).execute_if(dialect="sqlite"),
    )


# ============================================================================
# A3 CURATED SPEC CHECKLIST
# ============================================================================


class ChecklistTemplateVersionRow(Base):
    """Immutable curated checklist template manifest."""

    __tablename__ = "checklist_template_versions"
    __table_args__ = (
        UniqueConstraint(
            "template_id",
            "digest",
            name="uq_checklist_template_identity",
        ),
        CheckConstraint(
            "length(digest) = 64",
            name="ck_checklist_template_digest",
        ),
    )

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    items_json: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ChecklistBindingRow(Base):
    """Append-only board/target/phase checklist binding version.

    The primary-key version identifies the immutable governance record.
    ``digest`` identifies executable checklist semantics, so mode-only
    revisions intentionally share it and it must not be unique in this scope.
    """

    __tablename__ = "checklist_bindings"
    __table_args__ = (
        CheckConstraint(
            "target_type = 'spec'",
            name="ck_checklist_binding_target_type",
        ),
        CheckConstraint(
            "phase = 'spec_validation'",
            name="ck_checklist_binding_phase",
        ),
        CheckConstraint(
            "mode IN ('off', 'advisory', 'blocking')",
            name="ck_checklist_binding_mode",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_checklist_binding_version",
        ),
        CheckConstraint(
            "length(digest) = 64",
            name="ck_checklist_binding_digest",
        ),
    )

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    target_type: Mapped[str] = mapped_column(String(24), primary_key=True)
    phase: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    template_version: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("checklist_template_versions.version"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ChecklistBindingHeadRow(Base):
    """Mutable CAS pointer to the effective binding version."""

    __tablename__ = "checklist_binding_heads"
    __table_args__ = (
        ForeignKeyConstraint(
            (
                "board_id",
                "target_type",
                "phase",
                "version",
            ),
            (
                "checklist_bindings.board_id",
                "checklist_bindings.target_type",
                "checklist_bindings.phase",
                "checklist_bindings.version",
            ),
            ondelete="CASCADE",
            name="fk_checklist_binding_head_version",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_checklist_binding_head_version",
        ),
        CheckConstraint(
            "length(digest) = 64",
            name="ck_checklist_binding_head_digest",
        ),
    )

    board_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_type: Mapped[str] = mapped_column(String(24), primary_key=True)
    phase: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ChecklistValidationBindingSnapshotRow(Base):
    """Immutable checklist-governance pin for one Spec lifecycle edition.

    The row deliberately stores the resolved binding value rather than a
    foreign key to the live head.  This also represents the synthetic OFF
    configuration (revision zero), ensuring that enabling the board checklist
    later cannot retroactively add work to an edition already in validation.
    """

    __tablename__ = "checklist_validation_binding_snapshots"
    __table_args__ = (
        CheckConstraint(
            "spec_edition >= 1 AND binding_version >= 1 AND binding_revision >= 0",
            name="ck_checklist_validation_binding_snapshot_versions",
        ),
        CheckConstraint(
            "target_type = 'spec'",
            name="ck_checklist_validation_binding_snapshot_target_type",
        ),
        CheckConstraint(
            "phase = 'spec_validation'",
            name="ck_checklist_validation_binding_snapshot_phase",
        ),
        CheckConstraint(
            "mode IN ('off', 'advisory', 'blocking')",
            name="ck_checklist_validation_binding_snapshot_mode",
        ),
        CheckConstraint(
            "(binding_revision = 0 AND binding_version = 1 AND mode = 'off') "
            "OR binding_revision = binding_version",
            name="ck_checklist_validation_binding_snapshot_revision",
        ),
        CheckConstraint(
            "length(binding_digest) = 64",
            name="ck_checklist_validation_binding_snapshot_digest",
        ),
    )

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    spec_edition: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_type: Mapped[str] = mapped_column(String(24), primary_key=True)
    phase: Mapped[str] = mapped_column(String(32), primary_key=True)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    binding_version: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ChecklistExecutionRow(Base):
    """Frozen checklist execution opened before item submission."""

    __tablename__ = "checklist_executions"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_checklist_execution_idempotency",
        ),
        CheckConstraint(
            "status IN ('open', 'submitted')",
            name="ck_checklist_execution_status",
        ),
        CheckConstraint(
            "binding_mode IN ('advisory', 'blocking')",
            name="ck_checklist_execution_binding_mode",
        ),
        CheckConstraint(
            "spec_version >= 1 AND binding_version >= 1 AND revision >= 1",
            name="ck_checklist_execution_versions",
        ),
        CheckConstraint(
            "spec_edition IS NULL OR spec_edition >= 1",
            name="ck_checklist_execution_spec_edition",
        ),
        CheckConstraint(
            "length(content_digest) = 64 "
            "AND length(input_digest) = 64 "
            "AND length(template_digest) = 64 "
            "AND length(binding_digest) = 64 "
            "AND length(request_digest) = 64",
            name="ck_checklist_execution_digests",
        ),
        Index(
            "ix_checklist_execution_spec_created",
            "board_id",
            "spec_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_checklist_execution_spec_edition_created",
            "board_id",
            "spec_id",
            "spec_edition",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE"),
        nullable=False,
    )
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL rows predate lifecycle editions and are history-only.
    spec_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("checklist_template_versions.version"),
        nullable=False,
    )
    template_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_version: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    receipt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ChecklistReceiptRow(Base):
    """Immutable server-issued result receipt for one complete execution."""

    __tablename__ = "checklist_receipts"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_checklist_receipt_idempotency",
        ),
        CheckConstraint(
            "source IN ('native', 'legacy_unverified')",
            name="ck_checklist_receipt_source",
        ),
        CheckConstraint(
            "binding_mode IN ('off', 'advisory', 'blocking')",
            name="ck_checklist_receipt_binding_mode",
        ),
        CheckConstraint(
            "spec_version >= 1 AND binding_version >= 1 AND head_revision >= 1",
            name="ck_checklist_receipt_versions",
        ),
        CheckConstraint(
            "spec_edition IS NULL OR spec_edition >= 1",
            name="ck_checklist_receipt_spec_edition",
        ),
        CheckConstraint(
            "length(content_digest) = 64 "
            "AND length(input_digest) = 64 "
            "AND length(template_digest) = 64 "
            "AND length(binding_digest) = 64 "
            "AND length(request_digest) = 64",
            name="ck_checklist_receipt_digests",
        ),
        Index(
            "ix_checklist_receipt_spec_created",
            "board_id",
            "spec_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_checklist_receipt_spec_edition_created",
            "board_id",
            "spec_id",
            "spec_edition",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("checklist_executions.id", ondelete="CASCADE"),
        nullable=True,
    )
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("checklist_template_versions.version"),
        nullable=False,
    )
    template_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_version: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manual_checklist_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    predecessor_receipt_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    head_revision: Mapped[int] = mapped_column(Integer, nullable=False)


class ChecklistItemResultRow(Base):
    """Immutable, ordered per-item result belonging to one receipt."""

    __tablename__ = "checklist_item_results"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('pass', 'fail', 'not_applicable')",
            name="ck_checklist_item_outcome",
        ),
        CheckConstraint(
            "order_index >= 0",
            name="ck_checklist_item_order",
        ),
        UniqueConstraint(
            "receipt_id",
            "order_index",
            name="uq_checklist_item_order",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("checklist_receipts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("checklist_executions.id", ondelete="CASCADE"),
        nullable=True,
    )
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    anchor: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)


class ChecklistExecutionHeadRow(Base):
    """Mutable CAS pointer to the latest receipt for a Spec/phase."""

    __tablename__ = "checklist_execution_heads"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id",
            name="uq_checklist_execution_head_receipt",
        ),
        CheckConstraint(
            "phase = 'spec_validation'",
            name="ck_checklist_execution_head_phase",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_checklist_execution_head_revision",
        ),
    )

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    phase: Mapped[str] = mapped_column(String(32), primary_key=True)
    receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("checklist_receipts.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


for _checklist_immutable_table in (
    ChecklistTemplateVersionRow.__table__,
    ChecklistBindingRow.__table__,
    ChecklistReceiptRow.__table__,
    ChecklistItemResultRow.__table__,
):
    event.listen(
        _checklist_immutable_table,
        "after_create",
        DDL(
            "CREATE TRIGGER IF NOT EXISTS "
            f"trg_{_checklist_immutable_table.name}_immutable_update "
            f"BEFORE UPDATE ON {_checklist_immutable_table.name} "
            "BEGIN SELECT RAISE(ABORT, "
            "'checklist_row_immutable'); END"
        ).execute_if(dialect="sqlite"),
    )


# ============================================================================
# SK-A C7 LEGACY IMPORT EPOCH + QUALITY LIFECYCLE
# ============================================================================


class QualityAssessmentSubjectErasurePermitRow(Base):
    """Transaction-local permit for one governed subject purge."""

    __tablename__ = "quality_assessment_subject_erasure_permits"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "subject_type",
            "subject_id",
            name="uq_quality_subject_erasure_scope",
        ),
        CheckConstraint(
            "subject_type IN ('ideation', 'refinement', 'spec')",
            name="ck_quality_subject_erasure_type",
        ),
    )

    permit_token: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class QualityAssessmentLegacyImportRunRow(Base):
    """Immutable, board-scoped v1 import plan header."""

    __tablename__ = "quality_assessment_legacy_import_runs"
    __table_args__ = (
        CheckConstraint(
            "candidate_count >= 0",
            name="ck_quality_legacy_run_candidate_count",
        ),
        CheckConstraint(
            "length(code_digest) = 64 "
            "AND length(candidate_digest) = 64 "
            "AND length(plan_digest) = 64",
            name="ck_quality_legacy_run_digests",
        ),
    )

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    epoch: Mapped[str] = mapped_column(String(128), primary_key=True)
    cutoff: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rejection_counts_json: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    contract_version: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class QualityAssessmentLegacyImportCandidateRow(Base):
    """Frozen candidate plan with the exact BR15 five-column identity."""

    __tablename__ = "quality_assessment_legacy_import_candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ("board_id", "epoch"),
            (
                "quality_assessment_legacy_import_runs.board_id",
                "quality_assessment_legacy_import_runs.epoch",
            ),
            ondelete="CASCADE",
            name="fk_quality_legacy_candidate_run",
        ),
        UniqueConstraint(
            "board_id",
            "subject_type",
            "subject_id",
            "assessment_kind",
            "epoch",
            name="uq_quality_legacy_candidate_physical_identity",
        ),
        CheckConstraint(
            "ordinal >= 0 AND subject_version >= 1",
            name="ck_quality_legacy_candidate_ord_version",
        ),
        CheckConstraint(
            "(subject_type = 'ideation' AND assessment_kind = 'ambiguity') "
            "OR (subject_type = 'spec' "
            "AND assessment_kind = 'spec_validation')",
            name="ck_quality_legacy_candidate_subject_kind",
        ),
        CheckConstraint(
            "scale_minimum < scale_maximum "
            "AND score >= scale_minimum AND score <= scale_maximum",
            name="ck_quality_legacy_candidate_score",
        ),
        CheckConstraint(
            "length(content_digest) = 64 "
            "AND length(clarification_digest) = 64 "
            "AND length(ruleset_digest) = 64 "
            "AND length(taxonomy_digest) = 64 "
            "AND length(policy_digest) = 64 "
            "AND length(input_digest) = 64 "
            "AND length(legacy_source_digest) = 64",
            name="ck_quality_legacy_candidate_digests",
        ),
        Index(
            "ix_quality_legacy_candidate_subject",
            "board_id",
            "subject_type",
            "subject_id",
        ),
    )

    board_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    epoch: Mapped[str] = mapped_column(String(128), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    assessment_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scale_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scale_minimum: Mapped[float] = mapped_column(Float, nullable=False)
    scale_maximum: Mapped[float] = mapped_column(Float, nullable=False)
    scale_direction: Mapped[str] = mapped_column(String(24), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    clarification_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    ruleset_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    taxonomy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    canonicalization_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    ruleset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    legacy_source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    legacy_source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class QualityAssessmentLegacyImportCheckpointRow(Base):
    """Durable cursor advanced atomically with one candidate resolution."""

    __tablename__ = "quality_assessment_legacy_import_checkpoints"
    __table_args__ = (
        ForeignKeyConstraint(
            ("board_id", "epoch"),
            (
                "quality_assessment_legacy_import_runs.board_id",
                "quality_assessment_legacy_import_runs.epoch",
            ),
            ondelete="CASCADE",
            name="fk_quality_legacy_checkpoint_run",
        ),
        CheckConstraint(
            "processed_count >= 0 AND imported_count >= 0 "
            "AND native_wins_count >= 0 "
            "AND processed_count = imported_count + native_wins_count "
            "AND revision >= 1",
            name="ck_quality_legacy_checkpoint_counts",
        ),
        CheckConstraint(
            "(processed_count = 0 AND cursor_ordinal IS NULL "
            "AND last_subject_type IS NULL AND last_subject_id IS NULL "
            "AND last_assessment_kind IS NULL) "
            "OR (processed_count > 0 "
            "AND cursor_ordinal = processed_count - 1 "
            "AND last_subject_type IS NOT NULL "
            "AND last_subject_id IS NOT NULL "
            "AND last_assessment_kind IS NOT NULL)",
            name="ck_quality_legacy_checkpoint_cursor",
        ),
        CheckConstraint(
            "length(plan_digest) = 64 AND length(candidate_digest) = 64",
            name="ck_quality_legacy_checkpoint_digests",
        ),
    )

    board_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    epoch: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False)
    native_wins_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cursor_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_subject_type: Mapped[str | None] = mapped_column(
        String(24),
        nullable=True,
    )
    last_subject_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    last_assessment_kind: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class QualityAssessmentLegacyImportResolutionRow(Base):
    """Immutable per-candidate imported/native-wins resolution evidence."""

    __tablename__ = "quality_assessment_legacy_import_resolutions"
    __table_args__ = (
        ForeignKeyConstraint(
            ("board_id", "epoch", "ordinal"),
            (
                "quality_assessment_legacy_import_candidates.board_id",
                "quality_assessment_legacy_import_candidates.epoch",
                "quality_assessment_legacy_import_candidates.ordinal",
            ),
            ondelete="CASCADE",
            name="fk_quality_legacy_resolution_candidate",
        ),
        CheckConstraint(
            "resolution IN ('imported', 'native_wins')",
            name="ck_quality_legacy_resolution_kind",
        ),
        CheckConstraint(
            "processed_count >= 1 AND imported_count >= 0 "
            "AND native_wins_count >= 0 "
            "AND processed_count = imported_count + native_wins_count",
            name="ck_quality_legacy_resolution_counts",
        ),
        CheckConstraint(
            "length(request_digest) = 64 "
            "AND length(run_identity_digest) = 64 "
            "AND length(authority_digest) = 64",
            name="ck_quality_legacy_resolution_digests",
        ),
    )

    board_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    epoch: Mapped[str] = mapped_column(String(128), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    resolution: Mapped[str] = mapped_column(String(24), nullable=False)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False)
    native_wins_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Deliberately not an FK: subject purge preserves this durable epoch even
    # when the referenced operational receipt is physically erased.
    receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    history_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outbox_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    run_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    authority_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class QualityAssessmentLegacyImportCompletionRow(Base):
    """Immutable closure marker written only after physical postconditions."""

    __tablename__ = "quality_assessment_legacy_import_completions"
    __table_args__ = (
        ForeignKeyConstraint(
            ("board_id", "epoch"),
            (
                "quality_assessment_legacy_import_runs.board_id",
                "quality_assessment_legacy_import_runs.epoch",
            ),
            ondelete="CASCADE",
            name="fk_quality_legacy_completion_run",
        ),
        CheckConstraint(
            "candidate_count >= 0 AND processed_count = candidate_count "
            "AND imported_count >= 0 AND native_wins_count >= 0 "
            "AND processed_count = imported_count + native_wins_count",
            name="ck_quality_legacy_completion_counts",
        ),
        CheckConstraint(
            "length(plan_digest) = 64 "
            "AND length(candidate_digest) = 64 "
            "AND length(postcondition_digest) = 64",
            name="ck_quality_legacy_completion_digests",
        ),
    )

    board_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    epoch: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False)
    native_wins_count: Mapped[int] = mapped_column(Integer, nullable=False)
    all_candidates_resolved: Mapped[bool] = mapped_column(nullable=False)
    unique_identity_satisfied: Mapped[bool] = mapped_column(nullable=False)
    checkpoint_consistent: Mapped[bool] = mapped_column(nullable=False)
    zero_orphans: Mapped[bool] = mapped_column(nullable=False)
    audit_bundles_consistent: Mapped[bool] = mapped_column(nullable=False)
    epoch_closed: Mapped[bool] = mapped_column(nullable=False)
    postcondition_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class QualityAssessmentLifecycleTransitionRow(Base):
    """Idempotency fence and audit binding for validation/lifecycle moves."""

    __tablename__ = "quality_assessment_lifecycle_transitions"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_quality_lifecycle_board_idempotency",
        ),
        CheckConstraint(
            "action IN ('admit_validation', 'archive', 'cancel', 'restore', 'reopen')",
            name="ck_quality_lifecycle_action",
        ),
        CheckConstraint(
            "subject_type IN ('ideation', 'refinement', 'spec') "
            "AND before_version >= 1 AND after_version >= 1",
            name="ck_quality_lifecycle_subject",
        ),
        CheckConstraint(
            "(before_edition IS NULL OR before_edition >= 1) "
            "AND (after_edition IS NULL OR after_edition >= 1)",
            name="ck_quality_lifecycle_editions",
        ),
        CheckConstraint(
            "length(transition_digest) = 64",
            name="ck_quality_lifecycle_digest",
        ),
        Index(
            "ix_quality_lifecycle_subject",
            "board_id",
            "subject_type",
            "subject_id",
            "occurred_at",
        ),
    )

    transition_digest: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    before_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_status: Mapped[str] = mapped_column(String(50), nullable=False)
    before_archived: Mapped[bool] = mapped_column(nullable=False)
    after_version: Mapped[int] = mapped_column(Integer, nullable=False)
    after_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_status: Mapped[str] = mapped_column(String(50), nullable=False)
    after_archived: Mapped[bool] = mapped_column(nullable=False)
    head_rebuilds_json: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    history_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    outbox_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class QualityAssessmentLifecycleStaleTransitionRow(Base):
    """At-most-once evidence that a restored/reopened head became stale."""

    __tablename__ = "quality_assessment_lifecycle_stale_transitions"
    __table_args__ = (
        CheckConstraint(
            "assessment_kind IN ('ambiguity', 'spec_validation', 'requirement_lint')",
            name="ck_quality_lifecycle_stale_kind",
        ),
        Index(
            "ix_quality_lifecycle_stale_subject",
            "board_id",
            "subject_type",
            "subject_id",
        ),
    )

    stale_transition_key: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    transition_digest: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "quality_assessment_lifecycle_transitions.transition_digest",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    assessment_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


# SK-B3.1: actionable semantic pinpoint contract v2.  These tables are
# deliberately parallel to the established semantic-guideline ledger above:
# an old row is never upgraded in-place or reinterpreted as a v2 row.


class SemanticGuidelineAssessmentV2Row(Base):
    """Immutable, lossless v2 assessment aggregate."""

    __tablename__ = "semantic_guideline_assessments_v2"
    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "idempotency_key",
            name="uq_sg_assessment_v2_idempotency",
        ),
        CheckConstraint(
            "contract_version = 'semantic-guideline-assessment/v2'",
            name="ck_sg_assessment_v2_contract",
        ),
        CheckConstraint(
            "subject_version >= 1 AND binding_revision >= 1 "
            "AND confidence >= 0 AND confidence <= 100",
            name="ck_sg_assessment_v2_ranges",
        ),
        CheckConstraint(
            "validation_edition IS NULL OR validation_edition >= 1",
            name="ck_sg_assessment_v2_validation_edition",
        ),
        CheckConstraint(
            "length(request_digest) = 64 AND length(receipt_digest) = 64 "
            "AND length(subject_content_digest) = 64 "
            "AND length(revision_digest) = 64 "
            "AND length(configuration_digest) = 64",
            name="ck_sg_assessment_v2_digests",
        ),
        Index(
            "ix_sg_assessment_v2_current",
            "board_id",
            "subject_type",
            "subject_id",
            "recorded_at",
            "receipt_id",
        ),
        Index(
            "ix_sg_assessment_v2_edition_current",
            "board_id",
            "subject_type",
            "subject_id",
            "validation_edition",
            "recorded_at",
            "receipt_id",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_edition: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subject_content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    guideline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    assessor_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SemanticGuidelineMetricResultV2Row(Base):
    """Immutable v2 metric result with its complete pinpoint snapshot."""

    __tablename__ = "semantic_guideline_metric_results_v2"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id",
            "metric_id",
            name="uq_sg_metric_result_v2_metric",
        ),
        CheckConstraint(
            "contract_version = 'semantic-metric-result/v2'",
            name="ck_sg_metric_result_v2_contract",
        ),
        CheckConstraint(
            "outcome IN ('pass', 'fail') AND length(result_digest) = 64",
            name="ck_sg_metric_result_v2_shape",
        ),
        Index(
            "ix_sg_metric_result_v2_subject",
            "board_id",
            "subject_type",
            "subject_id",
            "outcome",
            "metric_code",
        ),
    )

    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "semantic_guideline_assessments_v2.receipt_id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_code: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SemanticGuidelineFindingV2Row(Base):
    """Exactly one immutable v2 finding for each failed metric result."""

    __tablename__ = "semantic_guideline_findings_v2"
    __table_args__ = (
        UniqueConstraint(
            "metric_result_id",
            name="uq_sg_finding_v2_metric_result",
        ),
        CheckConstraint(
            "contract_version = 'semantic-metric-finding/v2'",
            name="ck_sg_finding_v2_contract",
        ),
        CheckConstraint(
            "length(finding_digest) = 64 AND length(metric_result_digest) = 64",
            name="ck_sg_finding_v2_digests",
        ),
        Index(
            "ix_sg_finding_v2_queue",
            "board_id",
            "subject_type",
            "subject_id",
            "metric_code",
            "created_at",
        ),
    )

    finding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_result_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "semantic_guideline_metric_results_v2.result_id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_code: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    finding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


# Code Traceability persists only structured attestations submitted to Pulse by
# authenticated agents.  None of these rows is a locator for Community-side
# source access, and no model below implies a Git/filesystem/provider adapter.


class CodeInvestigationRequestRow(Base):
    """Single-use challenge binding for an external agent investigation."""

    __tablename__ = "code_investigation_requests"
    __table_args__ = (
        UniqueConstraint(
            "challenge_token_hash",
            name="uq_code_investigation_request_challenge",
        ),
        UniqueConstraint(
            "board_id",
            "issued_to_actor_id",
            "subject_type",
            "subject_id",
            "subject_version",
            "idempotency_key",
            name="uq_code_investigation_request_idempotency",
        ),
        CheckConstraint(
            "subject_type IN ('refinement', 'spec', 'card')",
            name="ck_code_investigation_request_subject_type",
        ),
        CheckConstraint(
            "subject_version >= 1 AND expected_head_generation >= 0",
            name="ck_code_investigation_request_versions",
        ),
        CheckConstraint(
            "status IN ('open', 'consumed', 'expired', 'revoked')",
            name="ck_code_investigation_request_status",
        ),
        CheckConstraint(
            "length(selector_scope_digest) = 64 "
            "AND length(challenge_token_hash) = 64 "
            "AND length(request_payload_sha256) = 64",
            name="ck_code_investigation_request_digests",
        ),
        CheckConstraint(
            "(status = 'consumed' AND consumed_at IS NOT NULL) OR "
            "(status <> 'consumed' AND consumed_at IS NULL)",
            name="ck_code_investigation_request_consumption",
        ),
        Index(
            "ix_code_investigation_request_subject",
            "board_id",
            "subject_type",
            "subject_id",
            "status",
        ),
        Index(
            "ix_code_investigation_request_expiry",
            "expires_at",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_to_actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    required_capabilities: Mapped[list] = mapped_column(JSON, nullable=False)
    selector_scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_head_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    # Deliberately a logical FK here: SQLite validates board/source lineage in
    # the same transaction as the head CAS and avoids a circular DDL dependency.
    expected_predecessor_receipt_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    canonicalization_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    limits_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    challenge_key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    challenge_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    single_use: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="open",
        server_default=text("'open'"),
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    request_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)


class CodeInvestigationReceiptRow(Base):
    """Immutable accepted observation submitted by an external agent."""

    __tablename__ = "code_investigation_receipts"
    __table_args__ = (
        UniqueConstraint(
            "request_id",
            name="uq_code_investigation_receipt_request",
        ),
        UniqueConstraint(
            "request_id",
            "attestor_actor_id",
            "idempotency_key",
            name="uq_code_investigation_receipt_idempotency",
        ),
        CheckConstraint(
            "subject_type IN ('refinement', 'spec', 'card')",
            name="ck_code_investigation_receipt_subject_type",
        ),
        CheckConstraint(
            "subject_version >= 1 AND generation >= 1",
            name="ck_code_investigation_receipt_versions",
        ),
        CheckConstraint(
            "trust_level IN ('single_attestation', 'corroborated', 'conflicted')",
            name="ck_code_investigation_receipt_trust",
        ),
        CheckConstraint(
            "acceptance_status = 'accepted'",
            name="ck_code_investigation_receipt_acceptance",
        ),
        CheckConstraint(
            "outcome IN ('accessible', 'partial', 'unavailable')",
            name="ck_code_investigation_receipt_outcome",
        ),
        CheckConstraint(
            "(workspace_state_id IS NULL AND declared_dirty IS NULL "
            "AND reproducibility_claim IS NULL "
            "AND fingerprint_algorithm IS NULL AND manifest_digest IS NULL "
            "AND manifest_entry_count IS NULL) OR "
            "(workspace_state_id IS NOT NULL AND declared_dirty IS NOT NULL "
            "AND reproducibility_claim IS NOT NULL "
            "AND fingerprint_algorithm IS NOT NULL "
            "AND manifest_digest IS NOT NULL "
            "AND manifest_entry_count IS NOT NULL)",
            name="ck_code_investigation_receipt_workspace",
        ),
        CheckConstraint(
            "reproducibility_claim IS NULL OR reproducibility_claim IN "
            "('committed', 'worktree_snapshot', 'metadata_only')",
            name="ck_code_investigation_receipt_reproducibility",
        ),
        CheckConstraint(
            "manifest_entry_count IS NULL OR manifest_entry_count >= 0",
            name="ck_code_investigation_receipt_manifest_count",
        ),
        CheckConstraint(
            "omission_count >= 0",
            name="ck_code_investigation_receipt_omission_count",
        ),
        CheckConstraint(
            "length(selector_scope_digest) = 64 "
            "AND (source_identity_digest IS NULL "
            "OR length(source_identity_digest) = 64) "
            "AND (manifest_digest IS NULL OR length(manifest_digest) = 64) "
            "AND length(omission_digest) = 64 "
            "AND length(observation_sha256) = 64 "
            "AND length(payload_sha256) = 64",
            name="ck_code_investigation_receipt_digests",
        ),
        CheckConstraint(
            "expires_at > received_at",
            name="ck_code_investigation_receipt_expiry",
        ),
        Index(
            "ix_code_investigation_receipt_lineage",
            "board_id",
            "source_ref",
            "generation",
        ),
        Index(
            "ix_code_investigation_receipt_subject",
            "board_id",
            "subject_type",
            "subject_id",
            "subject_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "code_investigation_requests.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    attestor_actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    predecessor_receipt_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "code_investigation_receipts.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=True,
    )
    trust_level: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="single_attestation",
        server_default=text("'single_attestation'"),
    )
    acceptance_status: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    capabilities: Mapped[list] = mapped_column(JSON, nullable=False)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_identity_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    canonicalization_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    limits_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    selector_scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    declared_revision: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_state_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    declared_dirty: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reproducibility_claim: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fingerprint_algorithm: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    manifest_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_entry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    omission_manifest: Mapped[list] = mapped_column(JSON, nullable=False)
    omission_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    omission_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tooling: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    observation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)


class CodeInvestigationReceiptRevocationRow(Base):
    """Append-only revocation without rewriting an accepted receipt."""

    __tablename__ = "code_investigation_receipt_revocations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "code_investigation_receipts.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
        unique=True,
    )
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_by: Mapped[str] = mapped_column(String(255), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class CodeInvestigationHeadRow(Base):
    """CAS-protected accepted receipt lineage for one logical source."""

    __tablename__ = "code_investigation_heads"
    __table_args__ = (
        CheckConstraint(
            "generation >= 1 AND revision >= 1",
            name="ck_code_investigation_head_versions",
        ),
        CheckConstraint(
            "state IN ('current', 'conflicted')",
            name="ck_code_investigation_head_state",
        ),
        Index(
            "ix_code_investigation_head_receipt",
            "board_id",
            "current_receipt_id",
        ),
    )

    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        primary_key=True,
    )
    source_ref: Mapped[str] = mapped_column(String(512), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    latest_receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "code_investigation_receipts.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    current_receipt_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "code_investigation_receipts.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=True,
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class CodeEvidenceRow(Base):
    """Immutable evidence content anchored to an accepted receipt."""

    __tablename__ = "code_evidence"
    __table_args__ = (
        UniqueConstraint(
            "supersedes_evidence_id",
            name="uq_code_evidence_supersedes",
        ),
        UniqueConstraint(
            "investigation_receipt_id",
            "submitted_by",
            "idempotency_key",
            name="uq_code_evidence_idempotency",
        ),
        CheckConstraint(
            "parent_type IN ('refinement', 'spec', 'card')",
            name="ck_code_evidence_parent_type",
        ),
        CheckConstraint(
            "((CASE WHEN refinement_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN spec_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN card_id IS NOT NULL THEN 1 ELSE 0 END)) = 1",
            name="ck_code_evidence_exactly_one_parent",
        ),
        CheckConstraint(
            "(parent_type = 'refinement' AND refinement_id IS NOT NULL "
            "AND parent_version IS NOT NULL) OR "
            "(parent_type = 'spec' AND spec_id IS NOT NULL) OR "
            "(parent_type = 'card' AND card_id IS NOT NULL)",
            name="ck_code_evidence_parent_alignment",
        ),
        CheckConstraint(
            "parent_version IS NULL OR parent_version >= 1",
            name="ck_code_evidence_parent_version",
        ),
        CheckConstraint(
            "evidence_type IN ('behavior', 'structure', 'contract', 'test', "
            "'configuration', 'data_model', 'migration', 'dependency', "
            "'runtime_observation')",
            name="ck_code_evidence_type",
        ),
        CheckConstraint(
            "reproducibility_claim IN "
            "('committed', 'worktree_snapshot', 'metadata_only')",
            name="ck_code_evidence_reproducibility",
        ),
        CheckConstraint(
            "selector_kind IN ('symbol', 'file', 'span', "
            "'configuration_key', 'schema_object', 'endpoint', 'test_case')",
            name="ck_code_evidence_selector_kind",
        ),
        CheckConstraint(
            "selector_kind <> 'symbol' OR qualified_symbol IS NOT NULL",
            name="ck_code_evidence_symbol_selector",
        ),
        CheckConstraint(
            "selector_kind <> 'file' OR relative_path IS NOT NULL",
            name="ck_code_evidence_file_selector",
        ),
        CheckConstraint(
            "(snapshot_line_start IS NULL AND snapshot_line_end IS NULL) OR "
            "(snapshot_line_start >= 1 "
            "AND snapshot_line_end >= snapshot_line_start)",
            name="ck_code_evidence_line_span",
        ),
        CheckConstraint(
            "attestation_state IN ('agent_attested', 'agent_attested_worktree')",
            name="ck_code_evidence_attestation_state",
        ),
        CheckConstraint(
            "attestation_basis = 'authenticated_agent_receipt'",
            name="ck_code_evidence_attestation_basis",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active', 'superseded', 'revoked')",
            name="ck_code_evidence_lifecycle",
        ),
        CheckConstraint(
            "(lifecycle_status = 'revoked' AND revocation_reason IS NOT NULL) "
            "OR (lifecycle_status <> 'revoked' AND revocation_reason IS NULL)",
            name="ck_code_evidence_revocation",
        ),
        CheckConstraint(
            "(excerpt IS NULL AND excerpt_sha256 IS NULL) OR "
            "(excerpt IS NOT NULL AND excerpt_sha256 IS NOT NULL)",
            name="ck_code_evidence_excerpt",
        ),
        CheckConstraint(
            "(excerpt_sha256 IS NULL OR length(excerpt_sha256) = 64) "
            "AND (declared_file_blob_sha256 IS NULL "
            "OR length(declared_file_blob_sha256) = 64) "
            "AND length(declared_source_content_sha256) = 64 "
            "AND length(payload_sha256) = 64",
            name="ck_code_evidence_digests",
        ),
        Index(
            "ix_code_evidence_parent",
            "board_id",
            "parent_type",
            "lifecycle_status",
        ),
        Index("ix_code_evidence_path", "source_ref", "relative_path"),
        Index("ix_code_evidence_symbol", "source_ref", "qualified_symbol"),
        Index("ix_code_evidence_receipt", "investigation_receipt_id"),
        Index("ix_code_evidence_refinement", "refinement_id", "parent_version"),
        Index("ix_code_evidence_spec", "spec_id"),
        Index("ix_code_evidence_card", "card_id"),
        Index("ix_code_evidence_superseded", "supersedes_evidence_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    investigation_receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "code_investigation_receipts.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    parent_type: Mapped[str] = mapped_column(String(24), nullable=False)
    refinement_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("refinements.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=True,
    )
    spec_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=True,
    )
    card_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("cards.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=True,
    )
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    declared_revision: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_state_id: Mapped[str] = mapped_column(String(255), nullable=False)
    declared_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reproducibility_claim: Mapped[str] = mapped_column(String(32), nullable=False)
    selector_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    relative_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    language: Mapped[str | None] = mapped_column(String(128), nullable=True)
    symbol_kind: Mapped[str | None] = mapped_column(String(128), nullable=True)
    qualified_symbol: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    symbol_signature: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    snapshot_line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    excerpt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    excerpt_omitted_reason: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    declared_file_blob_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    declared_source_content_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    attestation_state: Mapped[str] = mapped_column(String(32), nullable=False)
    attestation_basis: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default=text("'active'"),
    )
    supersedes_evidence_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("code_evidence.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=True,
    )
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)


class CodeEvidenceSpecLinkRow(Base):
    """Canonical link from immutable evidence into a structured Spec entity."""

    __tablename__ = "code_evidence_spec_links"
    __table_args__ = (
        UniqueConstraint(
            "spec_id",
            "evidence_id",
            "entity_type",
            "entity_id",
            "relation_type",
            name="uq_code_evidence_spec_link_identity",
        ),
        CheckConstraint(
            "entity_type IN ('spec', 'functional_requirement', "
            "'technical_requirement', 'acceptance_criterion', "
            "'business_rule', 'api_contract', 'integration_requirement', "
            "'observability_requirement', 'decision', 'test_scenario')",
            name="ck_code_evidence_spec_link_entity_type",
        ),
        CheckConstraint(
            "relation_type IN ('supports', 'constrains', 'motivates', "
            "'implements', 'tests', 'contradicts')",
            name="ck_code_evidence_spec_link_relation",
        ),
        CheckConstraint(
            "spec_version >= 1 AND "
            "(source_refinement_version IS NULL "
            "OR source_refinement_version >= 1)",
            name="ck_code_evidence_spec_link_versions",
        ),
        CheckConstraint(
            "length(evidence_content_sha256) = 64",
            name="ck_code_evidence_spec_link_digest",
        ),
        Index("ix_code_evidence_spec_link_evidence", "evidence_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    evidence_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("code_evidence.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_refinement_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class CodeEvidenceDispositionRow(Base):
    """Monotonic disposition for inherited evidence not used by a Spec."""

    __tablename__ = "code_evidence_dispositions"
    __table_args__ = (
        CheckConstraint(
            "disposition IN ('not_relevant', 'superseded', 'deferred')",
            name="ck_code_evidence_disposition_value",
        ),
        CheckConstraint(
            "spec_version >= 1",
            name="ck_code_evidence_disposition_version",
        ),
        CheckConstraint(
            "(active = true AND cleared_by IS NULL AND cleared_at IS NULL) OR "
            "(active = false AND cleared_by IS NOT NULL "
            "AND cleared_at IS NOT NULL)",
            name="ck_code_evidence_disposition_clear",
        ),
        Index(
            "uq_code_evidence_disposition_active",
            "spec_id",
            "evidence_id",
            unique=True,
            sqlite_where=text("active = true"),
            postgresql_where=text("active = true"),
        ),
        Index("ix_code_evidence_disposition_evidence", "evidence_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    evidence_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("code_evidence.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    disposition: Mapped[str] = mapped_column(String(24), nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    cleared_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ImplementationTargetRow(Base):
    """Versioned intent describing what an external agent should investigate."""

    __tablename__ = "implementation_targets"
    __table_args__ = (
        CheckConstraint(
            "selector_kind IN ('symbol', 'file', 'glob', 'semantic', 'new_file')",
            name="ck_implementation_target_selector",
        ),
        CheckConstraint(
            "selector_kind <> 'symbol' OR qualified_symbol IS NOT NULL",
            name="ck_implementation_target_symbol",
        ),
        CheckConstraint(
            "selector_kind NOT IN ('file', 'new_file') "
            "OR relative_path_hint IS NOT NULL",
            name="ck_implementation_target_path",
        ),
        CheckConstraint(
            "role IN ('read', 'modify', 'extend', 'create', 'delete', "
            "'test', 'validate')",
            name="ck_implementation_target_role",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active', 'superseded', 'revoked')",
            name="ck_implementation_target_lifecycle",
        ),
        CheckConstraint(
            "source_spec_version >= 1 AND revision >= 1",
            name="ck_implementation_target_versions",
        ),
        CheckConstraint(
            "last_change_reason_sha256 IS NULL "
            "OR length(last_change_reason_sha256) = 64",
            name="ck_implementation_target_change_digest",
        ),
        Index(
            "ix_implementation_target_card",
            "board_id",
            "card_id",
            "lifecycle_status",
        ),
        Index(
            "ix_implementation_target_path",
            "source_ref",
            "relative_path_hint",
        ),
        Index(
            "ix_implementation_target_symbol",
            "source_ref",
            "qualified_symbol",
        ),
        Index("ix_implementation_target_resolution", "current_resolution_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    card_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    selector_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    relative_path_hint: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    language: Mapped[str | None] = mapped_column(String(128), nullable=True)
    symbol_kind: Mapped[str | None] = mapped_column(String(128), nullable=True)
    qualified_symbol: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    symbol_signature: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    source_spec_version: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_evidence_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("code_evidence.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=True,
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default=text("'active'"),
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    # Kept logical to avoid the target/resolution circular DDL dependency.  The
    # store advances this pointer only after inserting the append-only row.
    current_resolution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_change_reason_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ImplementationTargetSpecLinkRow(Base):
    """Many-to-many coverage of structured Spec entities by a target."""

    __tablename__ = "implementation_target_spec_links"
    __table_args__ = (
        UniqueConstraint(
            "target_id",
            "spec_id",
            "entity_type",
            "entity_id",
            name="uq_implementation_target_spec_link",
        ),
        Index("ix_implementation_target_spec_link_spec", "spec_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "implementation_targets.id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("specs.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ImplementationTargetEvidenceLinkRow(Base):
    """Explicit derivation/validation link from a target to immutable evidence."""

    __tablename__ = "implementation_target_evidence_links"
    __table_args__ = (
        UniqueConstraint(
            "target_id",
            "evidence_id",
            "relation_type",
            name="uq_implementation_target_evidence_link",
        ),
        CheckConstraint(
            "relation_type IN ('derived_from', 'validates', 'replaces')",
            name="ck_implementation_target_evidence_relation",
        ),
        Index("ix_implementation_target_evidence_link", "evidence_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "implementation_targets.id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    evidence_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("code_evidence.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    relation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ImplementationTargetResolutionRow(Base):
    """Append-only target resolution claimed by an external agent."""

    __tablename__ = "implementation_target_resolutions"
    __table_args__ = (
        UniqueConstraint(
            "investigation_receipt_id",
            "target_id",
            "target_revision",
            name="uq_implementation_target_resolution_snapshot",
        ),
        UniqueConstraint(
            "investigation_receipt_id",
            "target_id",
            "submitted_by",
            "idempotency_key",
            name="uq_implementation_target_resolution_idempotency",
        ),
        CheckConstraint(
            "receipt_generation >= 1 AND subject_version >= 1 AND target_revision >= 1",
            name="ck_implementation_target_resolution_versions",
        ),
        CheckConstraint(
            "state IN ('resolved', 'moved', 'stale', 'ambiguous', "
            "'missing', 'unavailable')",
            name="ck_implementation_target_resolution_state",
        ),
        CheckConstraint(
            "(resolved_line_start IS NULL AND resolved_line_end IS NULL) OR "
            "(resolved_line_start >= 1 "
            "AND resolved_line_end >= resolved_line_start)",
            name="ck_implementation_target_resolution_lines",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_implementation_target_resolution_confidence",
        ),
        CheckConstraint(
            "candidate_count >= 0 AND candidate_count <= 20",
            name="ck_implementation_target_resolution_candidates",
        ),
        CheckConstraint(
            "(symbol_fingerprint IS NULL OR length(symbol_fingerprint) = 64) "
            "AND (declared_file_blob_sha256 IS NULL "
            "OR length(declared_file_blob_sha256) = 64) "
            "AND length(selector_fingerprint) = 64 "
            "AND length(payload_sha256) = 64",
            name="ck_implementation_target_resolution_digests",
        ),
        Index(
            "ix_implementation_target_resolution_target",
            "target_id",
            "received_at",
        ),
        Index(
            "ix_implementation_target_resolution_workspace",
            "source_ref",
            "workspace_state_id",
        ),
        Index(
            "ix_implementation_target_resolution_path",
            "source_ref",
            "resolved_relative_path",
        ),
        Index(
            "ix_implementation_target_resolution_symbol",
            "source_ref",
            "resolved_qualified_symbol",
        ),
        Index(
            "ix_implementation_target_resolution_receipt",
            "investigation_receipt_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "implementation_targets.id",
            ondelete="CASCADE",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    investigation_receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "code_investigation_receipts.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    receipt_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    declared_revision: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_state_id: Mapped[str] = mapped_column(String(255), nullable=False)
    declared_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    resolved_relative_path: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    resolved_language: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_symbol_kind: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_qualified_symbol: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    resolved_symbol_signature: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    resolved_line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    declared_file_blob_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    selector_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    candidates: Mapped[list | None] = mapped_column(JSON, nullable=True)
    declared_tool_id: Mapped[str] = mapped_column(String(128), nullable=False)
    declared_tool_version: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)


class ImplementationTargetExecutionRecordRow(Base):
    """Append-only post-execution disposition anchored to a result receipt."""

    __tablename__ = "implementation_target_execution_records"
    __table_args__ = (
        UniqueConstraint(
            "result_investigation_receipt_id",
            "target_id",
            "target_revision",
            name="uq_implementation_target_execution_snapshot",
        ),
        UniqueConstraint(
            "result_investigation_receipt_id",
            "target_id",
            "submitted_by",
            "idempotency_key",
            name="uq_implementation_target_execution_idempotency",
        ),
        CheckConstraint(
            "target_revision >= 1",
            name="ck_implementation_target_execution_revision",
        ),
        CheckConstraint(
            "disposition IN ('touched', 'not_touched', 'replaced', "
            "'created', 'deleted', 'superseded')",
            name="ck_implementation_target_execution_disposition",
        ),
        CheckConstraint(
            "disposition <> 'replaced' OR replacement_target_id IS NOT NULL",
            name="ck_implementation_target_execution_replacement",
        ),
        CheckConstraint(
            "length(payload_sha256) = 64",
            name="ck_implementation_target_execution_digest",
        ),
        Index(
            "ix_implementation_target_execution_card",
            "board_id",
            "card_id",
            "received_at",
        ),
        Index(
            "ix_implementation_target_execution_target",
            "target_id",
            "received_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    card_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "implementation_targets.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    target_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    result_investigation_receipt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "code_investigation_receipts.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    disposition: Mapped[str] = mapped_column(String(24), nullable=False)
    result_declared_revision: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    result_workspace_state_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    actual_relative_path: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    actual_qualified_symbol: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    replacement_target_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "implementation_targets.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=True,
    )
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)


class TargetOverlapAcknowledgementRow(Base):
    """Immutable acknowledgement for one exact pair of target resolutions."""

    __tablename__ = "target_overlap_acknowledgements"
    __table_args__ = (
        CheckConstraint(
            "target_a_id <> target_b_id AND resolution_a_id <> resolution_b_id",
            name="ck_target_overlap_ack_distinct",
        ),
        CheckConstraint(
            "disposition IN ('ordered_by_dependency', 'accepted_parallel', "
            "'merged_targets', 'false_positive')",
            name="ck_target_overlap_ack_disposition",
        ),
        Index(
            "ix_target_overlap_ack_targets",
            "board_id",
            "target_a_id",
            "target_b_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    target_a_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "implementation_targets.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    target_b_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "implementation_targets.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    resolution_a_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "implementation_target_resolutions.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    resolution_b_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "implementation_target_resolutions.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class CodeTraceabilityWaiverRow(Base):
    """Monotonic, scoped Code Traceability waiver."""

    __tablename__ = "code_traceability_waivers"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('refinement', 'spec', 'card', 'spec_entity')",
            name="ck_code_traceability_waiver_entity_type",
        ),
        CheckConstraint(
            "scope IN ('code_evidence', 'evidence_linkage', "
            "'implementation_target', 'target_resolution', 'target_overlap')",
            name="ck_code_traceability_waiver_scope",
        ),
        CheckConstraint(
            "reason_code IN ('no_code_change', 'documentation_only', "
            "'manual_process', 'external_source_unavailable', "
            "'conceptual_board', 'runtime_only', 'other')",
            name="ck_code_traceability_waiver_reason",
        ),
        CheckConstraint(
            "(active = true AND cleared_by IS NULL AND cleared_at IS NULL) OR "
            "(active = false AND cleared_by IS NOT NULL "
            "AND cleared_at IS NOT NULL)",
            name="ck_code_traceability_waiver_clear",
        ),
        Index(
            "uq_code_traceability_waiver_active",
            "board_id",
            "entity_type",
            "entity_id",
            "scope",
            unique=True,
            sqlite_where=text("active = true"),
            postgresql_where=text("active = true"),
        ),
        Index(
            "ix_code_traceability_waiver_entity",
            "board_id",
            "entity_type",
            "entity_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    board_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("boards.id", ondelete="CASCADE", onupdate="RESTRICT"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    cleared_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


# ``_migrate_add_human_lifecycle_editions`` executes before ``create_all`` so
# it can repair existing subject tables.  On a brand-new database those tables
# do not exist yet; install the identical guards at table creation time so the
# very first completed schema is already canonical and startup is idempotent.
for _trigger_name, (
    _lifecycle_subject_table_name,
    _lifecycle_trigger_ddl,
) in human_lifecycle_edition_sqlite_trigger_manifest().items():
    event.listen(
        Base.metadata.tables[_lifecycle_subject_table_name],
        "after_create",
        DDL(_lifecycle_trigger_ddl).execute_if(dialect="sqlite"),
    )
