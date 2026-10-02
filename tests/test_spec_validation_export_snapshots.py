"""Exports preserve native evidence and refuse incompatible pinpoint history."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from okto_pulse.community.adapters.sqlalchemy_entity_export import (
    _validate_spec_validation_pinpoints,
)
from okto_pulse.core.domain.guideline_semantic_v2 import (
    AnchorSnapshot, SemanticAnchorAvailability,
)
from okto_pulse.core.domain.spec_validation import SpecValidationPinpoint


def _pinpoint():
    return SpecValidationPinpoint.from_dict({
        "metric": "clarity", "anchor_type": "field",
        "anchor_ref": "description", "detail": "Make the condition explicit.",
    }).seal(AnchorSnapshot(
        label="Description", excerpt="Original condition", source_version="3",
        availability_at_seal=SemanticAnchorAvailability.AVAILABLE,
    )).to_dict()


def test_export_preserves_native_snapshot_without_reconstruction():
    history = [{"id": "validation", "pinpoints": [_pinpoint()]}]
    original = deepcopy(history)
    assert _validate_spec_validation_pinpoints(history) == original
    assert history == original


@pytest.mark.parametrize("snapshot", [None, {"availability_at_seal": "legacy_unavailable"}])
def test_export_refuses_old_snapshot_without_mutating_history(snapshot):
    pinpoint = _pinpoint()
    pinpoint.pop("anchor_snapshot")
    if snapshot is not None:
        pinpoint["anchor_snapshot"] = snapshot
    history = [{"id": "incompatible", "pinpoints": [pinpoint]}]
    original = deepcopy(history)
    with pytest.raises(ValidationError):
        _validate_spec_validation_pinpoints(history)
    assert history == original
