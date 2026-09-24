"""A mismatched installed pair must fail before runtime state is created."""

from importlib import metadata
import json
import subprocess
import sys

import pytest


def test_mismatched_core_refuses_before_creating_runtime_state(monkeypatch):
    from okto_pulse.community import main

    original = metadata.version
    monkeypatch.setattr(metadata, "version", lambda name: "0.0.0" if name == "okto-pulse-core" else original(name))
    monkeypatch.setattr(main, "_ensure_data_dir", lambda _: pytest.fail("incompatible pair reached runtime mutation"))
    with pytest.raises(RuntimeError, match="incompatible_pulse_distribution"):
        main.create_community_app()


def test_matching_installed_pair_is_admitted():
    from okto_pulse.community.adapters.distribution_compatibility import require_compatible_core

    require_compatible_core()


@pytest.mark.parametrize("contract", [None, "pulse-edition-api/old"])
def test_matching_versions_do_not_hide_an_incompatible_core_contract(monkeypatch, contract):
    from okto_pulse.community import main
    from okto_pulse.core.ports import edition_compatibility

    monkeypatch.setattr(edition_compatibility, "EDITION_API_CONTRACT", contract)
    monkeypatch.setattr(main, "_ensure_data_dir", lambda _: pytest.fail("contract mismatch reached runtime mutation"))
    with pytest.raises(RuntimeError, match="core_contract_mismatch"):
        main.create_community_app()


def test_direct_application_factory_cannot_bypass_pair_check(monkeypatch):
    from okto_pulse.community.app import create_app

    original = metadata.version
    monkeypatch.setattr(metadata, "version", lambda name: "0.0.0" if name == "okto-pulse-core" else original(name))
    with pytest.raises(RuntimeError, match="paired_version_mismatch"):
        create_app(None, None, None)


def test_cold_mismatched_import_refuses_before_loading_composition():
    script = '''
import sys
from importlib import metadata
original = metadata.version
metadata.version = lambda name: "0.0.0" if name == "okto-pulse-core" else original(name)
try:
    import okto_pulse.community.main
except RuntimeError as error:
    assert "paired_version_mismatch" in str(error), str(error)
else:
    raise AssertionError("mismatched runtime imported")
assert "okto_pulse.community.adapters.composition" not in sys.modules
assert "okto_pulse.community.adapters.sqlalchemy_database" not in sys.modules
print("refused_before_composition")
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refused_before_composition" in result.stdout


def test_cold_adapter_package_is_lazy_and_preserves_public_exports():
    script = '''
import json, sys
import okto_pulse.community.adapters as adapters
assert "okto_pulse.community.adapters.composition" not in sys.modules
assert len(adapters.__all__) == 71
for name in adapters.__all__:
    assert name in dir(adapters)
    assert getattr(adapters, name) is getattr(adapters, name)
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
assert adapters.CommunityBoardSourceReader is CommunityBoardSourceReader
print(json.dumps({"exports": len(adapters.__all__)}))
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"exports": 71}
