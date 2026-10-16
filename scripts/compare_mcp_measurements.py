"""Recount frozen MCP payloads with one tokenizer; never infer flow equivalence."""
from collections import Counter
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

import tiktoken


def measure(value):
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = serialized.encode("utf-8")
    return {"bytes": len(raw), "tokens_cl100k_base": len(
        tiktoken.get_encoding("cl100k_base").encode(serialized)),
        "sha256": hashlib.sha256(raw).hexdigest()}


def compare_catalogs(before, after):
    old = {row["name"]: row for row in before}
    new = {row["name"]: row for row in after}
    if len(old) != len(before) or len(new) != len(after):
        raise ValueError("Duplicate tool name")
    return {
        "before": {"tools": len(before), **measure(before)},
        "after": {"tools": len(after), **measure(after)},
        "by_tool": {name: {"before": measure(old[name]) if name in old else None,
                           "after": measure(new[name]) if name in new else None}
                    for name in sorted(old.keys() | new.keys())},
    }


def measure_flow(capture):
    # Recount retained bodies instead of trusting counts from another tokenizer.
    totals = Counter()
    methods = Counter()
    sessions = set()
    for row in capture["requests"]:
        methods[row["method"]] += 1
        sessions.add(row["session"])
        for direction in ("request", "response"):
            if direction not in row:
                raise ValueError("Incomplete measured request")
            measured = measure(row[direction]["payload"])
            for field in ("bytes", "tokens_cl100k_base"):
                totals[direction + "_" + field] += measured[field]
    return {"sessions": len(sessions), "methods": dict(methods), **totals}


def main():
    before, after, flow, output = map(Path, sys.argv[1:])
    inputs = {}

    def read(path):
        raw = path.read_bytes()
        inputs[str(path)] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    result = {
        "format": "pulse-mcp-payload-comparison/v1",
        "catalog": compare_catalogs(read(before / "catalog.json"), read(after / "catalog.json")),
        "instructions": {"before": measure(read(before / "instructions.json")),
                         "after": measure(read(after / "instructions.json"))},
        "current_flow": measure_flow(read(flow)),
        "input_sha256": inputs,
        "tokenizer": "cl100k_base; reproducible payload token counts, not model billing",
        "tiktoken_version": importlib.metadata.version("tiktoken"),
        "serialization": "UTF-8, sorted keys, compact JSON",
        "limits": ["Frozen catalog comparison is not equivalent full-flow comparison.",
                   "Current flow includes only executed fixture requests and responses.",
                   "No inference of savings from fewer tools or different populations."],
    }
    with output.open("x", encoding="utf-8") as target:
        json.dump(result, target, ensure_ascii=False, indent=2)
        target.write("\n")
    print(json.dumps({key: result[key] for key in ("instructions", "current_flow")}))


if __name__ == "__main__":
    main()
