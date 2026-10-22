"""Authenticated runner entry points for normalization outbox scenarios.

The Pulse runner extracts the committed Community and Core revisions before
invoking these methods. Each method executes existing integration tests against
temporary SQLite/KG data; it never touches an operational board.
"""

from __future__ import annotations

import inspect
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PulseOutboxScenarioTests(unittest.TestCase):
    def _run_cases(self, *selectors: str) -> None:
        import pytest

        core_checkout = os.environ.get("OKTO_PULSE_CORE_REPO")
        self.assertIsNotNone(core_checkout, "Immutable paired Core checkout required")
        result = pytest.main(["-q", "-p", "no:cacheprovider", *selectors])
        from okto_pulse.community.adapters.legacy_card_status_normalization import (
            normalize_legacy_card_status,
        )

        self.assertTrue(Path(inspect.getfile(normalize_legacy_card_status)).resolve().is_relative_to(ROOT))
        self.assertEqual(int(result), 0, "Committed outbox scenario checks failed")

    def test_sc13_event_and_targets(self) -> None:
        self._run_cases(
            "tests/test_legacy_card_status_normalization.py::test_normalization_appends_in_old_position_order_and_replays_exactly_once",
            "tests/test_legacy_card_status_normalization.py::test_normalization_event_is_delivered_and_deduplicated",
        )

    def test_sc14_commit_without_kg(self) -> None:
        self._run_cases(
            "tests/test_legacy_card_status_normalization.py::test_normalization_appends_in_old_position_order_and_replays_exactly_once",
            "tests/test_legacy_card_status_normalization.py::test_normalization_rolls_back_board_when_outbox_insert_fails",
        )

    def test_sc15_redelivery_and_late_event(self) -> None:
        self._run_cases(
            "tests/test_legacy_card_status_kg_convergence.py::test_legacy_started_card_converges_after_projection",
            "tests/test_legacy_card_status_normalization.py::test_normalization_event_is_delivered_and_deduplicated",
        )
