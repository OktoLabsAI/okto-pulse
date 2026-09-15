"""Bounded native read composition without changing the caller's snapshot.

Only read-only native query budget refusals split a batch. Each result is fully
materialized before any row escapes its attempt. Corruption, cancellation, stale
authority and an irreducible single-query refusal propagate unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from okto_grafx.errors import GrafxQueryBudgetExceeded


READ_BRANCH_BATCH_SIZE = 16
READ_FRONTIER_BATCH_SIZE = 128


def _result_rows(result: object) -> Sequence[Sequence[Any]]:
    rows = getattr(result, "rows", None)
    if not isinstance(rows, (tuple, list)):
        raise ValueError("Grafx returned an invalid row collection")
    if any(not isinstance(row, (tuple, list)) for row in rows):
        raise ValueError("Grafx returned an invalid result row")
    return rows


def read_branches(
    reader: Any,
    queries: Sequence[str],
    parameters: Mapping[str, Any],
) -> Iterator[tuple[int, Sequence[Any]]]:
    """Compose trusted same-heading RETURN branches in original branch order."""

    def run(start: int, stop: int) -> Iterator[tuple[int, Sequence[Any]]]:
        if stop - start == 1:
            for row in _result_rows(reader.execute(queries[start], parameters)):
                yield start, row
            return
        query = " UNION ALL ".join(
            f"{queries[index]}, {index} AS __pulse_branch"
            for index in range(start, stop)
        )
        try:
            result = reader.execute(query, parameters)
        except GrafxQueryBudgetExceeded:
            middle = (start + stop) // 2
            yield from run(start, middle)
            yield from run(middle, stop)
            return
        groups: dict[int, list[Sequence[Any]]] = {
            index: [] for index in range(start, stop)
        }
        for row in _result_rows(result):
            if not row or type(row[-1]) is not int or row[-1] not in groups:
                raise ValueError("Invalid native read branch identity")
            groups[row[-1]].append(row[:-1])
        for index, rows in groups.items():
            for row in rows:
                yield index, row

    for start in range(0, len(queries), READ_BRANCH_BATCH_SIZE):
        yield from run(start, min(start + READ_BRANCH_BATCH_SIZE, len(queries)))


def read_frontier(
    reader: Any,
    *,
    single_query: str,
    batch_query: str,
    identities: Sequence[str],
) -> Iterator[Sequence[Any]]:
    """Read each selected BFS input once, retaining input order and duplicates."""

    def run(start: int, stop: int) -> Iterator[Sequence[Any]]:
        if stop - start == 1:
            yield from _result_rows(
                reader.execute(single_query, {"current_id": identities[start]})
            )
            return
        parameters = {
            "frontier": [
                {"id": identities[index], "ordinal": index}
                for index in range(start, stop)
            ]
        }
        try:
            result = reader.execute(batch_query, parameters)
        except GrafxQueryBudgetExceeded:
            middle = (start + stop) // 2
            yield from run(start, middle)
            yield from run(middle, stop)
            return
        groups: dict[int, list[Sequence[Any]]] = {
            index: [] for index in range(start, stop)
        }
        for row in _result_rows(result):
            if not row or type(row[0]) is not int or row[0] not in groups:
                raise ValueError("Invalid native frontier identity")
            groups[row[0]].append(row[1:])
        for rows in groups.values():
            yield from rows

    for start in range(0, len(identities), READ_FRONTIER_BATCH_SIZE):
        yield from run(start, min(start + READ_FRONTIER_BATCH_SIZE, len(identities)))
