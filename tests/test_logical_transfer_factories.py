"""One agreement per scope, and four ways to hand it to a backend.

These tests care about composition, not about graphs: what a factory builds,
which contract it hands it, and that a scope it does not recognise is refused
rather than defaulted.  The transfer behaviour itself is proved by the adapter
suites, and is not re-proved here.
"""

from __future__ import annotations

import inspect

import pytest

from okto_pulse.community.adapters.grafx_global_discovery import (
    PULSE_GRAFX_GLOBAL_SCHEMA,
)
from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
)
from okto_pulse.community.adapters.logical_transfer_factories import (
    BOARD_RELATIONSHIP_TABLES,
    GLOBAL_RELATIONSHIP_TABLES,
    SCOPE_BOARD,
    SCOPE_GLOBAL_DISCOVERY,
    SCOPES,
    logical_transfer_scope,
)
from okto_pulse.community.adapters.logical_transfer_schema import (
    board_logical_schema,
    global_logical_schema,
)
from okto_pulse.core.kg.logical_transfer import LogicalSchemaError


class _Database:
    """A stand-in: every constructor under test stores the handle, unopened."""


REJECTED_SCOPES = [
    "",
    "Board",
    "BOARD",
    "global",
    "global-discovery",
    "globaldiscovery",
    " board",
    "board ",
    "ladybug",
    None,
    7,
    ("board",),
]


class TestTheScopeContract:
    """Each logical scope names its tables without prescribing a native file."""

    def test_only_the_two_scopes_resolve(self) -> None:
        assert SCOPES == ("board", "global_discovery")
        for scope in SCOPES:
            assert logical_transfer_scope(scope).name == scope

    @pytest.mark.parametrize("scope", REJECTED_SCOPES)
    def test_anything_else_is_refused_not_defaulted(self, scope) -> None:
        with pytest.raises(LogicalSchemaError) as caught:
            logical_transfer_scope(scope)
        assert "unknown logical transfer scope" in str(caught.value)

    def test_board_maps_sixty_nine_tables(self) -> None:
        contract = logical_transfer_scope(SCOPE_BOARD)

        assert contract.schema == board_logical_schema()
        assert len(contract.relationship_tables) == BOARD_RELATIONSHIP_TABLES == 69
        # Exactly the layout authority's own manifest, not a re-derivation.
        assert contract.relationship_tables == {
            (entry.logical_type, entry.from_type, entry.to_type): entry.physical_table
            for entry in PULSE_RELATIONSHIP_LAYOUT.entries
        }

    def test_global_maps_seven_tables(self) -> None:
        contract = logical_transfer_scope(SCOPE_GLOBAL_DISCOVERY)

        assert contract.schema == global_logical_schema()
        assert len(contract.relationship_tables) == GLOBAL_RELATIONSHIP_TABLES == 7
        assert contract.relationship_tables == {
            (rel.logical_relationship, rel.from_table, rel.to_table): rel.name
            for rel in PULSE_GRAFX_GLOBAL_SCHEMA.relationships
        }

    @pytest.mark.parametrize("scope", SCOPES)
    def test_every_layout_the_schema_declares_has_exactly_one_table(
        self, scope: str
    ) -> None:
        contract = logical_transfer_scope(scope)
        declared = {layout.identity for layout in contract.schema.relation_layouts}

        # Both directions: a layout with no table cannot be stored, and a table
        # with no layout is storage the transfer would never read.
        assert set(contract.relationship_tables) == declared
        assert all(contract.relationship_tables.values())

    def test_scopes_do_not_prescribe_a_retired_physical_filename(self) -> None:
        for scope in SCOPES:
            assert not hasattr(logical_transfer_scope(scope), "ladybug_filename")


class _Entries:
    """A layout authority with whatever manifest a test needs."""

    def __init__(self, entries) -> None:
        self.entries = tuple(entries)


class TestDriftIsRefused:
    """A map that no longer matches its schema makes a lossy transfer, not a partial one."""

    def _patched(self, monkeypatch, entries) -> None:
        from okto_pulse.community.adapters import logical_transfer_factories as mod

        monkeypatch.setattr(mod, "PULSE_RELATIONSHIP_LAYOUT", _Entries(entries))

    def test_a_missing_layout_is_refused(self, monkeypatch) -> None:
        self._patched(monkeypatch, PULSE_RELATIONSHIP_LAYOUT.entries[:-1])
        with pytest.raises(LogicalSchemaError) as caught:
            logical_transfer_scope(SCOPE_BOARD)
        assert "frozen census" in str(caught.value)
        assert "68 tables, expected 69" in str(caught.value)

    def test_an_extra_layout_is_refused(self, monkeypatch) -> None:
        entries = PULSE_RELATIONSHIP_LAYOUT.entries
        self._patched(monkeypatch, (*entries, _stowaway()))
        with pytest.raises(LogicalSchemaError) as caught:
            logical_transfer_scope(SCOPE_BOARD)
        assert "70 tables, expected 69" in str(caught.value)

    def test_a_swapped_layout_keeps_the_count_and_is_still_refused(
        self, monkeypatch
    ) -> None:
        # The census alone would pass here: sixty-nine entries, one of them for
        # a relation the schema never declared.
        entries = (*PULSE_RELATIONSHIP_LAYOUT.entries[:-1], _stowaway())
        self._patched(monkeypatch, entries)
        with pytest.raises(LogicalSchemaError) as caught:
            logical_transfer_scope(SCOPE_BOARD)
        assert "does not match the schema's layouts" in str(caught.value)
        assert "unmapped=" in str(caught.value)
        assert "unknown=" in str(caught.value)

    def test_an_empty_physical_table_is_refused(self, monkeypatch) -> None:
        entries = PULSE_RELATIONSHIP_LAYOUT.entries
        first = entries[0]
        nameless = _Entry(first.logical_type, first.from_type, first.to_type, "")
        self._patched(monkeypatch, (nameless, *entries[1:]))
        with pytest.raises(LogicalSchemaError) as caught:
            logical_transfer_scope(SCOPE_BOARD)
        assert "empty physical table" in str(caught.value)


class _Entry:
    def __init__(self, logical_type, from_type, to_type, physical_table) -> None:
        self.logical_type = logical_type
        self.from_type = from_type
        self.to_type = to_type
        self.physical_table = physical_table


def _stowaway() -> _Entry:
    return _Entry("stowaway", "Decision", "Decision", "stowaway__Decision__Decision")


def _rendered_signature(factory) -> str:
    """Render the call signature without annotations, so the shape is the subject."""

    rendered: list[str] = []
    starred = False
    for name, parameter in inspect.signature(factory).parameters.items():
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY and not starred:
            rendered.append("*")
            starred = True
        if parameter.default is inspect.Parameter.empty:
            rendered.append(name)
        else:
            rendered.append(f"{name}={parameter.default!r}")
    return f"({', '.join(rendered)})"
