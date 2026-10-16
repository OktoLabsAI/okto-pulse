"""Release smoke must fail on native domain refusals, not just HTTP errors."""
import json
from pathlib import Path
import runpy

import httpx
import pytest

McpClient = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/release_mcp_smoke.py"))["McpClient"]


def client_for(result, *, status=200, sse=False, response_id=1):
    client = McpClient("http://release.fixture/mcp", "fixture-not-a-credential")
    client.client.close()
    envelope = {"jsonrpc": "2.0", "id": response_id, "result": result}
    def respond(request):
        assert json.loads(request.content)["method"] == "tools/call"
        if sse:
            return httpx.Response(status, text="event: message\ndata: " + json.dumps(envelope) + "\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(status, json=envelope)
    client.client = httpx.Client(transport=httpx.MockTransport(respond))
    return client


@pytest.mark.parametrize("sse", [False, True])
def test_reads_native_success_from_both_http_encodings(sse):
    client = client_for({"isError": False, "structuredContent": {
        "outcome": "success", "data": {"id": "persisted"},
    }}, sse=sse)
    try:
        assert client.call("read", {}) == {"id": "persisted"}
    finally:
        client.client.close()


@pytest.mark.parametrize("case", ["http_denial", "domain_refusal", "wrong_id", "unexpected_success"])
def test_refuses_false_positive_release_results(case):
    failure = {"isError": True, "structuredContent": {
        "outcome": "error", "error_code": "permission_denied", "data": {},
    }}
    success = {"isError": False, "structuredContent": {"outcome": "success", "data": {}}}
    client = client_for(failure if case == "domain_refusal" else success,
                        status=403 if case == "http_denial" else 200,
                        response_id=999 if case == "wrong_id" else 1)
    try:
        with pytest.raises((AssertionError, httpx.HTTPStatusError)):
            client.call("read", {}, expect_error=case == "unexpected_success")
    finally:
        client.client.close()
