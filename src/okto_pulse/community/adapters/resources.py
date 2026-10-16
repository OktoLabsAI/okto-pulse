"""Community MCP resource catalog (spec R11-A IMP3 + R11-B content split).

The Community edition registers its OWN resource catalog using the CORE contracts
(``okto_pulse.core.ports.mcp_resources``) — core never imports community; the
composition root injects the catalog into the effective catalog and freezes it
after all providers are wired.

Common contracts remain owned by Core. The KG workflow adds Community runtime
notes to the injected common body through the existing explicit replacement
contract. Errors and tool documentation are served directly by Core, avoiding
hand-maintained copies of domain rules in the edition.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from okto_pulse.core.ports.mcp_resources import (
    RESOURCE_KIND_OPERATIONAL,
    FrozenMcpResourceCatalog,
    McpResourceCatalogError,
    McpResourceCatalog,
    McpResourceReplacementTarget,
    McpResourceSpec,
    StaticMcpResourceCatalog,
)
from okto_pulse.core.composition import RuntimeValueRegistry

COMMUNITY_RESOURCE_EDITION = "community"
COMMUNITY_RESOURCE_PRECEDENCE = 10


@dataclass(frozen=True)
class CommunityFileMcpInstructionProvider:
    """Community-owned adapter for deployment-configured prompt files."""

    provider_id: str
    path: Path

    def load_instructions(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.is_file() else ""


#: Edition-owned supplements only; common contract bodies are injected through
#: the public catalog port and frozen together with these operational notes.
_OPERATIONAL_DIR = Path(__file__).resolve().parent.parent / "resources" / "operational"
_LEGACY_AGENT_PROMPT_PATH = Path("/app/prompts/agent_system_prompt.md")

#: R11-B explicit replacement table: (uri, operational path, capability).
_COMMUNITY_REPLACEMENT_TABLE: tuple[tuple[str, str, str], ...] = (
    ("okto-pulse://workflows/kg", "workflows/kg.md", "kg"),
)


def _replacement_category(uri: str) -> str:
    rest = uri[len("okto-pulse://") :]
    parts = rest.split("/")
    if (
        parts[0] == "reference"
        and len(parts) >= 2
        and parts[1] in ("tool-docs", "tool-families")
    ):
        return f"reference/{parts[1]}"
    return parts[0] if parts and parts[0] else "misc"


def _build_replacement_specs(common_catalog: McpResourceCatalog) -> tuple[McpResourceSpec, ...]:
    common = {spec.uri: spec for spec in common_catalog.specs()}
    for uri, path, _capability in _COMMUNITY_REPLACEMENT_TABLE:
        source = common.get(uri)
        if source is None or source.edition != "core" or not source.is_common:
            raise McpResourceCatalogError(
                "common_resource_required",
                "Community notes require the original Core common resource",
                uri=uri,
            )
    return tuple(
        McpResourceSpec(
            uri=uri,
            description="KG query timing, consolidation and closeout; Community Grafx health and runtime notes.",
            category=_replacement_category(uri),
            edition=COMMUNITY_RESOURCE_EDITION,
            kind=RESOURCE_KIND_OPERATIONAL,
            provider="community-embedded-kg",
            capability=capability,
            mime_type="text/markdown",
            source_identity=f"community:{path}",
            replace_target=McpResourceReplacementTarget(
                edition="core",
                source_identity=f"core:{path}",
            ),
            path=path,
            content=common[uri].read().rstrip() + "\n\n" + (
                _OPERATIONAL_DIR / path
            ).read_text(encoding="utf-8"),
        )
        for uri, path, capability in _COMMUNITY_REPLACEMENT_TABLE
    )


def build_community_resource_catalog(common_catalog: McpResourceCatalog) -> StaticMcpResourceCatalog:
    """Compose the injected common workflow and edition notes once at startup.

    Only the public catalog port is consumed; no private Core modules or source
    paths are read. The existing freeze hashes the complete composed body.
    """
    return StaticMcpResourceCatalog(
        COMMUNITY_RESOURCE_EDITION,
        _build_replacement_specs(common_catalog),
        precedence=COMMUNITY_RESOURCE_PRECEDENCE,
    )


@dataclass
class CommunityMcpColdStartTransaction:
    """Rollback boundary for Community instruction/resource publication.

    Core's runtime registry copy clones the command catalog, including its
    instruction text and compatibility resource handlers.  Holding that copy
    until both Community listeners are ready lets one rollback restore the
    complete pre-start projection without reaching into either Core or FastMCP
    internals.
    """

    _registry: RuntimeValueRegistry
    _baseline: RuntimeValueRegistry | None
    frozen_catalog: FrozenMcpResourceCatalog | None = None
    projection_identity: str | None = None
    _committed: bool = False
    _rolled_back: bool = False

    @classmethod
    def begin(
        cls,
        runtime_values: RuntimeValueRegistry,
    ) -> "CommunityMcpColdStartTransaction":
        # The already-composed runtime values are the public composition
        # contract (``RuntimeComposition.runtime_values``); every caller injects
        # the active composition registry explicitly.  No ambient fallback and
        # no lazy ``create=True`` are permitted: rollback must restore the exact
        # registry object that composition mutated.
        if runtime_values is None:
            raise TypeError(
                "CommunityMcpColdStartTransaction.begin requires an explicit "
                "runtime-value registry"
            )
        return cls(_registry=runtime_values, _baseline=runtime_values.copy())

    def stage(self, catalog: FrozenMcpResourceCatalog) -> None:
        if self._committed or self._rolled_back:
            raise RuntimeError("community_mcp_cold_start_transaction_closed")
        if not isinstance(catalog, FrozenMcpResourceCatalog) or not catalog.is_frozen:
            raise McpResourceCatalogError(
                "registry_unfrozen",
                "Community cold start requires a frozen resource projection",
            )
        if catalog.identity != catalog.manifest.projection_identity:
            raise McpResourceCatalogError(
                "projection_identity_mismatch",
                "frozen catalog and manifest projection identities differ",
            )
        self.frozen_catalog = catalog
        self.projection_identity = catalog.identity

    def require_frozen_projection(
        self,
    ) -> tuple[FrozenMcpResourceCatalog, str]:
        if self._rolled_back:
            raise RuntimeError("community_mcp_cold_start_transaction_rolled_back")
        catalog = self.frozen_catalog
        identity = self.projection_identity
        if catalog is None or identity is None:
            raise McpResourceCatalogError(
                "registry_unfrozen",
                "Community resource projection was not frozen before host startup",
            )
        if not catalog.is_frozen or catalog.identity != identity:
            raise McpResourceCatalogError(
                "projection_identity_mismatch",
                "Community frozen projection identity changed before host startup",
            )
        return catalog, identity

    def rollback(self) -> None:
        """Restore the complete pre-compose runtime fingerprint once."""

        if self._committed or self._rolled_back:
            return
        baseline = self._baseline
        if baseline is None:
            raise RuntimeError("community_mcp_cold_start_baseline_missing")
        replacement = baseline.copy()
        current_keys = tuple(self._registry.snapshot())
        if current_keys:
            self._registry.discard(*current_keys)
        for key, value in replacement.snapshot().items():
            self._registry.register(key, value)
        self._baseline = None
        self.frozen_catalog = None
        self.projection_identity = None
        self._rolled_back = True

    def commit(self) -> None:
        """Discard rollback state after listener readiness is published."""

        if self._rolled_back:
            raise RuntimeError("community_mcp_cold_start_transaction_rolled_back")
        self._baseline = None
        self._committed = True


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


def register_and_freeze_community_resource_catalog(
    runtime_values: RuntimeValueRegistry,
) -> CommunityMcpColdStartTransaction:
    """Composition-root hook (R11-A IMP4): inject the Community operational
    catalog into the core effective catalog, then FREEZE it (after all providers
    are registered). Idempotent-safe: a second freeze is a no-op, but a late
    register AFTER the freeze raises (fail-closed).

    ``runtime_values`` is the already-composed public composition contract
    (``RuntimeComposition.runtime_values``) and is required; every caller
    injects the active composition registry explicitly."""
    from okto_pulse.core.mcp import (
        effective_resource_catalog,
        freeze_instruction_providers,
        freeze_resource_catalog,
        register_resource_catalog,
    )

    transaction = CommunityMcpColdStartTransaction.begin(runtime_values)
    try:
        register_community_instruction_provider(freeze=False)

        effective = effective_resource_catalog()
        composed_editions = {
            item.edition for item in getattr(effective, "catalogs", ())
        }
        if COMMUNITY_RESOURCE_EDITION not in composed_editions:
            register_resource_catalog(build_community_resource_catalog(effective))
        freeze_instruction_providers()
        freeze_resource_catalog()

        frozen = effective_resource_catalog()
        if not isinstance(frozen, FrozenMcpResourceCatalog):
            raise McpResourceCatalogError(
                "registry_unfrozen",
                "Community resource projection did not freeze",
            )
        transaction.stage(frozen)
    except BaseException:
        transaction.rollback()
        raise
    return transaction


__all__ = [
    "COMMUNITY_RESOURCE_EDITION",
    "COMMUNITY_RESOURCE_PRECEDENCE",
    "CommunityMcpColdStartTransaction",
    "build_community_instruction_provider",
    "build_community_resource_catalog",
    "register_and_freeze_community_resource_catalog",
    "register_community_instruction_provider",
]
