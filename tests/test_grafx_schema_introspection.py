"""M-PULSE-3A contract tests for Grafx node-property introspection."""

from __future__ import annotations

from pathlib import Path

import okto_grafx
import pytest
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
)

from okto_pulse.community.adapters.grafx_schema_introspection import (
    list_node_properties,
)


@pytest.fixture
def grafx_database(tmp_path: Path):
    """Open an empty durable Grafx database for one test."""

    database = okto_grafx.connect(tmp_path / "grafx-node-properties")
    try:
        yield database
    finally:
        database.close()


def _create_entity_table(database) -> None:
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Entity("
            "id STRING, title STRING, active BOOL, score DOUBLE, PRIMARY KEY(id))"
        )


def test_known_node_properties_keep_catalog_order_without_mutation(
    grafx_database,
) -> None:
    _create_entity_table(grafx_database)
    catalog_before = grafx_database.catalog

    assert list_node_properties(grafx_database, "Entity") == (
        "id",
        "title",
        "active",
        "score",
    )
    assert grafx_database.catalog == catalog_before


@pytest.mark.parametrize("node_type", ("BoardMeta", "supports", "CustomNode"))
def test_type_outside_pulse_vocabulary_returns_empty_without_backend_access(
    node_type: str,
) -> None:
    class BackendMustNotBeRead:
        @property
        def catalog(self):
            message = "unknown node types must not consult Grafx"
            raise AssertionError(message)

    assert list_node_properties(BackendMustNotBeRead(), node_type) == ()


def test_known_node_with_missing_table_fails_closed(grafx_database) -> None:
    with pytest.raises(GraphCapabilityUnavailable) as captured:
        list_node_properties(grafx_database, "Entity")

    assert captured.value.details["backend"] == "okto_grafx"
    assert captured.value.details["operation"] == "list_node_properties"
    assert captured.value.details["backend_error_type"] == "GrafxConfigurationError"


def test_known_node_backed_by_relationship_table_fails_closed(grafx_database) -> None:
    with grafx_database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE Source(id STRING, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE Target(id STRING, PRIMARY KEY(id))")
        schema.execute(
            "CREATE REL TABLE Entity(FROM Source TO Target, confidence DOUBLE)"
        )

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        list_node_properties(grafx_database, "Entity")

    assert captured.value.details == {
        "backend": "okto_grafx",
        "operation": "list_node_properties",
        "node_type": "Entity",
        "table_kind": "rel",
    }


def test_closed_database_failure_uses_existing_typed_error_mapping(
    tmp_path: Path,
) -> None:
    database = okto_grafx.connect(tmp_path / "closed-grafx")
    _create_entity_table(database)
    database.close()

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        list_node_properties(database, "Entity")

    assert captured.value.details["backend"] == "okto_grafx"
    assert captured.value.details["operation"] == "list_node_properties"


def test_persisted_catalog_returns_same_properties_after_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "reopened-grafx"
    database = okto_grafx.connect(database_path)
    _create_entity_table(database)
    expected = list_node_properties(database, "Entity")
    database.close()

    reopened = okto_grafx.connect(database_path)
    try:
        assert list_node_properties(reopened, "Entity") == expected
    finally:
        reopened.close()
