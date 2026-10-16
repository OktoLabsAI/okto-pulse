"""Exports preserve native evidence and refuse incompatible pinpoint history."""

from copy import deepcopy

from spec_validation_fixtures import native_validation

import pytest
from pydantic import ValidationError

from okto_pulse.community.adapters.sqlalchemy_entity_export import (
    _validate_spec_validation_history,
)
from okto_pulse.core.domain.guideline_semantic_v2 import (
    AnchorSnapshot, SemanticAnchorAvailability,
)
from okto_pulse.core.domain.spec_validation import SpecValidationPinpoint


def _pinpoint():
    return SpecValidationPinpoint.from_dict({
        "metrics": ["clarity"], "kind": "problem", "severity": "medium",
        "excerpt": "Original condition", "recommendation": "Specify the condition.", "anchor_type": "field",
        "anchor_ref": "description", "detail": "Make the condition explicit.",
    }).seal(AnchorSnapshot(
        label="Description", excerpt="Original condition", source_version="3",
        availability_at_seal=SemanticAnchorAvailability.AVAILABLE,
    )).to_dict()


def test_export_preserves_native_snapshot_without_reconstruction():
    history = [native_validation(pinpoints=[_pinpoint()])]
    original = deepcopy(history)
    assert _validate_spec_validation_history(history) == original
    assert history == original


@pytest.mark.parametrize("snapshot", [None, {"availability_at_seal": "legacy_unavailable"}])
def test_export_refuses_old_snapshot_without_mutating_history(snapshot):
    pinpoint = _pinpoint()
    pinpoint.pop("anchor_snapshot")
    if snapshot is not None:
        pinpoint["anchor_snapshot"] = snapshot
    history = [native_validation("incompatible", pinpoints=[pinpoint])]
    original = deepcopy(history)
    with pytest.raises(ValidationError):
        _validate_spec_validation_history(history)
    assert history == original


@pytest.mark.parametrize("field", ["score", "summary", "completeness", "general_justification"])
def test_export_refuses_old_record_fields_without_converting_them(field):
    history = [{**native_validation(), field: None}]
    original = deepcopy(history)
    with pytest.raises(ValidationError):
        _validate_spec_validation_history(history)
    assert history == original
