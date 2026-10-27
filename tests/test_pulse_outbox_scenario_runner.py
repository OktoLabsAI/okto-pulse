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
        resolved = []
        for selector in selectors:
            path, separator, case = selector.partition("::")
            resolved.append(str(ROOT / path) + (separator + case if separator else ""))
        result = pytest.main(["-q", "-p", "no:cacheprovider", *resolved])
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
            "tests/test_legacy_card_status_normalization.py::test_normalization_event_is_delivered_and_deduplicated",
        )

    def test_sc15_redelivery_and_late_event(self) -> None:
        self._run_cases(
            "tests/test_legacy_card_status_kg_convergence.py::test_legacy_started_card_converges_after_projection",
            "tests/test_legacy_card_status_normalization.py::test_normalization_event_is_delivered_and_deduplicated",
        )

    def test_sc10_full_migration_ordering(self) -> None:
        self._run_cases(
            "tests/test_legacy_card_status_normalization.py::test_two_boards_and_all_card_types_preserve_non_started_rows",
            "tests/test_legacy_card_status_normalization.py::test_large_legacy_column_keeps_order_and_durable_projection_intent",
            "tests/test_legacy_card_status_normalization.py::test_restore_snapshot_is_normalized_without_faking_a_current_move",
            "tests/test_legacy_card_status_normalization.py::test_concurrent_startups_normalize_each_card_once",
            "tests/test_legacy_card_status_normalization.py::test_admitted_current_schema_can_reopen_after_normalization",
        )

    def test_sc12_admission_scale_diagnostics(self) -> None:
        self._run_cases(
            "tests/test_current_relational_schema.py::test_database_without_native_decision_review_kind_is_refused_unchanged",
            "tests/test_current_relational_schema.py::test_lifecycle_never_seeds_incompatible_storage",
            "tests/test_r01c_imp4_schema_lifecycle_orchestrator.py::test_orchestrator_propagates_failure_without_running_later_steps",
            "tests/test_legacy_card_status_normalization.py::test_ten_thousand_cards_commit_without_kg_and_restart_is_noop",
            "tests/test_legacy_card_status_normalization.py::test_failure_after_hundreds_of_cards_rolls_back_entire_board",
        )
