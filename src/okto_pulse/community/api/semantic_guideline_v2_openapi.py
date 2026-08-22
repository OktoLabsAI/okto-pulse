"""Deterministic OpenAPI artifact for the semantic-assessment v2 route."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute


ARTIFACT_PATH = (
    Path(__file__).parents[1]
    / "resources"
    / "contracts"
    / "semantic-guideline-assessment-v2.openapi.json"
)
ROUTE_PATH = "/boards/{board_id}/semantic-guideline-assessments/v2"


def build_semantic_guideline_v2_openapi() -> dict[str, Any]:
    """Build only the public v2 route and its transitively referenced schemas."""

    from okto_pulse.community.api.policy_governance import router

    matching = [
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path == ROUTE_PATH
        and "POST" in route.methods
    ]
    if len(matching) != 1:
        raise RuntimeError("semantic_guideline_v2_openapi_route_not_unique")
    return get_openapi(
        title="Okto Pulse semantic guideline assessment v2",
        version="2.0.0",
        routes=matching,
    )


def render_semantic_guideline_v2_openapi() -> str:
    return json.dumps(
        build_semantic_guideline_v2_openapi(),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"


def regenerate_file() -> str:
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(
        render_semantic_guideline_v2_openapi(),
        encoding="utf-8",
        newline="\n",
    )
    return str(ARTIFACT_PATH)


if __name__ == "__main__":
    print(regenerate_file())


__all__ = [
    "ARTIFACT_PATH",
    "ROUTE_PATH",
    "build_semantic_guideline_v2_openapi",
    "regenerate_file",
    "render_semantic_guideline_v2_openapi",
]
