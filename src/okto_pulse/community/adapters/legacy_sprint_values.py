"""Frozen v0.3 storage values for the internal Sprint retirement reader.

These are historical decoders, not a lifecycle registry or transport contract.
They keep the legacy relational mapping readable until its atomic schema cutover;
the historical archive retains original strings. Do not use them for new product
operations or export them from Core. Unknown persisted values remain errors.
"""

from enum import Enum


class HistoricalSprintStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    REVIEW = "review"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class HistoricalSprintLaneType(str, Enum):
    NORMAL = "normal"
    HOTFIX = "hotfix"
