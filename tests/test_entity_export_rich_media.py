"""Security and completeness contracts for export rich media."""

from __future__ import annotations

import base64
from html import unescape
from html.parser import HTMLParser
import re

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_entity_export import (
    CommunitySqlAlchemyEntityExportReader,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureDesign,
    ArchitectureDiagramPayload,
    Base,
    Board,
    Spec,
)
from okto_pulse.community.services.entity_export_rich_media import (
    mermaid_to_static_svg,
    render_rich_media_html,
    render_rich_media_markdown,
    sanitize_mermaid_source,
    seal_screen_mockups,
)
from okto_pulse.community.services.entity_export_renderer import (
    render_entity_export_html,
)
from okto_pulse.core.domain.entity_export import (
    EntityExportDisclosure,
    EntityExportHistoryScope,
    EntityExportRequest,
    EntityExportType,
)
from okto_pulse.core.domain.realm import RealmScope


class _PassiveStandaloneHTMLAudit(HTMLParser):
    """Python equivalent of the frontend's fail-closed download audit."""

    _FORBIDDEN = {
        "applet",
        "base",
        "embed",
        "frame",
        "frameset",
        "iframe",
        "object",
        "script",
    }
    _SVG_FORBIDDEN = {"a", "animate", "foreignobject", "image", "set", "use"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.violations: list[str] = []
        self._svg_depth = 0
        self._style_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if normalized in self._FORBIDDEN:
            self.violations.append(f"forbidden_tag:{normalized}")
        values = {name.casefold(): value or "" for name, value in attrs}
        if (
            normalized == "meta"
            and values.get("http-equiv", "").casefold() == "refresh"
        ):
            self.violations.append("meta_refresh")
        if normalized == "link" and (values.get("href") or values.get("rel")):
            self.violations.append("external_link_resource")
        if self._svg_depth and normalized in self._SVG_FORBIDDEN:
            self.violations.append(f"active_svg_tag:{normalized}")
        if normalized == "svg":
            self._svg_depth += 1
        if normalized == "style":
            self._style_depth += 1
        for name, value in values.items():
            folded = re.sub(r"[\x00-\x20]+", "", value).casefold()
            if re.fullmatch(r"on[a-z0-9_-]+", name):
                self.violations.append(f"event_attribute:{name}")
            if name in {"src", "poster"} and re.match(r"^(?:https?:|//)", folded):
                self.violations.append(f"external_asset:{name}")
            if name in {"href", "src", "action", "formaction"} and re.match(
                r"^(?:javascript|vbscript):", folded
            ):
                self.violations.append(f"executable_url:{name}")
            if name in {"href", "src"} and re.match(r"^data:text/html", folded):
                self.violations.append(f"active_data_url:{name}")
            if name == "style" and re.search(
                r"@import\s|url\(\s*[\"']?\s*(?:https?:|//)", value, re.IGNORECASE
            ):
                self.violations.append("external_css")
            if self._svg_depth and (name in {"href", "xlink:href", "srcdoc"}):
                self.violations.append(f"active_svg_attribute:{name}")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "svg" and self._svg_depth:
            self._svg_depth -= 1
        if tag.casefold() == "style" and self._style_depth:
            self._style_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._style_depth and re.search(
            r"@import\s|url\(\s*[\"']?\s*(?:https?:|//)", data, re.IGNORECASE
        ):
            self.violations.append("external_css")


def _assert_passive_standalone_html(html: str) -> None:
    audit = _PassiveStandaloneHTMLAudit()
    audit.feed(html)
    audit.close()
    assert audit.violations == []
    decoded = unescape(html)
    assert "frame-src 'none'" in decoded
    assert "frame-src data:" not in decoded


def test_mockup_preview_is_a_strictly_passive_inline_projection() -> None:
    hostile = (
        '<meta http-equiv="refresh" content="0;url=https://attacker.invalid">'
        "<style>.card{background:url(https://attacker.invalid)}</style>"
        '<div class="chapter" style="display:grid;position:fixed;color:#7c3aed;'
        'background-image:url(https://attacker.invalid/x)">Checkout</div>'
        '<script>top.location="https://attacker.invalid"</script>'
        '<img src="https://attacker.invalid/pixel" onerror="alert(1)">'
        '<iframe srcdoc="<script>alert(1)</script>">hidden</iframe>'
        '<object data="https://attacker.invalid">hidden</object>'
        '<form action="https://attacker.invalid"><a href="javascript:alert(1)">'
        '<button onclick="alert(1)">Send</button></a>'
        '<input placeholder="Account name" value="Ada Lovelace" autofocus></form>'
    )
    sealed = seal_screen_mockups(
        [
            {
                "id": "mockup-1",
                "title": "Checkout",
                "description": "Successful payment state",
                "screen_type": "page",
                "html_content": hostile,
            }
        ]
    )

    envelope = sealed[0]["html_content"]
    assert envelope["state"] == "passive_inline_ready"
    assert envelope["execution_policy"] == "passive_inline_allowlist"
    assert "<script>" not in envelope["payload"]
    sealed_source = base64.b64decode(envelope["payload"]).decode("utf-8")
    assert "Checkout" in sealed_source
    assert "<script" not in sealed_source
    assert "attacker.invalid" not in sealed_source
    assert "onerror=" not in sealed_source
    assert "position:fixed" not in sealed_source
    assert "color:#7c3aed" in sealed_source
    assert "Ada Lovelace" in sealed_source

    html = render_rich_media_html(
        {"embedded": {"screen_mockups": sealed}}, field_key="mockups"
    )
    assert html is not None
    assert '<div class="mockup-preview" role="group"' in html
    assert '<div class="mockup-surface">' in html
    assert '<span class="mockup-control mockup-button">Send</span>' in html
    assert '<span class="mockup-control mockup-input">Ada Lovelace</span>' in html
    assert "Checkout" in html
    assert "attacker.invalid" not in html
    assert "onerror=" not in html
    for forbidden in (
        "<script",
        "<iframe",
        "<frame",
        "<frameset",
        "<object",
        "<embed",
        "<applet",
        "<base",
        "<form",
        "<button",
        "<a ",
        "srcdoc",
        "data:text/html",
        "javascript:",
        "http://",
        "https://",
    ):
        assert forbidden not in html.casefold()
    assert not re.search(r"\son[a-z0-9_-]+\s*=", html, re.IGNORECASE)


def test_mockup_invalid_seal_falls_back_without_inlining_markup() -> None:
    html = render_rich_media_html(
        [
            {
                "title": "Unavailable preview",
                "html_content": {
                    "state": "passive_inline_ready",
                    "encoding": "base64",
                    "payload": "not base64!",
                },
            }
        ],
        field_key="screen_mockups",
    )
    assert html is not None
    assert "Visual preview is not available" in html
    assert "mockup-preview" not in html


@pytest.mark.parametrize(
    ("source", "visible"),
    [
        ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB", True),
        (
            "data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+PC9zdmc+",
            False,
        ),
        (
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB#suffix",
            False,
        ),
        ("https://example.invalid/mockup.png", False),
    ],
)
def test_mockup_allows_only_signature_checked_raster_data(
    source: str, visible: bool
) -> None:
    sealed = seal_screen_mockups(
        [{"title": "Image", "html_content": f'<img src="{source}" alt="Preview">'}]
    )
    html = render_rich_media_html(sealed, field_key="mockups")
    assert html is not None
    if visible:
        assert f'src="{source}"' in html
    else:
        assert source not in html
        assert "<svg" not in html


def test_server_renderer_emits_passive_inline_mockup_and_static_architecture() -> None:
    sealed = seal_screen_mockups(
        [
            {
                "title": "Checkout screen",
                "screen_type": "page",
                "html_content": (
                    '<main><h1>Checkout</h1><button onclick="alert(1)">Pay now</button>'
                    '<iframe src="https://example.invalid"></iframe></main>'
                ),
            }
        ]
    )
    raw = {
        "subject": {
            "board_id": "board-1",
            "entity_type": "spec",
            "entity_id": "spec-1",
            "title": "Rich export",
            "status": "validation",
            "edition": 2,
            "version": 7,
        },
        "history_scope": "complete",
        "complete_for_actor": True,
        "source_complete": True,
        "overall_state": "complete",
        "manifest": {
            "entries": [
                {"section_key": "mockups", "status": "included", "total_count": 1},
                {"section_key": "architecture", "status": "included", "total_count": 1},
            ]
        },
        "sections": [
            {
                "section_key": "mockups",
                "payload": {"embedded": {"screen_mockups": sealed}},
            },
            {
                "section_key": "architecture",
                "payload": {
                    "records": {
                        "architecture_designs": [
                            {
                                "title": "Runtime flow",
                                "global_description": "Browser to API.",
                                "entities": [
                                    {"id": "web", "name": "Web UI"},
                                    {"id": "api", "name": "Application API"},
                                ],
                                "interfaces": [
                                    {
                                        "name": "Submit order",
                                        "participants": ["web", "api"],
                                    }
                                ],
                                "diagrams": [],
                            }
                        ]
                    }
                },
            },
        ],
    }

    html = render_entity_export_html(raw)

    _assert_passive_standalone_html(html)
    assert '<div class="mockup-preview" role="group"' in html
    assert '<span class="mockup-control mockup-button">Pay now</span>' in html
    assert '<svg class="diagram-canvas"' in html
    assert "Web UI" in html
    assert "Application API" in html
    assert "Submit order" in html
    assert "example.invalid" not in html


def test_architecture_mermaid_is_static_and_unsafe_directives_are_removed() -> None:
    source = """flowchart LR
api["API <script>alert(1)</script>"]
db["Database"]
click api "javascript:alert(1)"
style api fill:url(https://attacker.invalid/x)
remote@{ img: "https://attacker.invalid/diagram.svg" }
```
api -- "stores" --> db
"""
    safe = sanitize_mermaid_source(source)
    assert safe is not None
    assert "click" not in safe
    assert "javascript:" not in safe
    assert "style api" not in safe
    assert "remote@{" not in safe
    assert "```" not in safe
    assert "<script" not in safe

    payload = {
        "records": {
            "architecture_designs": [
                {
                    "id": "design-1",
                    "title": "Checkout architecture",
                    "global_description": "Browser to API and storage flow.",
                    "entities": [],
                    "interfaces": [],
                    "diagrams": [
                        {
                            "id": "diagram-1",
                            "format": "mermaid",
                            "adapter_payload_ref": "payload-1",
                        }
                    ],
                }
            ],
            "architecture_diagram_payloads": [
                {
                    "id": "payload-1",
                    "design_id": "design-1",
                    "diagram_id": "diagram-1",
                    "format": "mermaid",
                    "payload_text": source,
                }
            ],
        }
    }
    html = render_rich_media_html(payload, field_key="architecture")
    markdown = render_rich_media_markdown(payload, field_key="architecture")
    assert html is not None
    assert markdown is not None
    assert '<svg class="diagram-canvas"' in html
    assert "Checkout architecture" in html
    assert "Browser to API and storage flow." in html
    assert "<script" not in html
    assert "javascript:" not in html
    assert "attacker.invalid" not in html
    for forbidden in (
        "<iframe",
        "<object",
        "<embed",
        "<foreignobject",
        "<image",
        "<use",
        "<animate",
        "<set",
        "<a ",
        " href=",
        "xlink:",
        "srcdoc",
        "data:text/html",
        "http://",
        "https://",
    ):
        assert forbidden not in html.casefold()
    assert not re.search(r"\son[a-z0-9_-]+\s*=", html, re.IGNORECASE)
    joined = "\n".join(markdown)
    assert "```mermaid" in joined
    assert "click api" not in joined
    assert "javascript:" not in joined
    assert joined.startswith("<details>")


def test_semantic_architecture_fallback_renders_svg_and_mermaid() -> None:
    design = {
        "title": "Runtime flow",
        "entities": [
            {"id": "web", "name": "Web UI"},
            {"id": "api", "name": "Application API"},
        ],
        "interfaces": [{"name": "Fetch boards", "participants": ["web", "api"]}],
        "diagrams": [],
    }
    html = render_rich_media_html([design], field_key="architecture_designs")
    markdown = render_rich_media_markdown([design], field_key="architecture_designs")
    assert html is not None
    assert markdown is not None
    assert "Web UI" in html
    assert "Application API" in html
    assert "Fetch boards" in html
    assert '<svg class="diagram-canvas"' in html
    joined = "\n".join(markdown)
    assert 'web["Web UI"]' in joined
    assert 'web -- "Fetch boards" --> api' in joined


def test_static_svg_escapes_labels_and_has_no_active_content() -> None:
    svg = mermaid_to_static_svg(
        'flowchart LR\na["Safe"]\nb["Target"]\na -- "<img onerror=alert(1)>" --> b\n',
        title="Diagram </svg><script>alert(1)</script>",
    )
    assert svg is not None
    assert "<script" not in svg
    assert "<img" not in svg
    assert "onerror=" not in svg
    assert "<foreignobject" not in svg.casefold()
    assert "<image" not in svg.casefold()
    assert "<use" not in svg.casefold()
    assert "<animate" not in svg.casefold()
    assert "<set" not in svg.casefold()
    assert "<a " not in svg.casefold()
    assert " href=" not in svg.casefold()
    assert "xlink:" not in svg.casefold()
    assert "srcdoc" not in svg.casefold()
    assert "data:text/html" not in svg.casefold()
    assert not re.search(r"\son[a-z0-9_-]+\s*=", svg, re.IGNORECASE)
    assert "http://" not in svg.casefold()
    assert "https://" not in svg.casefold()
    assert "&lt;img onerror&#61;alert(1)&gt;" in svg


def test_knowledge_and_mockup_markdown_are_collapsed_under_parent_item() -> None:
    knowledge = render_rich_media_markdown(
        [
            {
                "title": "Availability decisions",
                "description": "Why the SLO was selected.",
                "content": "Three availability zones are required.\n<script>alert(1)</script>",
            }
        ],
        field_key="knowledge_bases",
    )
    mockups = render_rich_media_markdown(
        seal_screen_mockups(
            [
                {
                    "title": "Dashboard",
                    "screen_type": "page",
                    "html_content": "<main>Dashboard</main>",
                }
            ]
        ),
        field_key="mockups",
    )
    assert knowledge is not None and mockups is not None
    assert knowledge[0] == "<details>"
    assert "Availability decisions" in knowledge[1]
    assert "Three availability zones are required." in "\n".join(knowledge)
    assert "<script>" not in "\n".join(knowledge)
    assert "\\<script\\>alert(1)\\</script\\>" in "\n".join(knowledge)
    assert mockups[0] == "<details>"
    assert "passive inline visual preview" in "\n".join(mockups).casefold()


@pytest.mark.asyncio
async def test_reader_includes_mockup_visual_and_architecture_diagram_payload() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_community_session_factory(engine)
    mermaid = 'flowchart LR\nweb["Web"] --> api["API"]\n'
    async with sessions() as session:
        session.add(Board(id="board", name="Board", owner_id="user", realm_id="realm"))
        session.add(
            Spec(
                id="spec",
                board_id="board",
                title="Export rich media",
                created_by="user",
                screen_mockups=[
                    {
                        "id": "mockup-1",
                        "title": "Dashboard",
                        "screen_type": "page",
                        "html_content": "<main>Dashboard</main>",
                    }
                ],
            )
        )
        session.add(
            ArchitectureDesign(
                id="design-1",
                board_id="board",
                parent_type="spec",
                spec_id="spec",
                title="Runtime architecture",
                global_description="Web to API.",
                entities=[],
                interfaces=[],
                diagrams=[
                    {
                        "id": "diagram-1",
                        "title": "Runtime",
                        "format": "mermaid",
                        "adapter_payload_ref": "payload-1",
                        "order_index": 0,
                    }
                ],
                created_by="user",
            )
        )
        session.add(
            ArchitectureDiagramPayload(
                id="payload-1",
                design_id="design-1",
                diagram_id="diagram-1",
                board_id="board",
                storage_backend="database",
                storage_key="architecture/design-1/diagram-1",
                format="mermaid",
                payload_text=mermaid,
                content_hash="a" * 64,
                size_bytes=len(mermaid.encode("utf-8")),
            )
        )
        await session.commit()

        bundle = await CommunitySqlAlchemyEntityExportReader(session).build_bundle(
            request=EntityExportRequest(
                board_id="board",
                entity_type=EntityExportType.SPEC,
                entity_id="spec",
                history_scope=EntityExportHistoryScope.COMPLETE,
                requested_sections=("mockups", "architecture"),
            ),
            disclosure=EntityExportDisclosure(
                frozenset(
                    {
                        "spec.entity.read",
                        "spec.mockups.read",
                        "spec.architecture.read",
                    }
                ),
                ("mockups", "architecture"),
            ),
            actor_id="user",
            realm_scope=RealmScope.tenant("realm"),
        )
        raw = bundle.to_dict()
        mockups = next(
            section
            for section in raw["sections"]
            if section["section_key"] == "mockups"
        )
        architecture = next(
            section
            for section in raw["sections"]
            if section["section_key"] == "architecture"
        )
        sealed = mockups["payload"]["embedded"]["screen_mockups"][0]["html_content"]
        assert sealed["state"] == "passive_inline_ready"
        assert "Dashboard" in base64.b64decode(sealed["payload"]).decode("utf-8")
        payloads = architecture["payload"]["records"]["architecture_diagram_payloads"]
        assert len(payloads) == 1
        assert payloads[0]["payload_text"] == mermaid
        assert "storage_key" not in payloads[0]

    await engine.dispose()
