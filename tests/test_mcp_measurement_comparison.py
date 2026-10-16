"""ADV-23: fewer tools cannot hide a larger serialized schema."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "comparison", Path(__file__).parents[1] / "scripts" / "compare_mcp_measurements.py")
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def test_fewer_tools_can_cost_more_bytes_and_tokens():
    old = [{"name": name, "parameters": {"type": "object"}} for name in ("first", "second")]
    new = [{"name": "combined", "parameters": {"type": "object", "properties": {
        "field_" + str(i): {"type": "string", "description": "Explicit domain condition " + str(i)}
        for i in range(100)}}}]
    result = comparison.compare_catalogs(old, new)
    assert result["after"]["tools"] < result["before"]["tools"]
    for field in ("bytes", "tokens_cl100k_base"):
        assert result["after"][field] > result["before"][field]


def test_flow_recounts_payloads_and_refuses_missing_responses():
    row = {"session": 1, "method": "tools/call",
           "request": {"payload": {"name": "read"}, "bytes": 0},
           "response": {"payload": {"result": "não ocultar"}, "bytes": 0}}
    result = comparison.measure_flow({"requests": [row, {**row, "session": 2}]})
    assert result["sessions"] == 2 and result["methods"] == {"tools/call": 2}
    assert result["response_bytes"] == 2 * comparison.measure(row["response"]["payload"])["bytes"]
    with pytest.raises(ValueError, match="Incomplete measured request"):
        comparison.measure_flow({"requests": [{k: v for k, v in row.items() if k != "response"}]})
