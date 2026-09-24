"""Frozen 0.6.0 contract for offline recovery, never a runtime write mode."""

from okto_pulse.core.kg.logical_transfer import LogicalSchemaError

from .grafx_relationship_layout import PULSE_RELATIONSHIP_LAYOUT, RelationshipLayout
from .grafx_schema_manifest import build_grafx_schema_manifest

V060_FINGERPRINT = '3ab6faf0fd8a7fe3694ed7ddd336faa97a6b4af4a1626aafe20c75b0922b2bbe'
# Closed prospective delta for existing relational IR/OR -> TR declarations.
# Exclusion preserves history; it does not authorize these pairs at runtime.
POST_V060_REQUIREMENT_PAIRS = frozenset({
    ('derives_from', 'Requirement', 'Constraint'),
    ('derives_from', 'Constraint', 'Constraint'),
})
V060_RELATIONSHIP_LAYOUT = RelationshipLayout(
    (entry.logical_type, entry.from_type, entry.to_type)
    for entry in PULSE_RELATIONSHIP_LAYOUT.entries
    if (entry.logical_type, entry.from_type, entry.to_type) not in POST_V060_REQUIREMENT_PAIRS
)
V060_MANIFEST = build_grafx_schema_manifest(
    schema_version='0.6.0', relationship_layout=V060_RELATIONSHIP_LAYOUT,
)
if V060_MANIFEST.logical_fingerprint != V060_FINGERPRINT:
    raise LogicalSchemaError('frozen 0.6.0 recovery manifest changed')
