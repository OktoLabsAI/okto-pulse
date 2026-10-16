"""Capture identical registry metadata on isolated installed baseline/final pairs.

This is a schema census, NOT permission-filtered tools/list or a flow benchmark.
Usage: qualified-python measure_mcp_registry.py NEW_OUTPUT_DIRECTORY
"""
import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

import tiktoken


async def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.config import configure_settings
    configure_settings(CommunitySettings(data_dir=str(output / "data"),
        database_url="sqlite+aiosqlite:///" + (output / "fixture.db").as_posix(),
        upload_dir=str(output / "uploads"), kg_base_dir=str(output / "kg")))
    from okto_pulse.core.mcp import server
    tools = await server.mcp.get_tools()
    encoder = tiktoken.get_encoding("cl100k_base")
    catalog = [{"name": name, "description": tool.description,
                "parameters": tool.parameters} for name, tool in sorted(tools.items())]
    instructions = server._load_instructions()
    measurements = {}
    for name, value in (("catalog", catalog), ("instructions", instructions)):
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        data = serialized.encode("utf-8")
        (output / (name + ".json")).write_bytes(data)
        measurements[name] = {"bytes": len(data), "tokens_cl100k_base": len(encoder.encode(serialized)),
                              "sha256": hashlib.sha256(data).hexdigest()}
    report = {"scope": "registry metadata only; no credential filtering, lifespan or flow",
              "tools": len(tools), "measurements": measurements,
              "serialization": "UTF-8, sorted keys, compact JSON",
              "tokenizer": "cl100k_base estimate, not model billing",
              "versions": {name: importlib.metadata.version(name) for name in ("tiktoken", "mcp", "fastmcp")}}
    (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
