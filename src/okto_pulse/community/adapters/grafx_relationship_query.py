"""Resolve proven logical relationship endpoint pairs at the adapter boundary.

This is schema-name translation, not a query evaluator. Ambiguous patterns stay
unchanged so the engine can refuse them instead of silently reading a subset.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from okto_pulse.community.adapters.cypher_statement_policy import (
    strip_comments_and_literals,
)
from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
    resolve_relationship_table,
)

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_NODE_ALIAS_LABEL = re.compile(
    rf"\(\s*(?P<alias>{_IDENTIFIER})\s*:\s*(?P<label>{_IDENTIFIER})\b"
)


def _node(side: str) -> str:
    return (
        rf"\(\s*(?P<{side}_alias>{_IDENTIFIER})?"
        rf"(?:\s*:\s*(?P<{side}_label>{_IDENTIFIER}))?"
        r"\s*(?:\{[^{}()]*\}\s*)?\)"
    )


# Lookahead keeps the shared middle node visible in multi-hop patterns.
_RELATIONSHIP = re.compile(
    r"(?="
    + _node("left")
    + r"\s*(?P<left_arrow><-|-)\s*\[\s*"
    + rf"(?:{_IDENTIFIER}\s*)?:\s*(?P<logical_type>{_IDENTIFIER})"
    + r"\s*(?:\{[^{}\[\]]*\}\s*)?\]\s*(?P<right_arrow>->|-)\s*"
    + _node("right")
    + r")"
)
_SCOPE_BOUNDARY = re.compile(r"\b(?:UNION|CALL)\b", re.IGNORECASE)
_PROJECTED_ALIAS = re.compile(rf"\bAS\s+({_IDENTIFIER})\b", re.IGNORECASE)
_WITH_PROJECTION = re.compile(
    r"\bWITH\b(.*?)(?=\b(?:MATCH|OPTIONAL|WHERE|ORDER|LIMIT|SKIP|RETURN|WITH|CREATE|MERGE|SET|DELETE)\b|$)",
    re.IGNORECASE | re.DOTALL,
)


def _identity_aliases(projection: str) -> set[str]:
    """Only bare, top-level WITH items preserve an alias's node identity."""
    projection = re.sub(r"^\s*DISTINCT\b", "", projection, flags=re.IGNORECASE)
    parts: list[str] = []
    start = depth = 0
    for index, character in enumerate(projection):
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(projection[start:index].strip())
            start = index + 1
    parts.append(projection[start:].strip())
    return {
        part for part in parts if re.fullmatch(rf"{_IDENTIFIER}|\*", part) is not None
    }


def translate_logical_relationships(
    statement: str,
    *,
    relationship_pairs: Iterable[tuple[str, str, str]] | None = None,
    resolver: Callable[[str, str, str], str] = resolve_relationship_table,
) -> str:
    """Translate directed, fixed-length relationships with exactly one valid pair.

    Labels may be local or bound elsewhere under the same unambiguous alias. An
    anonymous/untyped endpoint is inferable only when the other endpoint and the
    immutable layout leave exactly one pair. A logical type with only one table
    may also resolve against incompatible labels: native MATCH returns no rows
    and native OPTIONAL MATCH null-extends, as the logical schema requires.
    Alias rebinding, subquery/UNION scope,
    conflicting labels and unsupported syntax never become wildcard evidence.
    Comments, literals, properties and already-physical names remain unchanged.
    """
    code = strip_comments_and_literals(statement)
    if "`" in code:
        return statement
    pairs = (
        tuple(relationship_pairs)
        if relationship_pairs is not None
        else tuple(
            (entry.logical_type, entry.from_type, entry.to_type)
            for entry in PULSE_RELATIONSHIP_LAYOUT.entries
        )
    )
    labels: dict[str, set[str]] = {}
    for match in _NODE_ALIAS_LABEL.finditer(code):
        labels.setdefault(match.group("alias"), set()).add(match.group("label"))
    rebound = set(_PROJECTED_ALIAS.findall(code))
    scoped = _SCOPE_BOUNDARY.search(code) is not None
    projections = [_identity_aliases(item) for item in _WITH_PROJECTION.findall(code)]

    def label(alias: str | None, local: str | None) -> tuple[str | None, bool]:
        if local is not None:
            return local, True
        if alias is None:
            return None, True
        candidates = labels.get(alias, set())
        if alias in rebound or scoped or len(candidates) > 1:
            return None, False
        # A bare WITH projection preserves node identity; expressions and
        # dropped aliases cannot lend their earlier label to a later binding.
        if any(
            alias not in projection and "*" not in projection
            for projection in projections
        ):
            return None, False
        return next(iter(candidates)) if candidates else None, True

    replacements: list[tuple[int, int, str]] = []
    for match in _RELATIONSHIP.finditer(code):
        left, left_valid = label(match.group("left_alias"), match.group("left_label"))
        right, right_valid = label(
            match.group("right_alias"), match.group("right_label")
        )
        if not left_valid or not right_valid or (left is None and right is None):
            continue
        arrows = match.group("left_arrow"), match.group("right_arrow")
        if arrows == ("-", "->"):
            source, target = left, right
        elif arrows == ("<-", "-"):
            source, target = right, left
        else:
            continue
        logical = match.group("logical_type")
        logical_pairs = {pair for pair in pairs if pair[0] == logical}
        candidates = set(
            pair
            for pair in logical_pairs
            if (source is None or pair[1] == source)
            and (target is None or pair[2] == target)
        )
        if not candidates and len(logical_pairs) == 1:
            candidates = logical_pairs
        if len(candidates) != 1:
            continue
        start, end = match.span("logical_type")
        replacements.append((start, end, resolver(*next(iter(candidates)))))
    translated = statement
    for start, end, physical in reversed(replacements):
        translated = translated[:start] + physical + translated[end:]
    return translated
