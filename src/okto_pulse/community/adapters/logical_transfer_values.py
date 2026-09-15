"""The small part of physical/logical value handling that is genuinely shared.

Deliberately narrow.  Timestamps and vectors are NOT converted here, because
the two backends do not agree on them: Grafx wants its own ``Timestamp`` and a
``VectorValue`` bound to a physical space, while Ladybug hands back different
natives entirely.  A single neutral converter for those would be an abstraction
invented for symmetry, and each adapter would end up unpicking it.  So this
module carries only what really is the same on both sides: scalars, the
physical-``None``-to-``LOGICAL_NULL`` rule, and the two completeness checks that
keep ``absent`` from being forged or silently dropped.

The two checks are opposites of each other and both matter:

exporting
    A fixed-schema row HAS every declared column.  Projecting fewer would
    invent ``absent`` for a column the database physically stores, and the wire
    format treats ``absent`` as a different fact from ``NULL``.

importing
    A logical record that OMITS a declared property cannot be written to a
    fixed-schema table at all, because there is no state there that means
    "never set".  Writing NULL instead would silently promote one fact into
    another, so the import refuses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    LogicalPropertyDef,
    LogicalSchemaError,
    LogicalValue,
)


SHARED_SCALAR_TYPES: frozenset[str] = frozenset({"bool", "int64", "float64", "string"})

_PYTHON_SCALARS: dict[str, type] = {
    "int64": int,
    "float64": float,
    "string": str,
}


def scalar_to_logical(declared: LogicalPropertyDef, native: object) -> LogicalValue:
    """Convert one physical SCALAR, mapping physical ``None`` to ``LOGICAL_NULL``.

    Refuses timestamp and vector on purpose: those belong to the adapter that
    knows the backend's own types, and routing them through here would be the
    neutral converter this module exists to avoid.
    """

    if declared.type not in SHARED_SCALAR_TYPES:
        raise LogicalSchemaError(
            "timestamp and vector conversion belongs to the adapter",
            detail=f"{declared.name}: {declared.type}",
        )
    if native is None:
        # The physical row stores a value slot with nothing in it. That is NULL,
        # never absent -- the column exists and the export projected it.
        return LOGICAL_NULL
    if declared.type == "bool":
        if native is not True and native is not False:
            raise _wrong_native(declared, native)
        return native
    expected = _PYTHON_SCALARS[declared.type]
    # bool first: it is a subclass of int, so an int64 column would otherwise
    # accept True and re-export it as 1.
    if declared.type == "int64" and (native is True or native is False):
        raise _wrong_native(declared, native)
    if type(native) is not expected:
        raise _wrong_native(declared, native)
    return native


def require_projected_columns(
    declared: Sequence[LogicalPropertyDef],
    properties: Mapping[str, LogicalValue],
    *,
    owner: str,
) -> None:
    """Refuse an exported record that dropped a column the schema declares.

    A fixed-schema table has every column in every row, so a missing one is the
    exporter's omission, not the source's ``absent``.
    """

    missing = [prop.name for prop in declared if prop.name not in properties]
    if missing:
        raise LogicalSchemaError(
            "a fixed-schema export must project every declared column",
            detail=f"{owner} omitted {','.join(sorted(missing))}",
        )
    unknown = [name for name in properties if name not in {p.name for p in declared}]
    if unknown:
        raise LogicalSchemaError(
            "the export carries a column the schema does not declare",
            detail=f"{owner}: {','.join(sorted(unknown))}",
        )


def require_representable(
    declared: Sequence[LogicalPropertyDef],
    properties: Mapping[str, LogicalValue],
    *,
    owner: str,
) -> None:
    """Refuse a logical record whose ABSENT property a fixed schema cannot hold.

    This is the typed refusal the contract requires on import.  The alternative
    -- writing NULL for the missing property -- would turn "never set" into
    "set to null", which the wire format deliberately keeps apart.  The caller
    aborts the candidate on this; it must never finalize.
    """

    absent = [prop.name for prop in declared if prop.name not in properties]
    if absent:
        raise LogicalSchemaError(
            "a fixed-schema table cannot represent an absent property",
            detail=f"{owner} is missing {','.join(sorted(absent))}",
        )


def _wrong_native(declared: LogicalPropertyDef, native: object) -> LogicalSchemaError:
    return LogicalSchemaError(
        "physical value does not match its declared logical type",
        detail=(
            f"{declared.name}: expected {declared.type}, got {type(native).__name__}"
        ),
    )


__all__ = [
    "SHARED_SCALAR_TYPES",
    "require_projected_columns",
    "require_representable",
    "scalar_to_logical",
]
