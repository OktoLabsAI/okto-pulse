"""Current catalog and index doubles for exact inventory mutation tests."""
from types import SimpleNamespace
from typing import Any
from okto_pulse.community.adapters.grafx_schema_manifest import PULSE_GRAFX_SCHEMA_MANIFEST

def _index_candidate() -> tuple[SimpleNamespace, list[Any], list[Any]]:
    manifest = PULSE_GRAFX_SCHEMA_MANIFEST
    catalog_tables = [
        SimpleNamespace(name=table.name, table_id=position)
        for position, table in enumerate(manifest.tables, start=1)
    ]
    table_ids = {table.name: table.table_id for table in catalog_tables}
    catalog_spaces = [
        SimpleNamespace(name=space.name, space_id=position)
        for position, space in enumerate(manifest.spaces, start=101)
    ]
    space_ids = {space.name: space.space_id for space in catalog_spaces}
    registered: list[Any] = []

    def add_index(
        name: str,
        table_name: str,
        positions: tuple[int, ...],
        visibility: str,
        key_derivation: str,
    ) -> None:
        file = f"index/{name}.idx"
        enum = SimpleNamespace(value=visibility)
        definition = SimpleNamespace(
            name=name,
            file=file,
            table_id=table_ids[table_name],
            table_name=table_name,
            positions=positions,
            visibility=enum,
            bucket_count=64,
            key_derivation=key_derivation,
        )
        registered.append(
            SimpleNamespace(
                name=name,
                file=file,
                visibility=enum,
                definition=definition,
                stale=False,
                stale_reason=None,
                missing_targets=0,
            )
        )

    for table in (manifest.board_meta, *manifest.nodes):
        primary_position = (
            next(
                index
                for index, column in enumerate(table.columns)
                if column.name == "id"
            )
            if table.name != "BoardMeta"
            else 0
        )
        add_index(
            f"pk_{table.name}",
            table.name,
            (primary_position,),
            "exact",
            "columns",
        )
    for table in manifest.relationships:
        add_index(f"ef_{table.name}", table.name, (0,), "exact", "columns")
        add_index(f"et_{table.name}", table.name, (1,), "exact", "columns")
    vectors: list[Any] = []
    for table, space in zip(manifest.nodes, manifest.spaces, strict=True):
        position = next(
            index
            for index, column in enumerate(table.columns)
            if column.name == "embedding"
        )
        name = f"vector_{table.name}_{space.name}"
        add_index(name, table.name, (position,), "proximity", "vector_digest_v1")
        vectors.append(
            SimpleNamespace(
                name=name,
                file=f"index/{name}.idx",
                space_id=space_ids[space.name],
                space_name=space.name,
                dimension=space.dimension,
                metric_of_space=SimpleNamespace(value=space.metric),
                storage_dtype=space.storage_dtype,
                stale=False,
                stale_reason=None,
            )
        )

    catalog = SimpleNamespace(
        tables=lambda: tuple(catalog_tables),
        spaces=lambda: tuple(catalog_spaces),
    )
    candidate = SimpleNamespace(
        unindexed_tables=(),
        stale_indexes=(),
        catalog=SimpleNamespace(catalog=catalog),
        indexes=SimpleNamespace(indexes=lambda: tuple(registered)),
        vectors=SimpleNamespace(indexes=lambda: tuple(vectors)),
    )
    return candidate, registered, vectors
