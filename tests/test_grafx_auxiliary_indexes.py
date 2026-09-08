"""Known extras are fully certified and never substitute for base indexes."""
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters import grafx_schema_evolution as evolution
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from test_grafx_schema_evolution import _index_candidate


def _add_auxiliary(candidate, registered, family):
    table = evolution.PULSE_GRAFX_SCHEMA_MANIFEST.nodes[0]
    table_id = next(t.table_id for t in candidate.catalog.catalog.tables() if t.name == table.name)
    columns, layout, derivation, automatic, name = {
        "source": (("source_artifact_ref",), "hash", "columns", False, f"pulse_source_{table.name.lower()}"),
        "page": (("created_at", "id"), "ordered", "ordered_timestamp_string_v1", False, f"pulse_page_{table.name.lower()}"),
        "identity": ((), "hash", "record_id_u64_v1", True, f"rid_t_{table_id:08x}"),
    }[family]
    positions = tuple(next(i for i, c in enumerate(table.columns) if c.name == n) for n in columns)
    visibility = SimpleNamespace(value="exact")
    layout = SimpleNamespace(value=layout)
    definition = SimpleNamespace(name=name, file="index/g_0000000000000001.idx",
                                 artifact_nonce=1,
                                 table_id=table_id, table_name=table.name, positions=positions,
                                 visibility=visibility, layout=layout, key_derivation=derivation)
    view = SimpleNamespace(name=name, file=definition.file, definition=definition,
                           table_name=table.name, columns=columns, visibility=visibility, layout=layout,
                           key_derivation=derivation, automatic=automatic, generation_state="active",
                           active_nonce=1, stale=False, stale_reason=None, missing_targets=0)
    registered.append(view)
    return view


@pytest.mark.parametrize("family", ["source", "page", "identity"])
def test_known_auxiliary_coexists_with_exact_base(family):
    candidate, registered, _ = _index_candidate()
    _add_auxiliary(candidate, registered, family)
    evolution._require_indexes(candidate, "test")


@pytest.mark.parametrize("family", ["source", "page", "identity"])
@pytest.mark.parametrize("mutation", ["definition", "coverage", "nonce", "automatic", "state", "file"])
def test_known_auxiliary_mutants_are_refused(family, mutation):
    candidate, registered, _ = _index_candidate()
    view = _add_auxiliary(candidate, registered, family)
    if mutation == "definition":
        view.definition.table_id += 1
    elif mutation == "coverage":
        view.missing_targets = 1
    elif mutation == "nonce":
        view.active_nonce = 0
    elif mutation == "automatic":
        view.automatic = not view.automatic
    elif mutation == "state":
        view.generation_state = "building"
    elif mutation == "file":
        view.definition.file = "index/different.idx"
    with pytest.raises(GraphCapabilityUnavailable) as caught:
        evolution._require_indexes(candidate, "test")
    assert "auxiliary_index" in caught.value.details["reason"]


def test_valid_extra_cannot_hide_missing_base_index_with_equal_total():
    candidate, registered, _ = _index_candidate()
    registered.pop(0)
    _add_auxiliary(candidate, registered, "source")
    assert len(registered) == evolution.EXPECTED_INDEX_TOTAL
    with pytest.raises(GraphCapabilityUnavailable) as caught:
        evolution._require_indexes(candidate, "test")
    assert caught.value.details["reason"] == "candidate_index_count_test"


def test_unknown_extra_cannot_enter_by_prefix():
    candidate, registered, _ = _index_candidate()
    view = _add_auxiliary(candidate, registered, "source")
    view.name += "_unknown"
    with pytest.raises(GraphCapabilityUnavailable) as caught:
        evolution._require_indexes(candidate, "test")
    assert caught.value.details["reason"] == "candidate_index_count_test"


def test_duplicate_auxiliary_is_rejected_before_partition():
    candidate, registered, _ = _index_candidate()
    view = _add_auxiliary(candidate, registered, "page")
    registered.append(view)
    with pytest.raises(GraphCapabilityUnavailable) as caught:
        evolution._require_indexes(candidate, "test")
    assert caught.value.details["reason"] == "candidate_duplicate_index_name_test"


@pytest.mark.parametrize("vector", [False, True])
@pytest.mark.parametrize("mutation", [None, "file", "nonce", "state", "bool_nonce"])
def test_base_generation_file_is_exact_and_vector_projection_agrees(vector, mutation):
    candidate, registered, vectors = _index_candidate()
    view = next(v for v in registered if v.name == vectors[0].name) if vector else registered[0]
    view.definition.artifact_nonce = 7
    view.definition.file = view.file = "index/g_0000000000000007.idx"
    view.active_nonce = 7
    view.generation_state = "active"
    if vector:
        vectors[0].file = view.file
    if mutation == "file":
        view.definition.file = view.file = "index/g_0000000000000008.idx"
    elif mutation == "nonce":
        view.active_nonce = 8
    elif mutation == "state":
        view.generation_state = "building"
    elif mutation == "bool_nonce":
        view.definition.artifact_nonce = 1
        view.active_nonce = True
        view.definition.file = view.file = "index/g_0000000000000001.idx"
    if mutation:
        with pytest.raises(GraphCapabilityUnavailable):
            evolution._require_indexes(candidate, "test")
    else:
        evolution._require_indexes(candidate, "test")
