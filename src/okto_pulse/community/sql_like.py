"""Shared SQL LIKE pattern contract for user-supplied literal searches."""

from __future__ import annotations


SQL_LIKE_ESCAPE = "\\"


def literal_contains_pattern(value: str) -> str:
    """Wrap *value* in wildcards while treating all of its characters literally.

    The escape character must be doubled before escaping LIKE's two wildcard
    characters.  The SQLAlchemy adapter declares the same escape character
    when executing every ``ilike`` application filter.
    """

    escaped = value.replace(SQL_LIKE_ESCAPE, SQL_LIKE_ESCAPE * 2)
    escaped = escaped.replace("%", f"{SQL_LIKE_ESCAPE}%")
    escaped = escaped.replace("_", f"{SQL_LIKE_ESCAPE}_")
    return f"%{escaped}%"
