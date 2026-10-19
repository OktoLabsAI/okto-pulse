"""Persist and inspect native graph commit provenance for projection recovery."""

from okto_grafx import CommitId, CommitMetadata, Database
from okto_grafx.errors import GrafxUnsupportedOperation


def begin_board_write(database, board_id, operation, *, operation_id=None):
    # Compatibility with structural test doubles, not with another DB provider.
    if not isinstance(database, Database):
        return database.begin("write")
    metadata = CommitMetadata(
        origin="okto-pulse.community",
        reason=operation,
        correlation_id=operation_id,
        attributes={"board_id": board_id},
    )
    try:
        return database.begin("write", metadata=metadata)
    except GrafxUnsupportedOperation as exc:
        # This exact refusal is before a transaction is opened or any effects.
        # Never retry a failed commit or downgrade any other native failure.
        if (
            operation_id is not None
            or exc.details.get("field") != "commit_catalog"
            or exc.details.get("remedy") != "enable_commit_history"
        ):
            raise
        return database.begin("write")


def projection_commit_receipt(
    database: Database,
    *,
    board_id: str,
    operation_id: str,
    after: str | None = None,
    max_commits: int = 100_000,
) -> str | None:
    """Find one durable native commit for an operation in a stable snapshot.

    The caller records ``after`` before starting the graph transaction.  A
    history gap, foreign database token, duplicate ID, or scan budget failure
    refuses recovery instead of reporting a false absence.  This is a commit
    receipt, not an assertion that SQLite finalization has happened.
    """

    if not isinstance(database, Database):
        raise TypeError("native Grafx database required")
    if not board_id or not operation_id:
        raise ValueError("board_id and operation_id are required")
    if type(max_commits) is not int or max_commits < 1:
        raise ValueError("max_commits must be positive")
    cursor = CommitId.parse(after) if after is not None else None
    match = None
    visited = 0
    with database.begin("read") as reader:
        while True:
            page = reader.commit_history(after=cursor, limit=min(1000, max_commits - visited))
            if cursor is not None and cursor.database_uuid != page.database_uuid:
                raise ValueError("projection_commit_database_changed")
            for entry in page.entries:
                metadata = entry.metadata
                if (
                    metadata is not None
                    and metadata.origin == "okto-pulse.community"
                    and metadata.reason == "graph_transaction"
                    and metadata.correlation_id == operation_id
                    and metadata.attributes.get("board_id") == board_id
                ):
                    if match is not None:
                        raise ValueError("projection_commit_duplicate_operation")
                    match = entry.identity.to_token()
            visited += len(page.entries)
            if not page.has_more:
                return match
            if visited >= max_commits or not page.entries:
                raise ValueError("projection_commit_scan_budget_exhausted")
            cursor = page.entries[-1].identity
