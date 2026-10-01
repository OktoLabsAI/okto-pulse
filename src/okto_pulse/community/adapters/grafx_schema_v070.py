"""Frozen 0.7.0 recovery contract; existing fingerprints never change in place."""
from okto_pulse.core.kg.logical_transfer import LogicalSchemaError

from .grafx_relationship_layout import PULSE_RELATIONSHIP_LAYOUT, RelationshipLayout
from .grafx_schema_manifest import build_grafx_schema_manifest

V070_FINGERPRINT = '099a8da29e07ccd0002a2000a5c5135d439e83cb15d4a84c8e2a71c001340a32'
POST_V070_CARD_DEPENDENCY_PAIRS = frozenset({
    ('precedes', 'Entity', 'Bug'), ('precedes', 'Bug', 'Entity'), ('precedes', 'Bug', 'Bug'),
})
V070_RELATIONSHIP_LAYOUT = RelationshipLayout(
    (entry.logical_type, entry.from_type, entry.to_type)
    for entry in PULSE_RELATIONSHIP_LAYOUT.entries
    if (entry.logical_type, entry.from_type, entry.to_type) not in POST_V070_CARD_DEPENDENCY_PAIRS
)
V070_MANIFEST = build_grafx_schema_manifest(schema_version='0.7.0', relationship_layout=V070_RELATIONSHIP_LAYOUT)
if V070_MANIFEST.logical_fingerprint != V070_FINGERPRINT:
    raise LogicalSchemaError('frozen 0.7.0 recovery manifest changed')
