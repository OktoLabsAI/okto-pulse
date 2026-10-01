"""Read-only manifests of the guards installed for the current SQLite format."""

from importlib.resources import files
import json
import re


def _trigger_manifest(prefix: str) -> dict[str, tuple[str, str]]:
    definitions = json.loads(
        files(__package__).joinpath("current_relational_objects.json").read_text(encoding="utf-8")
    )
    return {
        item["name"]: (item["table"], item["sql"])
        for item in definitions
        if item["kind"] == "trigger" and item["name"].startswith(prefix + "_")
    }


def global_discovery_source_revision_trigger_manifest() -> dict[str, tuple[str, str]]:
    return _trigger_manifest("trg_global_discovery_source_revision")


def normalize_global_discovery_source_revision_trigger_sql(raw: object) -> str:
    """Canonicalize SQLite trigger DDL for bounded integrity comparison."""
    return re.sub(r'[\s"`;\[\]]+', "", str(raw or "").lower())


def semantic_pinpoint_v2_sqlite_trigger_manifest() -> dict[str, tuple[str, str]]:
    return _trigger_manifest("trg_semantic_pinpoint_v2")


def code_traceability_sqlite_trigger_manifest() -> dict[str, tuple[str, str]]:
    return _trigger_manifest("trg_code_traceability_v1")
