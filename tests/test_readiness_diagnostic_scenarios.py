"""Community-primary entry points for the paired readiness diagnostic scenarios.

The local authenticated runner binds test evidence to its primary source. These
selectors keep Community as the primary revision while executing the shared
scenario definitions from the immutable paired Core checkout.
"""

import os
from pathlib import Path
import runpy
import unittest


class CommunityReadinessDiagnosticScenarios(unittest.TestCase):
    def _run_core_scenario(self, name: str) -> None:
        core = os.environ.get("OKTO_PULSE_CORE_REPO")
        if not core:
            raise AssertionError("paired Core checkout is required")
        os.environ["OKTO_PULSE_COMMUNITY_REPO"] = str(Path(__file__).resolve().parents[1])
        source = Path(core) / "tests" / "test_readiness_native_scenarios.py"
        scenarios = runpy.run_path(str(source))["PersistedReadinessNativeScenarios"]
        getattr(scenarios(name), name)()

    def test_sc25_instrumentation_samples_and_parity(self) -> None:
        self._run_core_scenario("test_sc25_instrumentation_samples_and_parity")

    def test_sc26_overload_and_resource_isolation(self) -> None:
        self._run_core_scenario("test_sc26_overload_and_resource_isolation")

    def test_sc27_safe_diagnostics(self) -> None:
        self._run_core_scenario("test_sc27_safe_diagnostics")
