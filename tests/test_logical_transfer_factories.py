"""One agreement per scope, and four ways to hand it to a backend.

These tests care about composition, not about graphs: what a factory builds,
which contract it hands it, and that a scope it does not recognise is refused
rather than defaulted.  The transfer behaviour itself is proved by the adapter
suites, and is not re-proved here.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from okto_pulse.community.adapters.grafx_global_discovery import (
    PULSE_GRAFX_GLOBAL_SCHEMA,
)
from okto_pulse.community.adapters.grafx_logical_sink import (
    CommunityGrafxLogicalCandidateSink,
)
from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
)
from okto_pulse.community.adapters.ladybug_logical_sink import (
    LadybugLogicalCandidateSink,
)
from okto_pulse.community.adapters.ladybug_logical_source import (
    LadybugLogicalSnapshotSource,
)
from okto_pulse.community.adapters.logical_transfer_factories import (
    BOARD_RELATIONSHIP_TABLES,
    GLOBAL_RELATIONSHIP_TABLES,
    SCOPE_BOARD,
    SCOPE_GLOBAL_DISCOVERY,
    SCOPES,
    logical_transfer_scope,
    make_grafx_logical_sink,
    make_grafx_logical_source,
    make_ladybug_logical_sink,
    make_ladybug_logical_source,
)
from okto_pulse.community.adapters.logical_transfer_grafx import (
    CommunityGrafxLogicalSnapshotSource,
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
    """Two scopes exist, and each one names its own tables and its own file."""

    def test_only_the_two_scopes_resolve(self) -> None:
        assert SCOPES == ("board", "global_discovery")
        for scope in SCOPES:
            assert logical_transfer_scope(scope).name == scope

    @pytest.mark.parametrize("scope", REJECTED_SCOPES)
    def test_anything_else_is_refused_not_defaulted(self, scope) -> None:
        with pytest.raises(LogicalSchemaError) as caught:
            logical_transfer_scope(scope)
        assert "unknown logical transfer scope" in str(caught.value)

    def test_board_maps_sixty_nine_tables_and_names_graph_lbug(self) -> None:
        contract = logical_transfer_scope(SCOPE_BOARD)

        assert contract.schema == board_logical_schema()
        assert len(contract.relationship_tables) == BOARD_RELATIONSHIP_TABLES == 69
        assert contract.ladybug_filename == "graph.lbug"
        # Exactly the layout authority's own manifest, not a re-derivation.
        assert contract.relationship_tables == {
            (entry.logical_type, entry.from_type, entry.to_type): entry.physical_table
            for entry in PULSE_RELATIONSHIP_LAYOUT.entries
        }

    def test_global_maps_seven_tables_and_names_discovery_lbug(self) -> None:
        contract = logical_transfer_scope(SCOPE_GLOBAL_DISCOVERY)

        assert contract.schema == global_logical_schema()
        assert len(contract.relationship_tables) == GLOBAL_RELATIONSHIP_TABLES == 7
        assert contract.ladybug_filename == "discovery.lbug"
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

    def test_the_two_scopes_do_not_share_a_file_name(self) -> None:
        names = {logical_transfer_scope(scope).ladybug_filename for scope in SCOPES}
        assert len(names) == len(SCOPES)


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


class TestTheFactoriesBuildTheRightThing:
    """Each backend gets the same contract, and the knobs survive the trip."""

    @pytest.mark.parametrize("scope", SCOPES)
    def test_the_ladybug_source_reads_the_scope_schema(self, scope: str) -> None:
        database = _Database()
        source = make_ladybug_logical_source(database, scope=scope)

        assert isinstance(source, LadybugLogicalSnapshotSource)
        assert source._database is database
        assert source._schema == logical_transfer_scope(scope).schema

    @pytest.mark.parametrize(
        ("scope", "filename"),
        [(SCOPE_BOARD, "graph.lbug"), (SCOPE_GLOBAL_DISCOVERY, "discovery.lbug")],
    )
    def test_the_ladybug_sink_writes_the_file_the_runtime_resolves(
        self, tmp_path: Path, scope: str, filename: str
    ) -> None:
        candidate = tmp_path / "candidate"
        sink = make_ladybug_logical_sink(candidate, scope=scope)

        assert isinstance(sink, LadybugLogicalCandidateSink)
        assert sink.candidate_path == candidate
        assert sink.database_path == candidate / filename
        assert sink._expected_schema == logical_transfer_scope(scope).schema

    @pytest.mark.parametrize("scope", SCOPES)
    def test_the_grafx_source_gets_the_scope_map_and_the_default_batch(
        self, scope: str
    ) -> None:
        contract = logical_transfer_scope(scope)
        database = _Database()
        source = make_grafx_logical_source(database, scope=scope)

        assert isinstance(source, CommunityGrafxLogicalSnapshotSource)
        assert source._database is database
        assert source._schema == contract.schema
        assert dict(source._relationship_tables) == dict(contract.relationship_tables)
        assert source._scan_batch_size == 500
        assert source._temporary_parent is None

    def test_the_grafx_source_keeps_the_knobs_it_was_given(
        self, tmp_path: Path
    ) -> None:
        source = make_grafx_logical_source(
            _Database(),
            scope=SCOPE_BOARD,
            scan_batch_size=17,
            temporary_parent=tmp_path,
        )

        assert source._scan_batch_size == 17
        assert source._temporary_parent == tmp_path

    @pytest.mark.parametrize("scope", SCOPES)
    def test_the_grafx_sink_gets_the_scope_map_and_the_default_batch(
        self, tmp_path: Path, scope: str
    ) -> None:
        contract = logical_transfer_scope(scope)
        candidate = tmp_path / "candidate"
        sink = make_grafx_logical_sink(candidate, scope=scope)

        assert isinstance(sink, CommunityGrafxLogicalCandidateSink)
        assert sink._expected_schema == contract.schema
        assert dict(sink._relationship_input) == dict(contract.relationship_tables)
        assert sink._max_batch_size == 500
        # Passing nothing leaves the adapter's own default in place; the
        # factory composes, it does not quietly re-specify the backend.
        assert dict(sink._connect_options) == {"page_size": 8192}
        assert sink._temporary_parent is None

    def test_the_grafx_sink_keeps_the_knobs_it_was_given(self, tmp_path: Path) -> None:
        sink = make_grafx_logical_sink(
            tmp_path / "candidate",
            scope=SCOPE_GLOBAL_DISCOVERY,
            max_batch_size=23,
            connect_options={"page_size": 16384},
            temporary_parent=tmp_path,
        )

        assert sink._max_batch_size == 23
        assert dict(sink._connect_options) == {"page_size": 16384}
        assert sink._temporary_parent == tmp_path

    def test_the_two_ends_of_one_scope_agree_on_the_map(self, tmp_path: Path) -> None:
        # The whole point: a source and a sink built for the same scope cannot
        # disagree about which table stores which layout.
        for scope in SCOPES:
            source = make_grafx_logical_source(_Database(), scope=scope)
            sink = make_grafx_logical_sink(tmp_path / f"cand_{scope}", scope=scope)
            assert dict(source._relationship_tables) == dict(sink._relationship_input)
            assert source._schema == sink._expected_schema


class TestAnInvalidScopeBuildsNothing:
    """The refusal happens before a candidate path or a handle is touched."""

    @pytest.mark.parametrize("scope", ["", "Board", "global", None, 7])
    def test_every_factory_refuses_before_constructing(
        self, tmp_path: Path, scope
    ) -> None:
        candidate = tmp_path / "candidate"
        for call in (
            lambda: make_ladybug_logical_source(_Database(), scope=scope),
            lambda: make_ladybug_logical_sink(candidate, scope=scope),
            lambda: make_grafx_logical_source(_Database(), scope=scope),
            lambda: make_grafx_logical_sink(candidate, scope=scope),
        ):
            with pytest.raises(LogicalSchemaError):
                call()
        assert not candidate.exists()


class TestTheSignaturesAreFrozen:
    """These four are the API other milestones will be written against."""

    @pytest.mark.parametrize(
        ("factory", "expected"),
        [
            (make_ladybug_logical_source, "(database, *, scope)"),
            (make_ladybug_logical_sink, "(candidate_root, *, scope)"),
            (
                make_grafx_logical_source,
                "(database, *, scope, scan_batch_size=500, temporary_parent=None)",
            ),
            (
                make_grafx_logical_sink,
                "(candidate_path, *, scope, max_batch_size=500, "
                "connect_options=None, temporary_parent=None)",
            ),
        ],
    )
    def test_the_signature_is_exactly_the_agreed_one(self, factory, expected) -> None:
        assert _rendered_signature(factory) == expected

    @pytest.mark.parametrize(
        "factory",
        [
            make_ladybug_logical_source,
            make_ladybug_logical_sink,
            make_grafx_logical_source,
            make_grafx_logical_sink,
        ],
    )
    def test_scope_is_keyword_only(self, factory) -> None:
        parameter = inspect.signature(factory).parameters["scope"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


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
