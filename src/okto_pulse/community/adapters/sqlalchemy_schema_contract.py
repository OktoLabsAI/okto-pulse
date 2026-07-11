"""Canonical contract for the Community-owned relational schema."""

from __future__ import annotations

import hashlib
import json
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


def schema_manifest(metadata: MetaData) -> dict[str, Any]:
    """Return a deterministic DDL-significant metadata representation."""

    tables: list[dict[str, Any]] = []
    for table_name in sorted(metadata.tables):
        table = metadata.tables[table_name]
        constraints = [_constraint_manifest(item) for item in table.constraints]
        constraints.sort(key=lambda item: json.dumps(item, sort_keys=True))
        indexes = [
            {
                "name": index.name,
                "unique": bool(index.unique),
                "expressions": sorted(str(expression) for expression in index.expressions),
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


def schema_contract_sha256(metadata: MetaData) -> str:
    payload = json.dumps(
        schema_manifest(metadata),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# Frozen from the last Core-owned SQLAlchemy metadata before F01 extraction.
LEGACY_CORE_SCHEMA_SHA256 = (
    "e86da78734745e3f1f2fab55a4eaefc5a60d8b6b97053d5d0914cf43609f4d74"
)


__all__ = [
    "LEGACY_CORE_SCHEMA_SHA256",
    "schema_contract_sha256",
    "schema_manifest",
]
