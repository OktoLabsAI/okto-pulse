"""Community MCP resource catalog (spec R11-A IMP3 + R11-B content split).

The Community edition registers its OWN resource catalog using the CORE contracts
(``okto_pulse.core.ports.mcp_resources``) — core never imports community; the
composition root injects the catalog into the effective catalog and freezes it
after all providers are wired.

R11-B content split: the four core common docs that leaked backend details
(``workflows/kg``, ``reference/errors``, ``reference/tool-docs/kg``,
``reference/tool-docs/decision``) were scrubbed backend-free in core. Their FULL
backend-aware content is migrated here as same-URI OVERLAY specs
(``kind=operational``, ``same_uri_overlay=True``) whose bodies are byte-exact
captures of the pre-split originals. In the Community runtime the effective
catalog MERGES the overlay over the common base for the same URI, so the agent
reads the original (byte-equivalent) content; in core-only the scrubbed common is
served and passes the closed forbidden-term scan.
"""

from __future__ import annotations

from pathlib import Path

from okto_pulse.core.ports.mcp_resources import (
    RESOURCE_KIND_OPERATIONAL,
    McpResourceSpec,
    StaticMcpResourceCatalog,
)

COMMUNITY_RESOURCE_EDITION = "community"

#: Where the Community operational resource bodies live (byte-exact captures of
#: the pre-split originals — they MAY name the concrete backend; they are
#: ``kind=operational`` and exempt from the common forbidden-term scan).
_OPERATIONAL_DIR = Path(__file__).resolve().parent.parent / "resources" / "operational"

#: R11-B same-URI OVERLAY table: (uri, operational-relative-path, capability).
_COMMUNITY_OVERLAY_TABLE: tuple[tuple[str, str, str], ...] = (
    ("okto-pulse://workflows/kg", "workflows/kg.md", "kg"),
    ("okto-pulse://reference/errors", "reference/errors.md", "errors"),
    ("okto-pulse://reference/tool-docs/kg", "reference/tool-docs/kg.md", "kg"),
    ("okto-pulse://reference/tool-docs/decision", "reference/tool-docs/decision.md", "decision"),
)


def _overlay_category(uri: str) -> str:
    rest = uri[len("okto-pulse://"):]
    parts = rest.split("/")
    if parts[0] == "reference" and len(parts) >= 2 and parts[1] in ("tool-docs", "tool-families"):
        return f"reference/{parts[1]}"
    return parts[0] if parts and parts[0] else "misc"


def _build_overlay_specs() -> tuple[McpResourceSpec, ...]:
    return tuple(
        McpResourceSpec(
            uri=uri,
            description="(operational overlay — backend-specific content)",
            category=_overlay_category(uri),
            edition=COMMUNITY_RESOURCE_EDITION,
            kind=RESOURCE_KIND_OPERATIONAL,
            provider="community-embedded-kg",
            capability=capability,
            same_uri_overlay=True,
            path=path,
            base_dir=_OPERATIONAL_DIR,
        )
        for uri, path, capability in _COMMUNITY_OVERLAY_TABLE
    )


def build_community_resource_catalog() -> StaticMcpResourceCatalog:
    """Build the Community edition's operational resource catalog via the core
    contracts (R11-B): the four same-URI OVERLAY specs that restore the full
    backend-aware content scrubbed from the core common docs. The composition root
    injects this catalog and the effective catalog MERGES the overlay over the
    common base for the same URI."""
    return StaticMcpResourceCatalog(
        COMMUNITY_RESOURCE_EDITION, _build_overlay_specs()
    )


def register_and_freeze_community_resource_catalog() -> None:
    """Composition-root hook (R11-A IMP4): inject the Community operational
    catalog into the core effective catalog, then FREEZE it (after all providers
    are registered). Idempotent-safe: a second freeze is a no-op, but a late
    register AFTER the freeze raises (fail-closed)."""
    from okto_pulse.core.mcp import server as core_mcp_server

    catalog = build_community_resource_catalog()
    if catalog.specs():
        core_mcp_server.register_resource_catalog(catalog)
    core_mcp_server.freeze_resource_catalog()


__all__ = [
    "COMMUNITY_RESOURCE_EDITION",
    "build_community_resource_catalog",
    "register_and_freeze_community_resource_catalog",
]
