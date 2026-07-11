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

import os
from dataclasses import dataclass
from pathlib import Path

from okto_pulse.core.ports.mcp_resources import (
    RESOURCE_KIND_OPERATIONAL,
    McpResourceSpec,
    StaticMcpResourceCatalog,
)

COMMUNITY_RESOURCE_EDITION = "community"


@dataclass(frozen=True)
class CommunityFileMcpInstructionProvider:
    """Community-owned adapter for deployment-configured prompt files."""

    provider_id: str
    path: Path

    def load_instructions(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.is_file() else ""


#: Where the Community operational resource bodies live (byte-exact captures of
#: the pre-split originals — they MAY name the concrete backend; they are
#: ``kind=operational`` and exempt from the common forbidden-term scan).
_OPERATIONAL_DIR = Path(__file__).resolve().parent.parent / "resources" / "operational"
_LEGACY_AGENT_PROMPT_PATH = Path("/app/prompts/agent_system_prompt.md")

#: R11-B same-URI OVERLAY table: (uri, operational-relative-path, capability).
_COMMUNITY_OVERLAY_TABLE: tuple[tuple[str, str, str], ...] = (
    ("okto-pulse://workflows/kg", "workflows/kg.md", "kg"),
    ("okto-pulse://reference/errors", "reference/errors.md", "errors"),
    ("okto-pulse://reference/tool-docs/kg", "reference/tool-docs/kg.md", "kg"),
    (
        "okto-pulse://reference/tool-docs/decision",
        "reference/tool-docs/decision.md",
        "decision",
    ),
)


def _overlay_category(uri: str) -> str:
    rest = uri[len("okto-pulse://") :]
    parts = rest.split("/")
    if (
        parts[0] == "reference"
        and len(parts) >= 2
        and parts[1] in ("tool-docs", "tool-families")
    ):
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
            loader=lambda path=path: (_OPERATIONAL_DIR / path).read_text(
                encoding="utf-8"
            ),
        )
        for uri, path, capability in _COMMUNITY_OVERLAY_TABLE
    )


def build_community_resource_catalog() -> StaticMcpResourceCatalog:
    """Build the Community edition's operational resource catalog via the core
    contracts (R11-B): the four same-URI OVERLAY specs that restore the full
    backend-aware content scrubbed from the core common docs. The composition root
    injects this catalog and the effective catalog MERGES the overlay over the
    common base for the same URI."""
    return StaticMcpResourceCatalog(COMMUNITY_RESOURCE_EDITION, _build_overlay_specs())


def _community_agent_prompt_path(prompt_path: str | Path | None = None) -> Path:
    if prompt_path is not None:
        return Path(prompt_path)
    configured = os.environ.get("OKTO_PULSE_AGENT_INSTRUCTIONS_PATH")
    return Path(configured) if configured else _LEGACY_AGENT_PROMPT_PATH


def build_community_instruction_provider(
    prompt_path: str | Path | None = None,
) -> CommunityFileMcpInstructionProvider:
    """Build the Community-owned MCP instruction provider.

    The legacy container prompt path is preserved here, in the edition adapter,
    so the core server no longer knows deployment-owned filesystem paths.
    """
    path = _community_agent_prompt_path(prompt_path)
    return CommunityFileMcpInstructionProvider(
        provider_id=COMMUNITY_RESOURCE_EDITION,
        path=path,
    )


def register_community_instruction_provider(
    prompt_path: str | Path | None = None,
    *,
    freeze: bool = True,
) -> None:
    """Register the Community MCP instruction provider via the core contract."""
    from okto_pulse.core.mcp import (
        freeze_instruction_providers,
        has_instruction_provider,
        register_instruction_provider,
    )

    if has_instruction_provider(COMMUNITY_RESOURCE_EDITION):
        if freeze:
            freeze_instruction_providers()
        return

    register_instruction_provider(build_community_instruction_provider(prompt_path))
    if freeze:
        freeze_instruction_providers()


def register_and_freeze_community_resource_catalog() -> None:
    """Composition-root hook (R11-A IMP4): inject the Community operational
    catalog into the core effective catalog, then FREEZE it (after all providers
    are registered). Idempotent-safe: a second freeze is a no-op, but a late
    register AFTER the freeze raises (fail-closed)."""
    from okto_pulse.core.mcp import (
        effective_resource_catalog,
        freeze_instruction_providers,
        freeze_resource_catalog,
        register_resource_catalog,
    )

    register_community_instruction_provider(freeze=False)

    catalog = build_community_resource_catalog()
    effective = effective_resource_catalog()
    composed_editions = {item.edition for item in getattr(effective, "catalogs", ())}
    if catalog.specs() and COMMUNITY_RESOURCE_EDITION not in composed_editions:
        register_resource_catalog(catalog)
    freeze_instruction_providers()
    freeze_resource_catalog()


__all__ = [
    "COMMUNITY_RESOURCE_EDITION",
    "build_community_instruction_provider",
    "build_community_resource_catalog",
    "register_and_freeze_community_resource_catalog",
    "register_community_instruction_provider",
]
