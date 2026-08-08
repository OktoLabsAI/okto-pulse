from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from okto_pulse.community.adapters.adapter_provenance import (
    COMMUNITY_ADAPTER_PROVENANCE_REGISTRY,
    audit_community_adapter_provenance,
)
from okto_pulse.community.adapters.hybrid_search import KuzuGraphExpander

from repo_layout import resolve_core_repo


ROOT = Path(__file__).resolve().parents[1]


def test_f13_full_inventory_contains_only_public_core_contracts() -> None:
    report = audit_community_adapter_provenance(ROOT)

    assert report["ok"] is True, report
    assert report["bridge_count"] == 0
    assert report["ledger_count"] == 0
    assert report["registration_count"] == len(
        COMMUNITY_ADAPTER_PROVENANCE_REGISTRY
    )
    assert report["registration_violations"] == ()
    assert report["inventory_count"] > 500
    assert report["inventory_by_classification"] == {
        "public_contract": report["inventory_count"]
    }


def test_f13_nominal_af35_facade_is_removed() -> None:
    facade = (
        ROOT
        / "src"
        / "okto_pulse"
        / "community"
        / "adapters"
        / "af35_sqlalchemy_services.py"
    )

    assert not facade.exists()


def test_f13_direct_adapters_import_without_private_core_service_modules() -> None:
    script = r'''
import builtins
import os
from pathlib import Path

blocked = (
    "okto_pulse.core.repositories.sqlalchemy.resource_gate_service",
    "okto_pulse.core.repositories.sqlalchemy.runtime_settings_service",
    "okto_pulse.core.repositories.sqlalchemy.traceability_read_model",
)
original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if any(name == item or name.startswith(item + ".") for item in blocked):
        raise ImportError("private_core_service_blocked:" + name)
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import

from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import CommunitySqlAlchemyResourceGateAdapter
from okto_pulse.community.adapters.sqlalchemy_runtime_settings_service import AppSetting
from okto_pulse.community.adapters.sqlalchemy_traceability_read_model import build_traceability_report
import okto_pulse.core.ports.permission_policy as core_permission_policy
import okto_pulse.community.adapters.sqlalchemy_resource_gate_service as community_resource_gate

assert CommunitySqlAlchemyResourceGateAdapter.__module__.startswith("okto_pulse.community.")
assert AppSetting.__module__.startswith("okto_pulse.community.")
assert build_traceability_report.__module__.startswith("okto_pulse.community.")
core_origin = Path(core_permission_policy.__file__).resolve()
community_origin = Path(community_resource_gate.__file__).resolve()
assert core_origin.is_relative_to(Path(os.environ["C1_EXPECTED_CORE_SRC"]).resolve()), core_origin
assert community_origin.is_relative_to(Path(os.environ["C1_EXPECTED_COMMUNITY_SRC"]).resolve()), community_origin
assert "site-packages" not in str(core_origin).lower()
assert "site-packages" not in str(community_origin).lower()
print("f13-private-core-isolation-ok")
'''
    core_src = resolve_core_repo(ROOT) / "src"
    env = os.environ.copy()
    env["C1_EXPECTED_CORE_SRC"] = str(core_src)
    env["C1_EXPECTED_COMMUNITY_SRC"] = str(ROOT / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        (
            str(ROOT / "src"),
            str(core_src),
        )
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "f13-private-core-isolation-ok"


def test_f13_graph_expander_requires_explicit_community_composition() -> None:
    with pytest.raises(ValueError, match="cypher_executor_required"):
        KuzuGraphExpander(None)

    executor = object()
    assert KuzuGraphExpander(executor)._executor is executor
