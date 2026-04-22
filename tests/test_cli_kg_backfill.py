"""Unit tests for the `okto-pulse kg backfill` CLI command.

We can't run the full `main()` without a live DB, so we test the helper
serialisers + the dispatcher shape + argparse wiring in isolation.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_SRC = Path(__file__).parent.parent / "src"
CORE_SRC = Path(__file__).parent.parent.parent / "okto_labs_pulse_core" / "src"

# Ensure local source paths win over any site-packages install of okto-pulse
# (the community edition was installed via wheel for runtime but tests need
# the in-tree code so iterate-and-run doesn't require a reinstall).
for p in (str(REPO_SRC), str(CORE_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Purge any pre-loaded okto_pulse.community.cli so the new module below is
# re-imported from the local path.
for mod in list(sys.modules):
    if mod.startswith("okto_pulse.community"):
        del sys.modules[mod]


def test_cli_kg_backfill_help_lists_flags():
    """Smoke test that the subparser is wired and all flags reachable."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'{}'); "
         "from okto_pulse.community.cli import main; main()".format(str(REPO_SRC)),
         "kg", "backfill", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    out = result.stdout
    assert "board_id" in out
    assert "--apply" in out
    assert "--artifact-type" in out
    assert "--json" in out


def test_spec_to_dict_preserves_structured_fields():
    from okto_pulse.community.cli import _spec_to_dict

    class _FakeSpec:
        id = "abc"
        title = "t"
        description = "d"
        context = "## Decisions\n- Use PG"
        functional_requirements = ["FR1"]
        technical_requirements = [{"text": "TR1"}]
        acceptance_criteria = ["AC1"]
        test_scenarios = [{"title": "T", "linked_criteria": ["AC1"]}]
        business_rules = [{"title": "BR1", "rule": "x"}]
        api_contracts = []

    d = _spec_to_dict(_FakeSpec())
    assert d["id"] == "abc"
    assert d["functional_requirements"] == ["FR1"]
    assert d["business_rules"][0]["rule"] == "x"


def test_card_to_dict_handles_bug_fields():
    from okto_pulse.community.cli import _card_to_dict

    class _FakeCard:
        id = "c1"
        title = "bug title"
        description = "bug desc"
        card_type = "bug"
        origin_task_id = "t-1"
        sprint_id = None
        spec_id = "s-1"

    d = _card_to_dict(_FakeCard())
    assert d["card_type"] == "bug"
    assert d["origin_task_id"] == "t-1"


def test_cli_backfill_runs_worker_on_mock_data(monkeypatch, capsys):
    """End-to-end of the worker dispatch + reporting with a mocked loader."""
    from okto_pulse.community import cli as cli_mod
    from okto_pulse.core.kg.workers.deterministic_worker import (
        DeterministicWorker,
    )

    # Short-circuit the async DB loader with in-memory dicts.
    fake_data = {
        "specs": [{
            "id": "00000000-0000-0000-0000-000000000001",
            "title": "Spec 1",
            "description": "",
            "context": "## Decisions\n- Use Redis",
            "functional_requirements": ["FR"],
            "technical_requirements": [],
            "acceptance_criteria": ["AC"],
            "test_scenarios": [
                {"title": "T", "given": "g", "when": "w", "then": "t",
                 "linked_criteria": ["AC"]},
            ],
            "business_rules": [],
            "api_contracts": [],
        }],
        "sprints": [],
        "cards": [],
    }

    def fake_asyncio_run(coro):
        coro.close()  # discard the unawaited coroutine
        return fake_data

    monkeypatch.setattr(cli_mod.asyncio, "run", fake_asyncio_run)
    monkeypatch.setattr(cli_mod, "configure_settings", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(cli_mod, "create_database", lambda *a, **k: None,
                        raising=False)

    class _Args:
        board_id = "67c0dcb7-xxxx"
        apply = False
        artifact_type = ""
        json = True

    try:
        cli_mod.cmd_kg_backfill(_Args())
    except SystemExit:
        pass
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["dry_run"] is True
    assert payload["artifacts"]["spec"] == 1
    assert payload["nodes_total"] > 0
    assert payload["edges_total"] > 0
