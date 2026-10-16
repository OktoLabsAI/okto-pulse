"""Measure complete cold MCP HTTP bodies from a proved installed wheel pair.

This inventory-only probe is NOT a workflow, authorization or end-to-end network
benchmark. It uses disposable configuration, the real FastMCP HTTP handler and
ASGI transport; it opens no listener and touches no existing Pulse installation.
Run the SAME script in baseline and candidate interpreters after byte proof.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx
import tiktoken


async def measure(resources: list[str]) -> dict:
    from okto_pulse.core import configure_settings
    from okto_pulse.core.mcp import server
    from okto_pulse.core.runtime_context import RuntimeValueRegistry, runtime_value_scope
    from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
    from okto_pulse.community.adapters.resources import register_and_freeze_community_resource_catalog
    from okto_pulse.community.config import CommunitySettings

    encoder = tiktoken.get_encoding("cl100k_base")
    exchanges: list[dict] = []
    sequence = 0
    with TemporaryDirectory(prefix="pulse-inventory-") as data_dir:
        with runtime_value_scope(RuntimeValueRegistry()) as registry:
            configure_settings(CommunitySettings(
                _env_file=None, data_dir=data_dir, kg_base_dir=data_dir,
                kg_embedding_mode="stub", kg_cleanup_enabled=False,
            ))
            transaction = register_and_freeze_community_resource_catalog(registry)
            frozen, identity = transaction.require_frozen_projection()
            started = perf_counter()
            host = CommunityMcpHostProvider().materialize_catalog(
                server.mcp, resource_catalog=frozen, projection_identity=identity,
            )
            transaction.commit()
            materialization_ms = (perf_counter() - started) * 1000
            app = host.http_app(path="/mcp", json_response=True, stateless_http=True)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://inventory.fixture",
                    headers={"Accept": "application/json, text/event-stream"},
                ) as client:

                    async def rpc(method, params=None, *, notification=False):
                        nonlocal sequence
                        sequence += 1
                        message = {"jsonrpc": "2.0", "method": method}
                        if not notification:
                            message["id"] = sequence
                        if params is not None:
                            message["params"] = params
                        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
                        start = perf_counter()
                        response = await client.post("/mcp", content=body,
                                                     headers={"Content-Type": "application/json"})
                        elapsed_ms = (perf_counter() - start) * 1000
                        response.raise_for_status()
                        output = response.content
                        exchanges.append({
                            "method": method, "params": params, "status": response.status_code,
                            "request_bytes": len(body), "response_bytes": len(output),
                            "request_tokens": len(encoder.encode(body.decode(), disallowed_special=())),
                            "response_tokens": len(encoder.encode(output.decode(), disallowed_special=())),
                            "elapsed_ms": elapsed_ms,
                            "request_body": body.decode(), "response_body": output.decode(),
                        })
                        if not output:
                            return {}
                        envelope = response.json()
                        if "error" in envelope:
                            raise RuntimeError(envelope["error"])
                        return envelope.get("result", {})

                    initialized = await rpc("initialize", {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "pulse-inventory-probe", "version": "1"},
                    })
                    client.headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
                    await rpc("notifications/initialized", notification=True)

                    async def pages(method, field):
                        cursor = None
                        seen = set()
                        entries = []
                        while True:
                            result = await rpc(method, {"cursor": cursor} if cursor else {})
                            entries.extend(result[field])
                            cursor = result.get("nextCursor")
                            if not cursor:
                                return entries
                            if cursor in seen or len(seen) >= 1000:
                                raise RuntimeError("nonterminating pagination")
                            seen.add(cursor)

                    tools = await pages("tools/list", "tools")
                    listed = await pages("resources/list", "resources")
                    for uri in resources:
                        await rpc("resources/read", {"uri": uri})

    return {
        "schema": "pulse-cold-mcp-http/v1",
        "scope": "inventory_only_no_grants_or_workflow_exercised",
        "transport": "real_streamable_http_json_over_in_process_asgi_no_network",
        "byte_scope": "exact_HTTP_entity_bodies_excludes_headers_TLS_and_network",
        "tokens": {"library": "tiktoken", "version": version("tiktoken"),
                   "encoding": "cl100k_base", "billing_measurement": False},
        "runtime": {name: version(name) for name in ("okto-pulse-core", "okto-pulse", "fastmcp", "mcp", "httpx")},
        "python": sys.version, "executable": sys.executable,
        "server": initialized["serverInfo"],
        "instructions_bytes": len(initialized.get("instructions", "").encode("utf-8")),
        "instructions_tokens": len(encoder.encode(initialized.get("instructions", ""), disallowed_special=())),
        "tool_count": len(tools), "resource_count": len(listed),
        "tool_names": sorted(item["name"] for item in tools),
        "resources_consumed": resources, "materialization_ms": materialization_ms,
        "totals": {key: sum(item[key] for item in exchanges) for key in
                   ("request_bytes", "response_bytes", "request_tokens", "response_tokens", "elapsed_ms")},
        "exchanges": exchanges,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resource", action="append", default=[])
    args = parser.parse_args()
    proof_bytes = args.provenance.read_bytes()
    proof = json.loads(proof_bytes.decode("utf-8-sig"))
    packages = proof.get("packages", proof)
    for edition in ("core", "community"):
        if packages[edition]["byte_identical"] is not True:
            raise RuntimeError("paired byte proof required")
        module = __import__(f"okto_pulse.{edition}", fromlist=["__file__"])
        if not Path(module.__file__).resolve().is_relative_to(Path(sysconfig.get_paths()["purelib"]).resolve()):
            raise RuntimeError("probe must use installed wheels")
    result = asyncio.run(measure(args.resource or ["okto-pulse://workflows/preflight"]))
    result["provenance_sha256"] = hashlib.sha256(proof_bytes).hexdigest()
    result["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("server", "tool_count", "resource_count", "totals")}))


if __name__ == "__main__":
    main()
