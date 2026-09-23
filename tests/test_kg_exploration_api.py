"""HTTP contracts/denials and real Grafx retrieval through optional Core ports."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from okto_pulse.community.api import kg_exploration as api
from okto_pulse.core.kg.interfaces.graph_errors import GraphUnavailable
from test_grafx_ranked_search import ranked  # noqa: F401
from test_grafx_graph_store import BOARD_ID, real_store  # noqa: F401

ROOT = f"/api/v1/kg/boards/{BOARD_ID}/exploration"


@pytest.fixture
def client(monkeypatch):
    provider = Mock()
    registry = SimpleNamespace(
        ranked_graph_search=provider,
        graph_history=provider,
        graph_analytics=provider,
        embedding_provider=SimpleNamespace(encode=lambda _: [1.0] + [0.0] * 383),
    )

    monkeypatch.setattr(api, "get_kg_registry", lambda: registry)
    monkeypatch.setattr(api.kg, "_require_kg_operation", AsyncMock())
    monkeypatch.setattr(
        api.kg,
        "_code_traceability_kg_read_access",
        AsyncMock(return_value=SimpleNamespace(allowed=True)),
    )
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[api.kg.require_kg_board_actor] = lambda: object()
    app.dependency_overrides[api.kg.require_kg_board_writer_actor] = lambda: object()
    app.dependency_overrides[api.kg.get_unit_of_work] = lambda: object()
    with TestClient(app) as http:
        yield http, registry, provider


def test_real_native_text_and_hybrid_from_http(client, request):
    http, registry, _ = client
    registry.ranked_graph_search = request.getfixturevalue("ranked")[0]
    # Index preparation is fixture setup, never a public HTTP operation.
    registry.ranked_graph_search.prepare(BOARD_ID, "Decision", reason="fixture setup")
    for mode in ("text", "hybrid"):
        result = http.post(
            ROOT + "/search",
            json={
                "node_type": "Decision",
                "query": "durable graph",
                "mode": mode,
                "limit": 1,
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["hits"][0]["node_id"] == "ranked:visible"
        assert result.json()["complete"] is True


@pytest.mark.parametrize(
    "name,path,body",
    [
        ("ranked_graph_search", "/search", {"node_type": "Decision", "query": "test"}),
        (
            "graph_history",
            "/history/as-of",
            {"node_types": ["Decision"], "at": "a" * 32 + ":" + "0" * 15 + "1"},
        ),
        (
            "graph_analytics",
            "/analytics",
            {"node_types": ["Decision"], "algorithm": "cycles"},
        ),
    ],
)
def test_other_adapters_can_explicitly_decline(client, name, path, body):
    http, registry, _ = client
    setattr(registry, name, None)
    result = http.post(ROOT + path, json=body)
    assert result.status_code == 503 and "capability" in result.text.lower()


@pytest.mark.parametrize(
    "path,body",
    [
        ("/search", {"node_type": "Decision", "query": "x", "limit": True}),
        ("/search", {"node_type": "Decision", "query": "x", "vector": [1]}),
        (
            "/analytics",
            {"node_types": ["Decision"], "algorithm": "cycles", "max_edges": 0},
        ),
        (
            "/analytics",
            {"node_types": ["Decision"], "algorithm": "cycles", "unknown": 1},
        ),
    ],
)
def test_invalid_and_unacknowledged_operations_do_not_call_storage(client, path, body):
    http, _, provider = client
    assert http.post(ROOT + path, json=body).status_code == 422
    assert not provider.mock_calls


def test_denial_precedes_every_provider_and_native_failure_is_not_empty(
    client, monkeypatch
):
    http, _, provider = client
    monkeypatch.setattr(
        api.kg, "_require_kg_operation", AsyncMock(side_effect=HTTPException(403))
    )
    assert http.get(ROOT + "/history/commits").status_code == 403
    assert not provider.mock_calls
    monkeypatch.setattr(api.kg, "_require_kg_operation", AsyncMock())
    provider.search.side_effect = GraphUnavailable("native read refused")
    response = http.post(ROOT + "/search", json={"node_type": "Decision", "query": "x"})
    assert response.status_code == 503
    assert "hits" not in response.json()


def test_analytics_filters_server_ct_authority_and_history_denies_unfiltered_access(
    client, monkeypatch
):
    http, _, provider = client
    access = SimpleNamespace(
        allowed=False, authority_resolved=True, missing_permissions=("code.read",)
    )
    monkeypatch.setattr(
        api.kg, "_code_traceability_kg_read_access", AsyncMock(return_value=access)
    )
    provider.analyze.return_value = {"complete": True, "components": []}
    response = http.post(
        ROOT + "/analytics",
        json={
            "node_types": ["Decision"],
            "algorithm": "components",
            "include_code_traceability": True,
        },
    )
    assert response.status_code == 200
    assert provider.analyze.call_args.kwargs["include_code_traceability"] is False
    assert http.get(ROOT + "/history/commits").status_code == 403
    assert (
        http.post(
            ROOT + "/search", json={"node_type": "Decision", "query": "x"}
        ).status_code
        == 403
    )
    provider.commits.assert_not_called()
    provider.search.assert_not_called()


@pytest.mark.parametrize(
    "path,method,body",
    [
        (
            "/history/as-of",
            "as_of",
            {"node_types": ["Decision"], "at": "a" * 32 + ":" + "0" * 15 + "1"},
        ),
        (
            "/history/diff",
            "diff",
            {
                "node_types": ["Decision"],
                "at": "a" * 32 + ":" + "0" * 15 + "2",
                "before": "a" * 32 + ":" + "0" * 15 + "1",
            },
        ),
    ],
)
def test_history_dispatches_bounded_explicit_contract(client, path, method, body):
    http, _, provider = client
    getattr(provider, method).return_value = {"qualified": method}
    response = http.post(ROOT + path, json=body)
    assert response.status_code == 200, response.text
    assert response.json() == {"qualified": method}
    getattr(provider, method).assert_called_once()
    assert getattr(provider, method).call_args.args[0] == BOARD_ID




def test_history_requires_audit_permission_before_native_access(client, monkeypatch):
    http, _, provider = client

    async def authorize(*args, **kwargs):
        if kwargs["operation"] == "kg.operations.audit.read":
            raise HTTPException(403, detail="audit permission required")

    monkeypatch.setattr(api.kg, "_require_kg_operation", authorize)
    assert http.get(ROOT + "/history/commits").status_code == 403
    assert not provider.mock_calls


def test_real_routed_reads_preserve_preexisting_history_and_index(
    client, tmp_path, monkeypatch
):
    from okto_pulse.community.adapters.routed_graph_composition import (
        build_community_routed_graph_composition,
    )
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.kg.guarded_write import guarded_board_write
    from okto_pulse.core.kg.safe_write_lifecycle import (
        KGSafeWriteLifecycle,
        LockOwnerProbe,
    )
    from test_grafx_graph_store import _attrs

    http, registry, _ = client

    def expose_native_failure(error):
        raise error

    monkeypatch.setattr(api.kg, "_graph_problem", expose_native_failure)
    bundle = build_community_routed_graph_composition(
        settings=CommunitySettings(
            _env_file=None,
            data_dir=tmp_path,
            kg_base_dir=str(tmp_path / "kg"),
            kg_grafx_page_size=4096,
        )
    )
    bundle.initialize_board_route(BOARD_ID)
    owner = {"held": False}

    def acquire(**kwargs):
        assert not owner["held"]
        owner["held"] = True
        return SimpleNamespace(acquired=True, owner_token="qualified-writer")

    def release(**kwargs):
        owner["held"] = False
        return True

    lock = SimpleNamespace(
        acquire=acquire,
        release=release,
        renew=lambda **_: True,
        is_owner=lambda *args, **kwargs: owner["held"],
    )
    lifecycle = KGSafeWriteLifecycle(
        step_adapter=bundle.board.graph_lifecycle.apply_step,
        owner_probe=LockOwnerProbe(is_active_owner=lock.is_owner),
    )

    def guard(board, **kwargs):
        return guarded_board_write(
            board, **kwargs, writer_lock=lock, lifecycle=lifecycle
        )

    registry.graph_history = bundle.board.graph_history
    registry.ranked_graph_search = bundle.board.ranked_graph_search
    try:
        with guard(
            BOARD_ID, operation="schema_bootstrap", owner_id="test", mutation_ref="test"
        ) as lease:
            bundle.board.graph_store.bootstrap(BOARD_ID)
            lease.ensure_durable()
        # Preexisting native history is a fixture prerequisite, not HTTP setup.
        with guard(BOARD_ID, operation="fixture_history", owner_id="test", mutation_ref="test") as lease:
            registry.graph_history.activate(BOARD_ID, ("Decision",), (), reason="fixture history")
            lease.ensure_durable()
        assert not owner["held"]
        with guard(
            BOARD_ID, operation="qualification", owner_id="test", mutation_ref="test"
        ) as lease:
            bundle.board.graph_store.create_node(
                BOARD_ID, "Decision", "routed", _attrs("Durable", "spec:routed", "test")
            )
            lease.ensure_durable()
        commits = http.get(ROOT + "/history/commits")
        assert commits.status_code == 200, commits.text
        token = commits.json()["entries"][-1]["commit"]
        response = http.post(
            ROOT + "/history/as-of", json={"node_types": ["Decision"], "at": token}
        )
        assert response.status_code == 200, response.text
        assert response.json()["nodes"][0]["id"] == "routed"
        with guard(BOARD_ID, operation="fixture_index", owner_id="test", mutation_ref="test") as lease:
            registry.ranked_graph_search.prepare(BOARD_ID, "Decision", reason="fixture index")
            lease.ensure_durable()
        response = http.post(
            ROOT + "/search", json={"node_type": "Decision", "query": "durable"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["hits"][0]["node_id"] == "routed"
    finally:
        for pool in bundle.board.grafx_read_pools:
            pool.close_all()
        bundle.grafx_pool.close_all()
