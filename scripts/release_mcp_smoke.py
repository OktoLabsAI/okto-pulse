"""Exercise the native release contract against a disposable initialized runtime.

Uses real authenticated MCP HTTP calls, never historical traces or seeded IDs.
API_KEY is read only from the environment and is never written to the report.
This artifact smoke complements the complete test suite; it is not an SDLC gate
waiver or a substitute for validation of a real project's deliverables.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx


class McpClient:
    def __init__(self, url: str, api_key: str):
        self.url = url
        self.sequence = 0
        self.client = httpx.Client(timeout=60, headers={
            "Accept": "application/json, text/event-stream", "X-API-Key": api_key,
        })

    def rpc(self, method: str, params: dict, *, notification=False):
        self.sequence += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = self.sequence
        response = self.client.post(self.url, json=message)
        response.raise_for_status()
        if session := response.headers.get("mcp-session-id"):
            self.client.headers["Mcp-Session-Id"] = session
        if notification:
            return None
        if "text/event-stream" in response.headers.get("content-type", ""):
            messages = [json.loads(line[5:].strip()) for line in response.text.splitlines()
                        if line.startswith("data:") and line[5:].strip()]
            matches = [item for item in messages if item.get("id") == self.sequence]
            assert len(matches) == 1, "MCP response missing or ambiguous"
            envelope = matches[0]
        else:
            envelope = response.json()
        assert envelope.get("id") == self.sequence, "MCP response ID mismatch"
        assert "error" not in envelope, f"MCP protocol failure: {method}"
        return envelope["result"]

    def call(self, name: str, arguments: dict, *, expect_error=False):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        outcome = result["structuredContent"]
        if expect_error:
            assert result.get("isError") is True, f"Expected refusal: {name}"
            assert outcome["outcome"] == "error" and outcome["error_code"]
        else:
            assert not result.get("isError"), f"Unexpected tool refusal: {name}: {outcome.get('error_code')}"
            assert outcome["outcome"] == "success", f"Unexpected outcome: {name}"
        return outcome["data"]

    def pages(self, method: str, field: str):
        cursor = None
        seen = set()
        items = []
        while True:
            page = self.rpc(method, {"cursor": cursor} if cursor else {})
            items.extend(page[field])
            cursor = page.get("nextCursor")
            if not cursor:
                return items
            assert cursor not in seen and len(seen) < 1000, "Nonterminating pagination"
            seen.add(cursor)


def smoke(url: str, api_key: str, expected_version: str):
    client = McpClient(url, api_key)
    try:
        initialized = client.rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "pulse-native-release-smoke", "version": expected_version},
        })
        assert initialized["serverInfo"]["version"] == expected_version
        client.client.headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        client.rpc("notifications/initialized", {}, notification=True)
        names = {tool["name"] for tool in client.pages("tools/list", "tools")}
        assert len(names) == 285
        assert not any("sprint" in name for name in names)
        resources = client.pages("resources/list", "resources")
        manifest = json.loads(client.rpc("resources/read", {
            "uri": "okto-pulse://server-manifest",
        })["contents"][0]["text"])
        assert manifest["server"]["version"] == expected_version
        assert manifest["tool_inventory"]["count"] == len(names)
        assert manifest["tool_inventory"]["aliases"] == {}

        boards = client.call("okto_pulse_list_my_boards", {})["boards"]
        assert len(boards) == 1 and boards[0]["name"] == "My Board", "Smoke requires fresh storage"
        board_id = boards[0]["id"]
        title = "Native release smoke"
        created = client.call("okto_pulse_create_ideation", {
            "board_id": board_id, "title": title,
            "description": "Disposable release contract exercise; no production data.",
            "problem_statement": "Verify the shipped native MCP persistence path.",
            "proposed_approach": "Create and read through authenticated public tools.",
        })
        ideation_id = created["ideation"]["id"]
        scope = {"board_id": board_id, "ideation_id": ideation_id}
        for _ in range(50):
            current = client.call("okto_pulse_get_ideation", scope)
            assert current["id"] == ideation_id and current["title"] == title
        client.call("okto_pulse_ask", {
            "board_id": board_id, "target_type": "ideation", "parent_id": ideation_id,
            "question": "Is this disposable release data?",
        })
        current = client.call("okto_pulse_get_ideation", scope)
        assert any(item["question"] == "Is this disposable release data?" for item in current["qa_items"])
        client.call("okto_pulse_ask", {
            "board_id": board_id, "target_type": "sprint", "parent_id": ideation_id,
            "question": "Retired target must fail closed.",
        }, expect_error=True)
        after = client.call("okto_pulse_get_ideation", scope)
        assert after["qa_items"] == current["qa_items"], "Refused call mutated Q&A"
        return {"schema": "pulse-native-release-smoke/v1", "version": expected_version,
                "tool_count": len(names), "resource_count": len(resources),
                "rpc_count": client.sequence, "persistence_round_trips": 50,
                "native_qa": "passed", "retired_target_refusal": "passed"}
    finally:
        client.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8101/mcp")
    parser.add_argument("--version", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = smoke(args.url, os.environ["API_KEY"], args.version)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
