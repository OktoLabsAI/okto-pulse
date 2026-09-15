"""Capture provider provenance only in stores explicitly opted into history."""

from okto_grafx import CommitMetadata, Database
from okto_grafx.errors import GrafxUnsupportedOperation


def begin_board_write(database, board_id, operation):
    # Compatibility with structural test doubles, not with another DB provider.
    if not isinstance(database, Database):
        return database.begin("write")
    metadata = CommitMetadata(
        origin="okto-pulse.community",
        reason=operation,
        attributes={"board_id": board_id},
    )
    try:
        return database.begin("write", metadata=metadata)
    except GrafxUnsupportedOperation as exc:
        # This exact refusal is before a transaction is opened or any effects.
        # Never retry a failed commit or downgrade any other native failure.
        if (
            exc.details.get("field") != "commit_catalog"
            or exc.details.get("remedy") != "enable_commit_history"
        ):
            raise
        return database.begin("write")
