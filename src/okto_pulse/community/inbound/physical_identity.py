"""Community-owned physical identity constraints for inbound adapters.

Core keeps policy identifiers opaque and intentionally allows the widest
portable contract.  The Community relational edition stores board identifiers
in ``VARCHAR(36)`` columns, so every Community inbound transport must narrow
that public contract before application work reaches a unit of work.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints, TypeAdapter


COMMUNITY_BOARD_ID_MAX_LENGTH = 36

CommunityBoardId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=COMMUNITY_BOARD_ID_MAX_LENGTH,
    ),
]

_COMMUNITY_BOARD_ID_ADAPTER = TypeAdapter(CommunityBoardId)


def validate_community_board_id(value: object) -> str:
    """Validate one board id against the Community physical boundary."""

    return _COMMUNITY_BOARD_ID_ADAPTER.validate_python(value)
