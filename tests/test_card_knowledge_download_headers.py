from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import okto_pulse.community.api.cards as cards_api


def test_knowledge_download_uses_ascii_fallback_and_rfc5987_filename(
    monkeypatch,
):
    title = "设计 / alpha?beta"
    description = "描述 & details"
    content = "内容 <ok>"

    async def execute(_self, _command, *, actor, uow):
        return SimpleNamespace(
            knowledge={
                "id": "knowledge-id",
                "title": title,
                "description": description,
                "content": content,
            }
        )

    monkeypatch.setattr(cards_api.GetCardKnowledgeUseCase, "execute", execute)

    app = FastAPI()
    app.include_router(cards_api.router, prefix="/cards")
    app.dependency_overrides[cards_api.require_user] = lambda: "download-user"
    app.dependency_overrides[cards_api.get_unit_of_work] = lambda: object()

    response = TestClient(app).get("/cards/card-id/knowledge/knowledge-id/download")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        'attachment; filename="knowledge.md"; '
        "filename*=utf-8''%E8%AE%BE%E8%AE%A1___alpha_beta.md"
    )
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.content == (f"# {title}\n\n> {description}\n\n{content}\n".encode())
