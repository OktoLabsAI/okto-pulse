"""Regression coverage for the bounded architecture finding boot sweep."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


SOURCE_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "okto_pulse" / "community"
)


@pytest.mark.parametrize("module_name", ("app.py", "main.py"))
def test_startup_architecture_backfill_only_processes_missing_designs(
    module_name: str,
) -> None:
    tree = ast.parse((SOURCE_ROOT / module_name).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            getattr(node.func, "id", None) == "backfill_architecture_finding_runs"
            or getattr(node.func, "attr", None)
            == "backfill_architecture_finding_runs"
        )
    ]

    assert calls, f"{module_name} must wire the architecture finding boot sweep"
    for call in calls:
        keyword = next(
            (item for item in call.keywords if item.arg == "only_missing"),
            None,
        )
        assert keyword is not None
        assert isinstance(keyword.value, ast.Constant)
        assert keyword.value.value is True
