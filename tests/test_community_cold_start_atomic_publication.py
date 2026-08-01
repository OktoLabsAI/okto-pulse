"""RED acceptance for atomic Community MCP cold-start publication.

The Community process composes instructions/resources while constructing the
application, materializes the Community FastMCP host, and only then starts its
two listeners.  A failure at any one of those boundaries must leave the
app-owned runtime projection exactly as it was before the attempt, and must not
publish either a listener or the final ready signal.

This is intentionally test-first coverage for Pulse test card 60414707.  The
probe catalog below avoids the four governed production overlay declarations;
the collision case uses a separate deliberate declaration so every injected
failure is independent of that pending migration.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
import sys
from dataclasses import dataclass, fields
from types import ModuleType
from typing import Any, Iterator

import pytest
import uvicorn

import okto_pulse.community as community_package
import okto_pulse.community.app as community_app_module
import okto_pulse.community.adapters.mcp_host as mcp_host_module
import okto_pulse.community.adapters.resources as community_resources
import okto_pulse.community.adapters.runtime_composition as composition_module
import okto_pulse.community.runtime as community_runtime_module
import okto_pulse.core.mcp.server as core_server
from okto_pulse.core.composition import RuntimeComposition, runtime_composition_scope
from okto_pulse.core.ports.mcp_resources import (
    RESOURCE_KIND_OPERATIONAL,
    McpResourceCatalogError,
    McpResourceManifest,
    McpResourceSpec,
    StaticMcpResourceCatalog,
)


_MAIN_MODULE = "okto_pulse.community.main"
_PROBE_URI = "okto-pulse://community/cold-start-atomicity-probe"
_COLLISION_URI = "okto-pulse://workflows/preflight"
_READY_MESSAGE = "Startup complete - The application is ready"


class _InjectedColdStartFailure(RuntimeError):
    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"injected cold-start failure: {stage}")


@dataclass(frozen=True)
class _PublicationFingerprint:
    instruction_provider_ids: tuple[str, ...]
    instructions_frozen: bool
    instructions_sha256: str
    effective_resources: tuple[tuple[str, str, str, str], ...]
    registry_projection: tuple[tuple[str, str, str], ...]
    transport_resources: tuple[tuple[str, str], ...]
    resource_catalog_frozen: bool
    manifest_state: tuple[str, str, int]
    authoritative_matches_transport: bool


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _publication_fingerprint(
    composition: RuntimeComposition,
) -> _PublicationFingerprint:
    """Observe only public/runtime projection state inside the app owner."""

    with runtime_composition_scope(composition):
        effective = tuple(
            sorted(
                (
                    spec.uri,
                    spec.edition,
                    str(spec.source_identity),
                    _sha256(spec.read()),
                )
                for spec in core_server.effective_resource_catalog().specs()
                if spec.enabled
            )
        )
        transport = tuple(
            sorted(
                (str(resource.uri), _sha256(resource.fn()))
                for resource in core_server.mcp.iter_resources()
                if resource.enabled
            )
        )
        try:
            manifest = core_server.effective_resource_manifest()
        except McpResourceCatalogError as exc:
            manifest_state = ("unavailable", exc.code, 0)
        else:
            manifest_state = (
                "published",
                manifest.projection_identity,
                manifest.count,
            )

        effective_bytes = {uri: digest for uri, _edition, _source, digest in effective}
        transport_bytes = dict(transport)
        return _PublicationFingerprint(
            instruction_provider_ids=tuple(
                provider.provider_id
                for provider in core_server._instruction_providers()  # noqa: SLF001
            ),
            instructions_frozen=core_server._instruction_providers_frozen(),  # noqa: SLF001
            instructions_sha256=_sha256(core_server.mcp.instructions),
            effective_resources=effective,
            registry_projection=core_server.resource_registry_projection(),
            transport_resources=transport,
            resource_catalog_frozen=core_server.resource_catalog_is_frozen(),
            manifest_state=manifest_state,
            authoritative_matches_transport=effective_bytes == transport_bytes,
        )


def _resource_delta(
    before: tuple[tuple[Any, ...], ...],
    after: tuple[tuple[Any, ...], ...],
) -> str:
    before_by_uri = {str(item[0]): item[1:] for item in before}
    after_by_uri = {str(item[0]): item[1:] for item in after}
    return repr(
        {
            "added": sorted(after_by_uri.keys() - before_by_uri.keys()),
            "removed": sorted(before_by_uri.keys() - after_by_uri.keys()),
            "changed": sorted(
                uri
                for uri in before_by_uri.keys() & after_by_uri.keys()
                if before_by_uri[uri] != after_by_uri[uri]
            ),
        }
    )


def _publication_leaks(
    before: _PublicationFingerprint,
    after: _PublicationFingerprint,
) -> list[str]:
    leaks: list[str] = []
    resource_fields = {
        "effective_resources",
        "registry_projection",
        "transport_resources",
    }
    for field in fields(_PublicationFingerprint):
        prior = getattr(before, field.name)
        current = getattr(after, field.name)
        if prior == current:
            continue
        if field.name in resource_fields:
            detail = _resource_delta(prior, current)
        else:
            detail = f"before={prior!r}, after={current!r}"
        leaks.append(f"{field.name}: {detail}")
    return leaks


def _probe_catalog() -> StaticMcpResourceCatalog:
    return StaticMcpResourceCatalog(
        "community",
        (
            McpResourceSpec(
                uri=_PROBE_URI,
                description="test-only Community cold-start probe",
                category="community",
                edition="community",
                kind=RESOURCE_KIND_OPERATIONAL,
                content="community cold-start probe body",
                source_identity="community:test-only-cold-start-probe",
            ),
        ),
        precedence=20,
    )


def _undeclared_collision_catalog() -> StaticMcpResourceCatalog:
    return StaticMcpResourceCatalog(
        "community",
        (
            McpResourceSpec(
                uri=_COLLISION_URI,
                description="test-only undeclared cross-edition collision",
                category="workflows",
                edition="community",
                kind=RESOURCE_KIND_OPERATIONAL,
                content="must never be published",
                source_identity="community:test-only-collision",
            ),
        ),
        precedence=20,
    )


@pytest.fixture
def _fresh_main_module() -> Iterator[None]:
    """Force the module-level app construction to execute for every case."""

    missing = object()
    previous_module = sys.modules.pop(_MAIN_MODULE, None)
    previous_attribute: object = vars(community_package).pop("main", missing)
    try:
        yield
    finally:
        sys.modules.pop(_MAIN_MODULE, None)
        if previous_module is not None:
            sys.modules[_MAIN_MODULE] = previous_module
        if previous_attribute is missing:
            vars(community_package).pop("main", None)
        else:
            vars(community_package)["main"] = previous_attribute


@pytest.mark.parametrize(
    "stage",
    (
        "cross_edition_collision_validation",
        "instruction_freeze",
        "resource_freeze_manifest_generation",
        "post_freeze_app_assembly",
        "module_app_wrapper",
        "community_host_materialization",
        "serve_configuration",
        "shutdown_signal_setup",
        "heartbeat_task_launch",
        "api_listener_task_launch",
        "mcp_listener_task_launch",
    ),
)
@pytest.mark.asyncio
async def test_failed_community_cold_start_publishes_nothing_atomically(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _fresh_main_module: None,
) -> None:
    core_server.reset_resource_catalog_for_tests()
    captured_compositions: list[RuntimeComposition] = []
    before_publication: list[_PublicationFingerprint] = []
    listener_serve_calls: list[int] = []
    ready_publications: list[tuple[int, int]] = []

    original_builder = composition_module.build_community_runtime_composition

    def _capture_composition(*args: Any, **kwargs: Any) -> RuntimeComposition:
        composition = original_builder(*args, **kwargs)
        captured_compositions.append(composition)
        before_publication.append(_publication_fingerprint(composition))
        return composition

    monkeypatch.setattr(
        composition_module,
        "build_community_runtime_composition",
        _capture_composition,
    )

    catalog = (
        _undeclared_collision_catalog()
        if stage == "cross_edition_collision_validation"
        else _probe_catalog()
    )
    monkeypatch.setattr(
        community_resources,
        "build_community_resource_catalog",
        lambda: catalog,
    )

    if stage == "instruction_freeze":
        original_refresh = core_server._refresh_catalog_instructions  # noqa: SLF001

        def _fail_freeze_refresh() -> None:
            if core_server._instruction_providers_frozen():  # noqa: SLF001
                raise _InjectedColdStartFailure(stage)
            original_refresh()

        monkeypatch.setattr(
            core_server,
            "_refresh_catalog_instructions",
            _fail_freeze_refresh,
        )
    elif stage == "resource_freeze_manifest_generation":

        def _fail_manifest(
            _cls: type[McpResourceManifest],
            _specs: tuple[McpResourceSpec, ...],
        ) -> McpResourceManifest:
            raise _InjectedColdStartFailure(stage)

        monkeypatch.setattr(
            McpResourceManifest,
            "from_specs",
            classmethod(_fail_manifest),
        )
    elif stage == "post_freeze_app_assembly":

        def _fail_app_assembly(*_args: object, **_kwargs: object) -> object:
            raise _InjectedColdStartFailure(stage)

        monkeypatch.setattr(
            community_app_module,
            "create_app",
            _fail_app_assembly,
        )
    elif stage == "module_app_wrapper":

        def _fail_module_wrapper(_app: object) -> object:
            raise _InjectedColdStartFailure(stage)

        monkeypatch.setattr(
            community_runtime_module,
            "AccessLogQueryRedactionMiddleware",
            _fail_module_wrapper,
        )
    elif stage == "community_host_materialization":

        def _fail_host_materialization(
            _self: object,
            _catalog: object,
            **_kwargs: object,
        ) -> object:
            raise _InjectedColdStartFailure(stage)

        monkeypatch.setattr(
            mcp_host_module.CommunityMcpHostProvider,
            "materialize_catalog",
            _fail_host_materialization,
        )

    class _FakeConfig:
        def __init__(self, app: object, **kwargs: object) -> None:
            self.app = app
            self.kwargs = kwargs

    class _FakeServer:
        instances = 0

        def __init__(self, config: _FakeConfig) -> None:
            self.config = config
            self.instance_id = type(self).instances
            type(self).instances += 1
            self.started = True
            self.should_exit = False
            self.force_exit = False
            self.capture_signals = None

        async def serve(self) -> None:
            listener_serve_calls.append(self.instance_id)
            await asyncio.sleep(0)

    monkeypatch.setattr(uvicorn, "Config", _FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    async def _attempt_full_cold_start() -> None:
        main_module = importlib.import_module(_MAIN_MODULE)
        assert isinstance(main_module, ModuleType)

        if stage == "serve_configuration":
            # Build the app under the REAL settings; the CommunitySettings
            # patch below must only hit the _serve_dual serve-config read,
            # not the lazy get_module_app() composition build.
            main_module.get_module_app()

        original_create_task = main_module.asyncio.create_task
        launch_failure_by_stage = {
            "heartbeat_task_launch": "serve_lock_heartbeat",
            "api_listener_task_launch": "api_ui_server",
            "mcp_listener_task_launch": "mcp_server",
        }

        def _fail_named_launch_task(coroutine, *args, **kwargs):
            if kwargs.get("name") == launch_failure_by_stage.get(stage):
                raise _InjectedColdStartFailure(stage)
            return original_create_task(coroutine, *args, **kwargs)

        if stage in launch_failure_by_stage:
            monkeypatch.setattr(
                main_module.asyncio,
                "create_task",
                _fail_named_launch_task,
            )
        if stage == "serve_configuration":

            def _fail_serve_configuration() -> object:
                raise _InjectedColdStartFailure(stage)

            monkeypatch.setattr(
                main_module,
                "CommunitySettings",
                _fail_serve_configuration,
            )

        async def _idle_heartbeat() -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(main_module, "_lock_heartbeat_loop", _idle_heartbeat)
        def _install_signal_handlers(_callback):
            if stage == "shutdown_signal_setup":
                raise _InjectedColdStartFailure(stage)
            return ()

        monkeypatch.setattr(
            main_module,
            "_install_shutdown_signal_handlers",
            _install_signal_handlers,
        )
        monkeypatch.setattr(
            main_module,
            "_log_ready_servers",
            lambda api_port, mcp_port: ready_publications.append(
                (api_port, mcp_port)
            ),
        )
        try:
            await main_module._serve_dual(api_port=0, mcp_port=0)
        finally:
            if stage in launch_failure_by_stage:
                monkeypatch.setattr(
                    main_module.asyncio,
                    "create_task",
                    original_create_task,
                )

    if stage == "cross_edition_collision_validation":
        with pytest.raises(McpResourceCatalogError) as exc_info:
            await _attempt_full_cold_start()
        assert exc_info.value.code == "undeclared_replace"
        assert exc_info.value.uri == _COLLISION_URI
    else:
        with pytest.raises(_InjectedColdStartFailure) as exc_info:
            await _attempt_full_cold_start()
        assert exc_info.value.stage == stage

    assert len(captured_compositions) == 1
    assert len(before_publication) == 1
    assert listener_serve_calls == [], (
        f"{stage} started a listener before cold-start commit: "
        f"{listener_serve_calls}"
    )
    assert ready_publications == [], (
        f"{stage} published ready before cold-start commit: {ready_publications}"
    )
    assert not any(
        _READY_MESSAGE in record.getMessage() for record in caplog.records
    ), f"{stage} emitted the ready log before cold-start commit"

    after_publication = _publication_fingerprint(captured_compositions[0])
    leaks = _publication_leaks(before_publication[0], after_publication)
    assert leaks == [], (
        f"{stage} left observable partial cold-start publication:\n- "
        + "\n- ".join(leaks)
    )
