"""Community lifespan must wire the Core KG events port.

The Community app replaces core's default lifespan, so it must replay the
Local First reader registration and shutdown hook for ``kg_events_hub``.
"""

from __future__ import annotations

import ast
from pathlib import Path


def test_combined_lifespan_registers_and_shutdowns_kg_events_hub() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "okto_pulse"
        / "community"
        / "main.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    combined = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "combined_lifespan"
    )
    calls = {
        getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        for call in ast.walk(combined)
        if isinstance(call, ast.Call)
    }

    assert "register_community_kg_events_reader" in calls
    assert "shutdown_kg_events_hub" in calls
