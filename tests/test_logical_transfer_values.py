"""The shared value rules: physical NULL, projection completeness, absent refusal.

Covers frozen-contract item 2 for the shared module. The adapter-local half
(timestamps and vectors) is deliberately not here, because it is deliberately
not in the module.
"""

from __future__ import annotations

import pytest

from okto_pulse.community.adapters.logical_transfer_values import (
    require_projected_columns,
    require_representable,
    scalar_to_logical,
)
from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    LogicalPropertyDef,
    LogicalSchemaError,
)


STRING = LogicalPropertyDef("title", "string")
INT64 = LogicalPropertyDef("rank", "int64")
FLOAT64 = LogicalPropertyDef("score", "float64")
BOOL = LogicalPropertyDef("done", "bool")
TIMESTAMP = LogicalPropertyDef("created_at", "timestamp_us")
VECTOR = LogicalPropertyDef("embedding", "vector", vector_space="x_idx")


class TestPhysicalNullBecomesLogicalNull:
    @pytest.mark.parametrize("declared", [STRING, INT64, FLOAT64, BOOL])
    def test_none_is_null_not_absent(self, declared: LogicalPropertyDef) -> None:
        # The column exists and was projected; nothing was ever "not set".
        assert scalar_to_logical(declared, None) is LOGICAL_NULL

    def test_the_empty_string_is_a_value_not_a_null(self) -> None:
        decoded = scalar_to_logical(STRING, "")
        assert decoded == ""
        assert decoded is not LOGICAL_NULL


class TestScalarsKeepTheirType:
    @pytest.mark.parametrize(
        ("declared", "native"),
        [
            (STRING, "t"),
            (INT64, 7),
            (INT64, 0),
            (FLOAT64, 0.5),
            (BOOL, True),
            (BOOL, False),
        ],
    )
    def test_a_matching_native_passes_through(
        self, declared: LogicalPropertyDef, native: object
    ) -> None:
        assert scalar_to_logical(declared, native) == native

    def test_a_bool_is_not_an_int64(self) -> None:
        # bool subclasses int, so an int64 column would otherwise re-export
        # True as 1.
        with pytest.raises(LogicalSchemaError):
            scalar_to_logical(INT64, True)

    @pytest.mark.parametrize(
        ("declared", "native"),
        [(INT64, "7"), (STRING, 7), (FLOAT64, 1), (BOOL, 1)],
    )
    def test_a_mismatched_native_is_refused(
        self, declared: LogicalPropertyDef, native: object
    ) -> None:
        with pytest.raises(LogicalSchemaError):
            scalar_to_logical(declared, native)


class TestTimestampAndVectorStayWithTheAdapter:
    @pytest.mark.parametrize("declared", [TIMESTAMP, VECTOR])
    def test_the_shared_module_refuses_them(self, declared: LogicalPropertyDef) -> None:
        with pytest.raises(LogicalSchemaError) as caught:
            scalar_to_logical(declared, object())
        assert "adapter" in str(caught.value)


class TestProjectionCompleteness:
    def test_a_complete_projection_is_accepted(self) -> None:
        require_projected_columns(
            [STRING, INT64], {"title": "t", "rank": 1}, owner="T:1"
        )

    def test_a_dropped_column_is_refused(self) -> None:
        # Dropping one would invent `absent` for a column the table stores.
        with pytest.raises(LogicalSchemaError) as caught:
            require_projected_columns([STRING, INT64], {"title": "t"}, owner="T:1")
        assert "rank" in str(caught.value)

    def test_an_undeclared_column_is_refused(self) -> None:
        with pytest.raises(LogicalSchemaError):
            require_projected_columns([STRING], {"title": "t", "ghost": 1}, owner="T:1")


class TestAbsentIsUnrepresentableOnImport:
    def test_a_complete_record_is_accepted(self) -> None:
        require_representable([STRING, INT64], {"title": "t", "rank": 1}, owner="T:1")

    def test_an_absent_property_is_refused(self) -> None:
        # Writing NULL instead would promote "never set" into "set to null".
        with pytest.raises(LogicalSchemaError) as caught:
            require_representable([STRING, INT64], {"title": "t"}, owner="T:1")
        assert "absent" in str(caught.value)

    def test_an_explicit_null_is_representable(self) -> None:
        require_representable(
            [STRING, INT64], {"title": LOGICAL_NULL, "rank": 1}, owner="T:1"
        )
