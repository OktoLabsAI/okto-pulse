"""Canonical contract for the Community-owned relational schema."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, MetaData


def _server_default(column: Any) -> str | None:
    default = column.server_default
    return str(default.arg) if default is not None else None


def _constraint_manifest(constraint: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": type(constraint).__name__,
        "name": constraint.name,
        "columns": sorted(column.name for column in constraint.columns),
    }
    if isinstance(constraint, CheckConstraint):
        item["sqltext"] = str(constraint.sqltext)
    if isinstance(constraint, ForeignKeyConstraint):
        references = [
            {
                "local": element.parent.name,
                "remote": element.target_fullname,
                "ondelete": element.ondelete,
                "onupdate": element.onupdate,
            }
            for element in constraint.elements
        ]
        references.sort(key=lambda reference: json.dumps(reference, sort_keys=True))
        item["references"] = references
    return item


def schema_manifest(
    metadata: MetaData,
    *,
    table_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a deterministic DDL-significant metadata representation."""

    selected = set(table_names) if table_names is not None else set(metadata.tables)
    unknown = selected.difference(metadata.tables)
    if unknown:
        raise ValueError(f"unknown_schema_tables:{','.join(sorted(unknown))}")
    tables: list[dict[str, Any]] = []
    for table_name in sorted(selected):
        table = metadata.tables[table_name]
        constraints = [_constraint_manifest(item) for item in table.constraints]
        constraints.sort(key=lambda item: json.dumps(item, sort_keys=True))
        indexes = [
            {
                "name": index.name,
                "unique": bool(index.unique),
                # Index expression order is DDL-significant: ``(a, b)`` and
                # ``(b, a)`` serve different access paths and must never hash
                # to the same governed schema contract.
                "expressions": [str(expression) for expression in index.expressions],
            }
            for index in table.indexes
        ]
        indexes.sort(key=lambda item: json.dumps(item, sort_keys=True))
        tables.append(
            {
                "name": table_name,
                "columns": [
                    {
                        "name": column.name,
                        "type": str(column.type),
                        "nullable": bool(column.nullable),
                        "primary_key": bool(column.primary_key),
                        "server_default": _server_default(column),
                    }
                    for column in table.columns
                ],
                "constraints": constraints,
                "indexes": indexes,
            }
        )
    return {"tables": tables}


def schema_contract_sha256(
    metadata: MetaData,
    *,
    table_names: Iterable[str] | None = None,
) -> str:
    payload = json.dumps(
        schema_manifest(metadata, table_names=table_names),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# One complete Community-owned schema for fresh v0.4.0 installations.
# Pin every table, column, constraint and index; no inherited/extension split.
CURRENT_COMMUNITY_SCHEMA_SHA256 = (
    "6dcbf913aea0ae223ab0f884f8df4fb7d1a84b304e5d7364f6b85973f658c27e"
)


__all__ = [
    "CURRENT_COMMUNITY_SCHEMA_SHA256",
    "schema_contract_sha256",
    "schema_manifest",
]
