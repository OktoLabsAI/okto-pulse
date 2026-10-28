"""Authenticated Community entry points for Started migration verification."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PulseCommunityStatusScenarioTests(unittest.TestCase):
    def _run_cases(self, *selectors: str) -> None:
        from okto_pulse.community.adapters import (
            card_status_migration_read,
            card_status_permission_reconciliation,
        )

        for module in (card_status_migration_read, card_status_permission_reconciliation):
            self.assertTrue(Path(inspect.getfile(module)).resolve().is_relative_to(ROOT))
        result = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", *selectors],
            cwd=ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-4000:])

    def test_sc08_permission_presets(self) -> None:
        self._run_cases(
            "tests/test_card_status_permission_reconciliation.py::test_custom_same_name_direct_grants_and_board_ceilings_are_converted",
            "tests/test_card_status_permission_reconciliation.py::test_concurrent_permission_startups_do_not_duplicate_conversion",
            "tests/test_card_status_permission_reconciliation.py::test_permission_conversion_failure_rolls_back_before_readiness",
        )

    def test_sc09_migration_provenance(self) -> None:
        self._run_cases(
            "tests/test_card_status_migration_return.py::test_migrated_return_expires_after_governed_reentry",
            "tests/test_card_status_migration_return.py::test_migrated_card_can_return_once_before_new_execution",
        )
