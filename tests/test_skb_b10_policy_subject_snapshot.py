"""Ratchets for the semantic transition snapshot replacement."""

from __future__ import annotations

import inspect
from importlib.util import find_spec

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.core.domain.guideline_semantic_transition import (
    PolicyTransitionSnapshot,
)


def test_legacy_policy_subject_snapshot_adapter_remains_absent() -> None:
    assert (
        find_spec(
            "okto_pulse.community.adapters.sqlalchemy_policy_subject_snapshot"
        )
        is None
    )


def test_relational_wiring_uses_semantic_transition_snapshot_authority() -> None:
    source = inspect.getsource(CommunityRelationalApplicationAdapter.guideline_policy)
    assert "CommunitySqlAlchemySemanticGuidelineAssessment" in source
    assert "transition_snapshot_resolver" in source
    assert "current_snapshot_resolver" not in source
    assert PolicyTransitionSnapshot.__module__.endswith(
        "guideline_semantic_transition"
    )
