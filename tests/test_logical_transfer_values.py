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


class TestDerivationRefusesAMissingPrimaryKey:
    """The PRIMARY KEY marker is derived, never guessed from column order."""

    def parse(self, ddl: str):
        from okto_pulse.community.adapters.logical_transfer_schema import (
            _parse_node_table,
            _primary_key,
        )

        name, columns = _parse_node_table(ddl)
        return _primary_key(columns, name)

    def test_the_marked_column_is_the_key_even_when_it_is_not_first(self) -> None:
        # Taking columns[0] would answer 'name' here.
        assert (
            self.parse(
                "CREATE NODE TABLE T (name STRING, id STRING PRIMARY KEY, n INT64)"
            )
            == "id"
        )

    def test_a_ddl_without_a_primary_key_is_refused(self) -> None:
        from okto_pulse.community.adapters.logical_transfer_schema import (
            SchemaDerivationError,
        )

        with pytest.raises(SchemaDerivationError) as caught:
            self.parse("CREATE NODE TABLE T (id STRING, name STRING)")
        assert "PRIMARY KEY" in str(caught.value)

    def test_two_primary_keys_are_refused(self) -> None:
        from okto_pulse.community.adapters.logical_transfer_schema import (
            SchemaDerivationError,
        )

        with pytest.raises(SchemaDerivationError):
            self.parse(
                "CREATE NODE TABLE T (id STRING PRIMARY KEY, b STRING PRIMARY KEY)"
            )


class TestTimestampConversionIsExact:
    """A float multiply loses a microsecond at the far end of the range."""

    def convert(self, moment):
        from okto_pulse.community.adapters.ladybug_logical_source import (
            timestamp_to_logical,
        )

        return timestamp_to_logical(moment, owner="T.when").micros

    def test_the_last_representable_instant_keeps_its_microsecond(self) -> None:
        import datetime as dt

        moment = dt.datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=dt.timezone.utc)
        # int(total_seconds() * 1e6) answers 253402300800000000 here: one micro
        # too many, because the product exceeds 2**53.
        assert self.convert(moment) == 253402300799999999

    def test_a_pre_epoch_instant_is_exact(self) -> None:
        import datetime as dt

        moment = dt.datetime(1900, 1, 1, 0, 0, 0, 1, tzinfo=dt.timezone.utc)
        assert self.convert(moment) == -2208988799999999

    def test_the_ordinary_case_is_unchanged(self) -> None:
        import datetime as dt

        moment = dt.datetime(2026, 8, 28, 1, 2, 3, 456789, tzinfo=dt.timezone.utc)
        assert self.convert(moment) == 1787878923456789


class TestVectorPropertyToSpaceIsDerivedNotGuessed:
    """VECTOR_INDEXES names WHICH column is the vector; the type does not."""

    def derive(self, monkeypatch, node_ddl=None, vector_indexes=None):
        from okto_pulse.community.adapters import logical_transfer_schema as mod

        if node_ddl is not None:
            monkeypatch.setattr(mod, "NODE_DDL", node_ddl)
        if vector_indexes is not None:
            monkeypatch.setattr(mod, "VECTOR_INDEXES", vector_indexes)
        return mod.global_logical_schema()

    def test_a_renamed_vector_column_is_refused(self, monkeypatch) -> None:
        from okto_pulse.community.adapters.logical_transfer_schema import (
            SchemaDerivationError,
        )

        # The DDL calls it `other_embedding`; VECTOR_INDEXES still says
        # `embedding`. Trusting the type alone would map it anyway.
        ddl = [
            "CREATE NODE TABLE Entity (id STRING PRIMARY KEY,"
            " other_embedding DOUBLE[384])"
        ]
        with pytest.raises(SchemaDerivationError) as caught:
            self.derive(
                monkeypatch,
                node_ddl=ddl,
                vector_indexes=[("Entity", "entity_embedding_idx", "embedding")],
            )
        assert "embedding" in str(caught.value)

    def test_a_second_vector_column_is_refused(self, monkeypatch) -> None:
        from okto_pulse.community.adapters.logical_transfer_schema import (
            SchemaDerivationError,
        )

        ddl = [
            "CREATE NODE TABLE Entity (id STRING PRIMARY KEY,"
            " embedding DOUBLE[384], extra DOUBLE[384])"
        ]
        with pytest.raises(SchemaDerivationError):
            self.derive(
                monkeypatch,
                node_ddl=ddl,
                vector_indexes=[("Entity", "entity_embedding_idx", "embedding")],
            )

    def test_a_vector_column_with_no_entry_is_refused(self, monkeypatch) -> None:
        from okto_pulse.community.adapters.logical_transfer_schema import (
            SchemaDerivationError,
        )

        ddl = [
            "CREATE NODE TABLE Entity (id STRING PRIMARY KEY, embedding DOUBLE[384])"
        ]
        with pytest.raises(SchemaDerivationError) as caught:
            self.derive(monkeypatch, node_ddl=ddl, vector_indexes=[])
        assert "no VECTOR_INDEXES entry" in str(caught.value)

    def test_an_entry_with_no_ddl_is_refused(self, monkeypatch) -> None:
        from okto_pulse.community.adapters.logical_transfer_schema import (
            SchemaDerivationError,
        )

        ddl = [
            "CREATE NODE TABLE Entity (id STRING PRIMARY KEY, embedding DOUBLE[384])"
        ]
        with pytest.raises(SchemaDerivationError) as caught:
            self.derive(
                monkeypatch,
                node_ddl=ddl,
                vector_indexes=[
                    ("Entity", "entity_embedding_idx", "embedding"),
                    ("Ghost", "ghost_idx", "embedding"),
                ],
            )
        assert "Ghost" in str(caught.value)
