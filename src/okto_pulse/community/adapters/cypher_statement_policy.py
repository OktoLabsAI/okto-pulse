"""One fail-closed authority on what a Cypher statement is allowed to be.

Every Community graph adapter has to answer the same question before it runs a
statement: is this a read, or does it need a writer fence?  That question was
being answered in three places -- the Ladybug transaction, the Grafx executor
and the Grafx Global Discovery runtime -- and the three had already drifted:
the Ladybug side treated the small introspection/vector ``CALL`` allowlist as
readable while the Grafx side fenced it as a write.  A policy that differs by
engine is not a policy, so it lives here now and the adapters consume it.

The module deliberately imports nothing but ``re`` and Core's validator.  It is
loaded on the Grafx path, where Ladybug must not be imported at all, so it can
never reach for an engine package -- and it has no business knowing which
engine is asking.
"""

from __future__ import annotations

import re

from okto_pulse.core.kg.tier_power import validate_cypher_read_only

MUTATING_MATCH_KEYWORDS = ("CREATE", "DELETE", "MERGE", "REMOVE", "SET")

PROVEN_READ_ONLY_CALLS = frozenset(
    {
        "QUERY_VECTOR_INDEX",
        "SHOW_CONNECTION",
        "SHOW_INDEXES",
        "SHOW_TABLES",
        "TABLE_INFO",
    }
)

POTENTIALLY_MUTATING_TOKENS = frozenset(
    {
        "ALTER",
        "ATTACH",
        "BEGIN",
        "CHECKPOINT",
        "COMMIT",
        "COMMENT",
        "COPY",
        "CREATE",
        "DELETE",
        "DETACH",
        "DROP",
        "EXPORT",
        "IMPORT",
        "INSTALL",
        "LOAD",
        "MERGE",
        "REMOVE",
        "RENAME",
        "ROLLBACK",
        "SET",
        "TRUNCATE",
        "USE",
        "VACUUM",
    }
)

_LEADING_TOKEN = re.compile(r"(?:EXPLAIN\s+|PROFILE\s+)?([A-Z_]+)")
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_LITERAL = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
_VECTOR_USE = re.compile(r"(?:VECTOR_INDEX|EMBEDDING)", re.IGNORECASE)


def strip_comments_and_literals(statement: str) -> str:
    """Remove what a token scan must not read as grammar.

    A comment can hide a statement separator and a string literal can contain
    text that looks exactly like DDL, so both are blanked before anything else
    inspects the statement.
    """

    return _LITERAL.sub(" ", _COMMENT.sub(" ", statement))


def statement_is_write(statement: str) -> bool:
    """Fail closed unless the statement is proven to be read-only.

    ``GraphTransactionScope.execute`` is a generic backend contract, so a
    leading-token denylist is not a sufficient final writer fence: comments,
    ``WITH``/``UNWIND`` pipelines and newly-supported backend statements can
    all hide mutations behind a token the adapter does not know yet.

    Core's validator is the primary authority.  The token scan and the
    multistatement check in front of it are defence in depth -- today they
    refuse nothing Core would accept, and that is the point: if Core's grammar
    ever widens, this fence does not widen with it silently.
    """

    normalized = strip_comments_and_literals(statement).strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    if not normalized or ";" in normalized:
        return True

    tokens = re.findall(r"[A-Z_]+", normalized.upper())
    if any(token in POTENTIALLY_MUTATING_TOKENS for token in tokens):
        return True

    # CALL is outside Core's general read-only grammar because procedures can
    # mutate. Keep a deliberately small allowlist for the introspection/vector
    # readers used by the embedded adapters, and fence every other procedure.
    # A second CALL means a nested or chained procedure the allowlist cannot
    # vouch for, so the count check is part of the fence, not an optimisation.
    if "CALL" in tokens:
        call_match = re.match(r"(?is)^\s*CALL\s+([A-Z_]+)", normalized)
        return not (
            call_match is not None
            and tokens.count("CALL") == 1
            and call_match.group(1).upper() in PROVEN_READ_ONLY_CALLS
        )

    try:
        validate_cypher_read_only(statement)
    except Exception:  # noqa: BLE001 - anything unproven is treated as a write
        return True
    return False


def statement_kind(statement: str) -> str:
    """Return a low-cardinality statement class without exposing its text.

    The detailed vocabulary (``MATCH_READ``, ``MATCH_SET``, ``CALL_*``) used by
    the Ladybug transaction's single-writer telemetry.  See
    :func:`leading_statement_token` for the coarse one the Grafx executor
    reports; they are two published vocabularies, not one that drifted, so
    both are kept.
    """

    normalized = statement.lstrip().upper()
    match = _LEADING_TOKEN.match(normalized)
    if match is None:
        return "UNKNOWN"
    first = match.group(1)
    if first == "CALL":
        for operation in (
            "CREATE_VECTOR_INDEX",
            "DROP_VECTOR_INDEX",
        ):
            if operation in normalized:
                return f"CALL_{operation}"
        return "CALL"
    if first != "MATCH":
        return (
            first
            if first
            in {
                "ALTER",
                "BEGIN",
                "CHECKPOINT",
                "COMMIT",
                "CREATE",
                "DROP",
                "INSTALL",
                "LOAD",
                "MERGE",
                "ROLLBACK",
            }
            else "OTHER"
        )
    for keyword in MUTATING_MATCH_KEYWORDS:
        if re.search(rf"\b{keyword}\b", normalized):
            return f"MATCH_{keyword}"
    return "MATCH_READ"


def leading_statement_token(statement: str) -> str:
    """A coarse class for telemetry that never echoes the query text.

    The vocabulary the Grafx executor publishes: the leading keyword alone,
    past an ``EXPLAIN``/``PROFILE`` prefix.
    """

    match = _LEADING_TOKEN.match(statement.lstrip().upper())
    return match.group(1) if match else "UNKNOWN"


def statement_uses_vector(statement: str) -> bool:
    """Whether the statement reaches a vector index or embedding.

    Comments and literals are stripped first, so a query that merely mentions
    "embedding" in a string is not mistaken for one that uses the index.
    """

    return _VECTOR_USE.search(strip_comments_and_literals(statement)) is not None


__all__ = [
    "MUTATING_MATCH_KEYWORDS",
    "POTENTIALLY_MUTATING_TOKENS",
    "PROVEN_READ_ONLY_CALLS",
    "leading_statement_token",
    "statement_is_write",
    "statement_kind",
    "statement_uses_vector",
    "strip_comments_and_literals",
]
