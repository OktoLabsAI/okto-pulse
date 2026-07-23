"""SQLAlchemy database models."""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
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
    "consolidation_audit",
    "consolidation_dead_letter",
    "consolidation_queue",
    "global_update_outbox",
    "ideations",
    "kg_cognitive_sources",
    "kg_cognitive_source_revisions",
    "kuzu_node_refs",
    "refinements",
    "specs",
    "sprints",
    "stories",
)
GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID = "_global"
GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION = "gdsr-fence-v2"
GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION = "gdsr-trigger-manifest-v3"
GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX = "trg_global_discovery_source_revision"


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
    # Cancellation justification (ITEM 17): required when moving to 'cancelled';
    # reopening (cancelled -> any other status) clears all three fields.
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    # Cancellation justification (ITEM 17): required when moving to 'cancelled';
    # reopening (cancelled -> any other status) clears all three fields.
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
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

    refinement: Mapped["Refinement"] = relationship(
        "Refinement", back_populates="qa_items"
    )


# ============================================================================
# SPEC
# ============================================================================


class Spec(Base):
    """Spec model - represents a specification that drives card creation."""

    __tablename__ = "specs"

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
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[SpecStatus] = mapped_column(
        SpecStatusType(), default=SpecStatus.DRAFT, nullable=False
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
        DateTime(timezone=True), server_default=func.now()
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
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    # Task validations: [{id, card_id, board_id, reviewer_id, confidence, confidence_justification,
    # estimated_completeness, completeness_justification, estimated_drift, drift_justification,
    # general_justification, recommendation, outcome, threshold_violations, created_at}]
    validations: Mapped[list | None] = mapped_column(JSON, nullable=True)

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
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

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
        DateTime(timezone=True), server_default=func.now()
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
    # Audit identity, intentionally not an FK. Board deletion must not erase
    # or cascade into the append-only propagation cluster. The write adapter
    # revalidates the live board-scoped spec/card immediately before its CAS.
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
            "'refresh_snapshot', 'grandfather')",
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
            "'refresh_snapshot', 'grandfather')",
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
