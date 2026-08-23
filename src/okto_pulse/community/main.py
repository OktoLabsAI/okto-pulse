"""Community edition application entry point."""

# ruff: noqa: E402

import warnings

warnings.filterwarnings(
    "ignore",
    message=r"urllib3.*or chardet.*doesn't match a supported version",
    category=Warning,
)

import asyncio
import contextlib
import faulthandler
import logging
import math
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import AsyncGenerator, Callable

import uvicorn
from fastapi.staticfiles import StaticFiles

from okto_pulse.community.app import (
    create_app,
    register_kg_daily_tick_job,
    run_startup_schema_sweep,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    close_db,
    get_session_factory,
    init_db,
)
from okto_pulse.core.services.application_kg import get_current_provider_registry

# NOTE: MCP server is imported lazily inside create_community_app (after
# create_app has called configure_settings) and inside combined_lifespan
# (after init_db). Module-level import would cache the default settings
# singleton via get_settings() at import time and break runtime config.
# Settings cache trap respected by Spec 23350275 (Fix C, BR5).
from okto_pulse.community.adapters.composition import (
    community_storage_provider,
    configure_community_kg_registry,
)
from okto_pulse.community.adapters.embedding import CommunityStubEmbeddingProvider
from okto_pulse.community.adapters.scheduler import SingletonSchedulerControl
from okto_pulse.community.adapters.workers import build_community_worker_registry
from okto_pulse.community.auth import LocalAuthProvider
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.local_secrets import (
    provision_guideline_policy_cursor_signing_key,
)
from okto_pulse.community.api import metrics_router
from okto_pulse.community.runtime import (
    AccessLogQueryRedactionMiddleware,
    build_uvicorn_log_config,
    run_async_server,
    set_shutdown_log_suppression,
)
from okto_pulse.community.seed import seed_community_defaults

_EMBEDDING_LOGGER = logging.getLogger("okto_pulse.community.embedding")
_STARTUP_LOGGER = logging.getLogger("uvicorn.error")
_METRICS_LOGGER = logging.getLogger("okto_pulse.community.metrics")
_LOCK_LOGGER = logging.getLogger("okto_pulse.community.serve_lock")
_NATIVE_DIAGNOSTICS_LOGGER = logging.getLogger(
    "okto_pulse.community.native_diagnostics"
)
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 120.0
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 15.0
# KGD-01 (TR7): budget próprio do checkpoint+close dos grafos no teardown —
# MENOR que o force-exit de 15s do _shutdown_server_pair, para que a etapa
# complete (ou desista logando) antes de qualquer cancel forçado.
_GRAPH_CLOSE_SHUTDOWN_TIMEOUT_S = 10.0
_RECOVERY_NATIVE_DRAIN_TIMEOUT_S = 5.0
_DEFAULT_METRICS_BEACON_INTERVAL_SECONDS = 3600.0


def _log_native_runtime_budget() -> None:
    """Publish one structured post-composition native runtime budget event."""

    from okto_pulse.community.adapters.kg_runtime import (
        build_native_runtime_budget_snapshot,
    )

    snapshot = build_native_runtime_budget_snapshot()
    requested = dict(snapshot.requested)
    normalized = dict(snapshot.normalized)
    effective = dict(snapshot.effective)
    sources = dict(snapshot.sources)
    process_envelope = dict(snapshot.process_envelope)
    _STARTUP_LOGGER.info(
        "kg.native_runtime_budget effective=%s process_envelope=%s",
        effective,
        process_envelope,
        extra={
            "event": "kg.native_runtime_budget",
            "source": snapshot.source,
            "status": snapshot.status,
            "requested": requested,
            "normalized": normalized,
            "effective": effective,
            "sources": sources,
            "process_envelope": process_envelope,
            "is_direct_memory_telemetry": False,
        },
    )


_DEFAULT_METRICS_BEACON_STARTUP_DELAY_SECONDS = 60.0
_METRICS_LOG_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")

# Preload retry policy: 3 attempts, exponential backoff (2s, 4s, 8s), 30s total budget.
# Only transient network errors retry. ImportError / OSError (disk full) / ValueError
# are deterministic and should fail fast — no amount of retrying will fix them.
_EMBEDDING_PRELOAD_ATTEMPTS = 3
_EMBEDDING_PRELOAD_BACKOFF_S = (2.0, 4.0, 8.0)
_EMBEDDING_PRELOAD_BUDGET_S = 30.0
_EMBEDDING_PRELOAD_TASKS: set[asyncio.Task[None]] = set()


def _enable_native_crash_diagnostics() -> None:
    """Enable Python's fatal-signal traceback without blocking startup.

    Ladybug/Kuzu executes in a native extension.  A process-level access
    violation bypasses Python exception handlers; ``faulthandler`` preserves
    the Python stacks of every thread in stderr so a repeated native crash has
    an actionable call site.  Some embedded/service hosts expose no usable
    stderr file descriptor, so diagnostics remain best-effort.
    """

    if faulthandler.is_enabled():
        return
    try:
        faulthandler.enable(all_threads=True)
    except (OSError, RuntimeError, ValueError) as exc:
        _NATIVE_DIAGNOSTICS_LOGGER.warning(
            "native crash diagnostics unavailable error_type=%s",
            type(exc).__name__,
            extra={
                "event": "community.native_diagnostics.unavailable",
                "error_type": type(exc).__name__,
            },
        )


def _startup_timeout_seconds() -> float:
    """Return the readiness timeout used while uvicorn lifespans complete."""
    raw = os.environ.get("OKTO_PULSE_STARTUP_TIMEOUT_SECONDS") or os.environ.get(
        "OKTO_PULSE_STARTUP_TIMEOUT"
    )
    if not raw:
        return _DEFAULT_STARTUP_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        _STARTUP_LOGGER.warning(
            "Invalid OKTO_PULSE_STARTUP_TIMEOUT_SECONDS=%r; using %.0fs.",
            raw,
            _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        )
        return _DEFAULT_STARTUP_TIMEOUT_SECONDS
    if timeout < 1:
        _STARTUP_LOGGER.warning(
            "OKTO_PULSE_STARTUP_TIMEOUT_SECONDS must be >= 1; using %.0fs.",
            _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        )
        return _DEFAULT_STARTUP_TIMEOUT_SECONDS
    return timeout


def _shutdown_timeout_seconds() -> float:
    """Return the graceful shutdown timeout for open HTTP/WebSocket streams."""
    raw = os.environ.get("OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS") or os.environ.get(
        "OKTO_PULSE_SHUTDOWN_TIMEOUT"
    )
    if not raw:
        return _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        _STARTUP_LOGGER.warning(
            "Invalid OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS=%r; using %.0fs.",
            raw,
            _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        )
        return _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    if timeout < 1:
        _STARTUP_LOGGER.warning(
            "OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS must be >= 1; using %.0fs.",
            _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        )
        return _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    return timeout


def _metrics_beacon_timing() -> tuple[float, float]:
    def _read_seconds(name: str, default: float, minimum: float) -> float:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            _METRICS_LOGGER.warning("Invalid %s=%r; using %.0fs.", name, raw, default)
            return default
        return max(minimum, value)

    interval = _read_seconds(
        "OKTO_PULSE_METRICS_BEACON_INTERVAL_SECONDS",
        _DEFAULT_METRICS_BEACON_INTERVAL_SECONDS,
        60.0,
    )
    startup_delay = _read_seconds(
        "OKTO_PULSE_METRICS_BEACON_STARTUP_DELAY_SECONDS",
        min(_DEFAULT_METRICS_BEACON_STARTUP_DELAY_SECONDS, interval),
        5.0,
    )
    return interval, min(startup_delay, interval)


async def _metrics_publish_cycle(
    settings: CommunitySettings, *, sender=None
) -> tuple[dict, dict]:
    """Run both publish legs independently so one cannot starve the other."""

    if sender is None:
        from okto_pulse.core.telemetry.sender_registry import get_telemetry_sender

        sender = get_telemetry_sender(settings)

    async def _run_leg(name: str, operation: Callable[[], dict]) -> dict:
        try:
            return await asyncio.to_thread(operation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_class = type(exc).__name__
            if not _METRICS_LOG_LABEL.fullmatch(error_class):
                error_class = "Exception"
            _METRICS_LOGGER.warning(
                "metrics.publish_leg.failed leg=%s error_class=%s",
                name,
                error_class,
                extra={
                    "event": "metrics.publish_leg.failed",
                    "leg": name,
                    "error_class": error_class,
                },
            )
            return {
                "sent": False,
                "reason": "unhandled_exception",
                "error_class": error_class,
            }

    delta = await _run_leg("usage", sender.send_pending)
    snapshot = await _run_leg(
        "product_snapshot",
        sender.publish_product_snapshot,
    )
    return delta, snapshot


def _safe_metrics_reason(value: object) -> str:
    reason = str(value or "unknown")
    return reason if _METRICS_LOG_LABEL.fullmatch(reason) else "unclassified"


async def _metrics_beacon_loop(settings: CommunitySettings) -> None:
    interval, delay = _metrics_beacon_timing()
    # R10-C: resolve the REGISTERED telemetry sender (the Community TelemetrySink
    # adapter) through the port registry — never import the concrete core sender.
    from okto_pulse.core.telemetry.sender_registry import get_telemetry_sender

    sender = get_telemetry_sender(settings)
    while True:
        await asyncio.sleep(delay)
        delay = interval
        result, snapshot_result = await _metrics_publish_cycle(
            settings,
            sender=sender,
        )
        if result.get("sent"):
            _METRICS_LOGGER.info(
                "metrics.beacon.sent batch_seq=%s",
                result.get("batch_seq"),
                extra={
                    "event": "metrics.beacon.sent",
                    "batch_seq": result.get("batch_seq"),
                },
            )
        elif result.get("reason") not in {"not_enabled", "empty"}:
            reason = _safe_metrics_reason(result.get("reason"))
            _METRICS_LOGGER.info(
                "metrics.beacon.skipped reason=%s",
                reason,
                extra={
                    "event": "metrics.beacon.skipped",
                    "reason": reason,
                },
            )
        if snapshot_result.get("sent"):
            _METRICS_LOGGER.info(
                "metrics.product_snapshot.sent batch_seq=%s",
                snapshot_result.get("batch_seq"),
                extra={
                    "event": "metrics.product_snapshot.sent",
                    "batch_seq": snapshot_result.get("batch_seq"),
                },
            )
        elif snapshot_result.get("reason") not in {"not_enabled", "empty"}:
            reason = _safe_metrics_reason(snapshot_result.get("reason"))
            _METRICS_LOGGER.info(
                "metrics.product_snapshot.pending reason=%s",
                reason,
                extra={
                    "event": "metrics.product_snapshot.pending",
                    "reason": reason,
                },
            )


def _is_transient_network_error(exc: BaseException) -> bool:
    """Return True when `exc` is worth retrying (network glitch / timeout)."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    # requests is a transitive dep of sentence-transformers / huggingface_hub;
    # import it lazily so the preload logic does not pay the import cost when
    # the provider is already loaded.
    try:
        import requests  # type: ignore
    except ImportError:
        return False
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _track_deferred_embedding_preload(
    task: asyncio.Task[None],
    *,
    registry,
    provider,
    model_name: str | None,
    dim: int,
    started: float,
    attempt: int,
) -> None:
    """Keep a timed-out warm-up alive and publish its eventual outcome."""

    _EMBEDDING_PRELOAD_TASKS.add(task)

    def _on_done(completed: asyncio.Task[None]) -> None:
        _EMBEDDING_PRELOAD_TASKS.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception as exc:
            if registry.embedding_provider is provider:
                registry.embedding_provider = CommunityStubEmbeddingProvider(dim=dim)
            _EMBEDDING_LOGGER.warning(
                "kg.embedding.load_failed",
                extra={
                    "event": "kg.embedding.load_failed",
                    "reason": "deferred_load_failed",
                    "model": model_name,
                    "error": repr(exc),
                    "fallback": "CommunityStubEmbeddingProvider",
                    "deferred": True,
                },
            )
            return

        duration_ms = int((time.monotonic() - started) * 1000)
        _EMBEDDING_LOGGER.info(
            "kg.embedding.loaded",
            extra={
                "event": "kg.embedding.loaded",
                "model": model_name,
                "dimension": dim,
                "duration_ms": duration_ms,
                "attempt": attempt,
                "deferred": True,
            },
        )

    task.add_done_callback(_on_done)


async def _preload_embedding_model(settings: CommunitySettings) -> None:
    """Preload the sentence-transformers model at startup.

    On success emits `kg.embedding.loaded`; on unrecoverable failure swaps
    the registry to a StubEmbeddingProvider and emits `kg.embedding.load_failed`
    so the server keeps serving (semantic search degrades, but app is up).
    """
    registry = get_current_provider_registry()
    provider = registry.embedding_provider
    # R05-B: capability/metadata-driven (NO isinstance against a concrete
    # provider). Only a non-stub provider that exposes preload() needs warming.
    meta = (
        provider.embedding_metadata() if hasattr(provider, "embedding_metadata") else {}
    )
    if meta.get("is_stub", True) or not hasattr(provider, "preload"):
        # Stub mode or a provider with no preload — nothing to do.
        return

    model_name = meta.get("model_name")
    dim = meta.get("embedding_dimension", settings.kg_embedding_dim)
    started = time.monotonic()

    last_exc: Exception | None = None
    for attempt in range(1, _EMBEDDING_PRELOAD_ATTEMPTS + 1):
        remaining = _EMBEDDING_PRELOAD_BUDGET_S - (time.monotonic() - started)
        if remaining <= 0:
            last_exc = last_exc or TimeoutError("embedding preload budget exhausted")
            break
        preload_task = asyncio.create_task(
            asyncio.to_thread(provider.preload),
            name=f"community.embedding.preload.{attempt}",
        )
        try:
            await asyncio.wait_for(asyncio.shield(preload_task), timeout=remaining)
            duration_ms = int((time.monotonic() - started) * 1000)
            _EMBEDDING_LOGGER.info(
                "kg.embedding.loaded",
                extra={
                    "event": "kg.embedding.loaded",
                    "model": model_name,
                    "dimension": dim,
                    "duration_ms": duration_ms,
                    "attempt": attempt,
                },
            )
            return
        except TimeoutError as exc:
            if not preload_task.done():
                _track_deferred_embedding_preload(
                    preload_task,
                    registry=registry,
                    provider=provider,
                    model_name=model_name,
                    dim=dim,
                    started=started,
                    attempt=attempt,
                )
                _EMBEDDING_LOGGER.warning(
                    "kg.embedding.load_deferred",
                    extra={
                        "event": "kg.embedding.load_deferred",
                        "reason": "startup_budget_exhausted",
                        "model": model_name,
                        "dimension": dim,
                        "budget_ms": int(_EMBEDDING_PRELOAD_BUDGET_S * 1000),
                        "attempt": attempt,
                    },
                )
                return
            last_exc = exc
        except Exception as exc:
            last_exc = exc
        if not _is_transient_network_error(last_exc):
            # ImportError, OSError, ValueError, etc. — fail fast.
            break
        if attempt < _EMBEDDING_PRELOAD_ATTEMPTS:
            backoff = _EMBEDDING_PRELOAD_BACKOFF_S[attempt - 1]
            remaining = _EMBEDDING_PRELOAD_BUDGET_S - (time.monotonic() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(backoff, remaining))

    # Degrade to the deterministic stub, preserving the dimension + event.
    registry.embedding_provider = CommunityStubEmbeddingProvider(dim=dim)
    _EMBEDDING_LOGGER.warning(
        "kg.embedding.load_failed",
        extra={
            "event": "kg.embedding.load_failed",
            "reason": "load_failed",
            "model": model_name,
            "error": repr(last_exc) if last_exc else "unknown",
            "fallback": "CommunityStubEmbeddingProvider",
        },
    )


# Frontend dist embedded in the package (built with VITE_AUTH_MODE=local)
FRONTEND_DIR = Path(__file__).parent / "frontend_dist"


def _ensure_data_dir(settings: CommunitySettings) -> None:
    """Create local state and provision installation-owned secrets."""
    data_path = Path(settings.data_dir).expanduser().resolve()
    kg_base = Path(settings.kg_base_dir).expanduser().resolve()
    metrics_path = Path(settings.metrics_dir).expanduser().resolve()
    for subdir in [
        data_path,
        data_path / "data",
        data_path / "uploads",
        kg_base / "boards",
        kg_base / "global",
        metrics_path,
        metrics_path / "events",
        metrics_path / "sent",
        metrics_path / "failures",
        metrics_path / "exports",
    ]:
        subdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(data_path), 0o700)
    except (OSError, NotImplementedError):
        pass
    provision_guideline_policy_cursor_signing_key(settings)


# The former _configure_sqlite_pragmas helper (a partial WAL + foreign_keys
# connect listener) was removed. SQLite hardening is now owned by the Community
# SQLAlchemy adapter as the single local-first runtime owner.


# Paths que nunca recebem o fallback SPA (API, docs, MCP e assets estáticos).
_SPA_PASSTHROUGH_PREFIXES = (
    "/api/",
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/mcp",
    "/config.js",
    "/assets",
)

_SPA_NO_CACHE_HEADERS = (
    (b"cache-control", b"no-store, no-cache, must-revalidate, max-age=0"),
    (b"pragma", b"no-cache"),
    (b"expires", b"0"),
)


class ImmutableFrontendStaticFiles(StaticFiles):
    """Serve content-hashed frontend assets with an immutable cache policy."""

    async def get_response(self, path, scope):  # noqa: ANN001, ANN201
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


class SPAFallbackMiddleware:
    """Fallback de SPA (404 → index.html) como ASGI puro.

    Root-cause fix (2026-06-09): a versão anterior era BaseHTTPMiddleware,
    que envolve TODA resposta — inclusive os SSE de
    ``/api/v1/kg/.../events`` — num task group do anyio. Na desconexão do
    cliente, o cancel scope hard-cancelava o generator no meio de awaits de
    DB, vazando conexões do pool (exaustão → "travamento"). Como ASGI puro,
    só intercepta o ``http.response.start``: 404 em path de SPA vira
    index.html; todo o resto passa direto, sem cancel scope adicional.
    """

    def __init__(self, app, index_body: bytes) -> None:
        self.app = app
        self.index_body = index_body

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if any(path.startswith(p) for p in _SPA_PASSTHROUGH_PREFIXES):
            await self.app(scope, receive, send)
            return

        replaced = False
        index_body = self.index_body

        async def send_wrapper(message) -> None:
            nonlocal replaced
            if message["type"] == "http.response.start" and message["status"] == 404:
                replaced = True
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"text/html; charset=utf-8"),
                            (b"content-length", str(len(index_body)).encode("ascii")),
                            *_SPA_NO_CACHE_HEADERS,
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": index_body,
                        "more_body": False,
                    }
                )
                return
            if replaced:
                # Descarta o body do 404 original — a resposta SPA já foi
                # enviada por inteiro acima.
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _mount_frontend(
    app,
    frontend_dir: Path,
    api_port: int = 8100,
    mcp_port: int = 8101,
    public_host: str = "127.0.0.1",
    explicit_public_host: bool = False,
    public_api_port: int | None = None,
    public_mcp_port: int | None = None,
) -> None:
    """Mount the pre-built frontend SPA on the FastAPI app.

    Uses Starlette middleware approach to avoid route-ordering issues:
    - /assets/* → StaticFiles mount (correct MIME types)
    - /api/*, /health, /docs, /openapi.json → pass through to FastAPI
    - Everything else → index.html (SPA routing)

    Injects runtime configuration for API and MCP ports to support custom ports.
    Set PUBLIC_HOST / PUBLIC_API_PORT / PUBLIC_MCP_PORT env vars to override the
    URLs the browser SPA uses (needed when behind a reverse proxy or NAT).
    """
    if not frontend_dir.exists():
        return

    assets_dir = frontend_dir / "assets"
    if assets_dir.exists():
        app.mount(
            "/assets",
            ImmutableFrontendStaticFiles(directory=str(assets_dir)),
            name="frontend-assets",
        )

    index_html_path = frontend_dir / "index.html"
    if not index_html_path.exists():
        return

    # Read index.html content once for injection
    index_html_content = index_html_path.read_text()

    # Inject config.js script tag before the closing </head> tag
    config_script_tag = '  <script src="/config.js"></script>\n'
    if "</head>" in index_html_content:
        injected_index_html = index_html_content.replace(
            "</head>", config_script_tag + "</head>"
        )
    else:
        injected_index_html = index_html_content

    # Inject runtime configuration BEFORE SPA middleware.
    # PUBLIC_* env vars override the URLs the browser SPA uses — set them when
    # the server is accessed through a different host/port than the internal bind
    # (e.g. NAT, reverse proxy, or LAN deployment).
    _pub_host = public_host
    _pub_api_port = public_api_port if public_api_port is not None else api_port
    _pub_mcp_port = public_mcp_port if public_mcp_port is not None else mcp_port
    _api_url = (
        f"http://{_pub_host}:{_pub_api_port}/api/v1"
        if explicit_public_host or public_api_port is not None
        else "/api/v1"
    )
    config_script = f"""
// Runtime configuration injected by server
window.OKTO_PULSE_CONFIG = {{
    API_URL: '{_api_url}',
    MCP_URL: 'http://{_pub_host}:{_pub_mcp_port}'
}};
"""

    # Add a route to inject config (must be BEFORE SPA middleware)
    @app.get("/config.js")
    async def get_config():
        from fastapi.responses import Response

        return Response(
            content=config_script,
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    app.add_middleware(
        SPAFallbackMiddleware, index_body=injected_index_html.encode("utf-8")
    )


async def _close_graphs_on_teardown() -> None:
    """KGD-01 (FR2/BR3/TR7): checkpoint+close de todos os board graphs abertos.

    Roda em ``asyncio.to_thread`` (o close guard drena leitores — bloquear o
    event loop impediria os próprios leitores de saírem) e é protegido contra
    cancelamento: o force-exit de 15s do ``_shutdown_server_pair`` cancela a
    task do servidor — e com ela o teardown do lifespan — mas esta etapa NÃO
    pode ser abandonada com um CHECKPOINT em curso (escritor stale zera o
    WAL). O ``asyncio.shield`` mantém a task interna viva através de cancels
    repetidos, dentro de um budget próprio menor que o force-exit
    (``_GRAPH_CLOSE_SHUTDOWN_TIMEOUT_S``). Mesmo no pior caso (budget
    estourado), a worker thread NÃO é abandonada antes do exit do processo:
    ``asyncio.Runner``/``asyncio.run`` aguardam o default executor no close
    do loop (``loop.shutdown_default_executor``). Nunca propaga exceção — o
    teardown segue para o close do SQLite.
    """
    from okto_pulse.community.adapters.kg_shutdown import (
        close_all_graphs_on_shutdown,
    )

    loop = asyncio.get_running_loop()
    close_task = asyncio.ensure_future(asyncio.to_thread(close_all_graphs_on_shutdown))
    deadline = loop.time() + _GRAPH_CLOSE_SHUTDOWN_TIMEOUT_S
    while not close_task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            _STARTUP_LOGGER.warning(
                "kg.shutdown.graphs_close_timeout timeout_s=%.0f — o close "
                "continua na worker thread (aguardada no shutdown do executor)",
                _GRAPH_CLOSE_SHUTDOWN_TIMEOUT_S,
                extra={
                    "event": "kg.shutdown.graphs_close_timeout",
                    "timeout_s": _GRAPH_CLOSE_SHUTDOWN_TIMEOUT_S,
                },
            )
            return
        try:
            await asyncio.wait_for(asyncio.shield(close_task), timeout=remaining)
        except asyncio.CancelledError:
            # Cancel do force-exit/runner durante o teardown: NÃO abandonar o
            # checkpoint+close — continua aguardando dentro do budget próprio.
            continue
        except asyncio.TimeoutError:
            continue  # o while reavalia o deadline e loga o timeout acima
        except Exception as exc:  # noqa: BLE001 — teardown nunca aborta aqui
            _STARTUP_LOGGER.warning(
                "kg.shutdown.graphs_close_failed err=%s",
                exc,
                extra={"event": "kg.shutdown.graphs_close_failed"},
            )
            return
    if not close_task.cancelled() and close_task.exception() is None:
        summary = close_task.result()
        # Linha legível no console (uvicorn.error) — o evento estruturado
        # kg.shutdown.graphs_closed é emitido pelo próprio kg_shutdown.
        _STARTUP_LOGGER.info(
            "KG graphs closed on shutdown "
            "(boards_closed=%s boards_failed=%s duration_ms=%s)",
            summary.get("boards_closed"),
            summary.get("boards_failed"),
            summary.get("duration_ms"),
        )


def _compose_global_discovery_recovery_runtime(settings: CommunitySettings):
    """Compose and publish recovery only after relational schema startup."""

    from okto_pulse.community.adapters.board_source_reader import (
        resolve_pulse_db_path,
    )
    from okto_pulse.community.adapters.global_discovery_recovery import (
        CommunityPreparedRecoveryRevoker,
        CommunityRecoverySnapshotFingerprint,
        CommunityRelationalRecoverySnapshotFingerprint,
    )
    from okto_pulse.community.adapters.global_discovery_recovery_preparation import (
        CommunityGlobalDiscoveryRecoveryPreparationOperation,
    )
    from okto_pulse.community.adapters.global_discovery_recovery_worker import (
        CommunityDurableRecoveryInputProvider,
        build_community_recovery_runtime,
    )
    from okto_pulse.core.ports.global_discovery_recovery_control import (
        CognitivePendingOverlaySnapshotService,
        register_recovery_control_plane,
        resolve_global_discovery_recovery_runtime_dependencies,
    )
    from okto_pulse.core.ports.materialization_health import (
        get_materialization_evidence_port,
    )
    from okto_pulse.core.runtime_registry import resolve_unit_of_work_factory

    recovery, artifact_store = resolve_global_discovery_recovery_runtime_dependencies()
    bind_snapshot_fingerprint = getattr(
        recovery,
        "bind_snapshot_fingerprint_provider",
        None,
    )
    if not callable(bind_snapshot_fingerprint):
        raise RuntimeError("recovery_snapshot_fingerprint_binding_unavailable")
    database_path = Path(resolve_pulse_db_path()).resolve()

    def recovery_database_path() -> Path:
        """Return the startup-resolved path from context-free worker threads."""

        return database_path

    relational_fingerprint = CommunityRelationalRecoverySnapshotFingerprint(
        db_path_provider=recovery_database_path,
    )
    overlay_snapshot = CognitivePendingOverlaySnapshotService(
        artifact_store=artifact_store,
    )
    snapshot_fingerprint = CommunityRecoverySnapshotFingerprint(
        relational=relational_fingerprint,
        cognitive_overlay=overlay_snapshot,
    )
    bind_snapshot_fingerprint(snapshot_fingerprint)
    input_provider = CommunityDurableRecoveryInputProvider(
        artifact_store=artifact_store
    )
    prepared_revoker = CommunityPreparedRecoveryRevoker(
        artifact_store=artifact_store,
    )
    preparation_operation = CommunityGlobalDiscoveryRecoveryPreparationOperation(
        recovery=recovery,
        artifact_store=artifact_store,
        db_path_provider=recovery_database_path,
        unit_of_work_factory=resolve_unit_of_work_factory(),
        materialization_evidence_port=get_materialization_evidence_port(),
        relational_fingerprint=relational_fingerprint,
        overlay_snapshot_service=overlay_snapshot,
        snapshot_fingerprint=snapshot_fingerprint,
    )
    runtime = build_community_recovery_runtime(
        database_url=settings.database_url,
        recovery=recovery,
        input_provider=input_provider,
        prepared_revoker=prepared_revoker,
        preparation_operation=preparation_operation,
    )
    try:
        register_recovery_control_plane(runtime.control)
    except Exception:
        with contextlib.suppress(Exception):
            runtime.close(timeout_seconds=0.0)
        raise
    return runtime


async def _drain_global_discovery_recovery_runtime(
    runtime,
    *,
    timeout_seconds: float,
) -> bool:
    """Unpublish first, then drain native work off the event loop."""

    from okto_pulse.core.ports.global_discovery_recovery_control import (
        reset_recovery_control_plane,
    )

    reset_recovery_control_plane()
    try:
        await asyncio.to_thread(runtime.close, timeout_seconds=timeout_seconds)
    except Exception:
        return False
    return True


def create_community_app():
    """Create the community FastAPI application with embedded frontend."""
    settings = CommunitySettings()

    # Read ports from environment (set by CLI) or use defaults
    api_port = int(os.environ.get("OKTO_PULSE_PORT", str(settings.port)))
    mcp_port = int(os.environ.get("OKTO_PULSE_MCP_PORT", str(settings.mcp_port)))
    # Public-facing host/ports for the browser SPA config.js.
    # Override when the container is accessed through a different host/port
    # than the internal bind address (NAT, reverse proxy, LAN deployment).
    public_host_env = os.environ.get("PUBLIC_HOST")
    public_host = public_host_env or "127.0.0.1"
    public_api_port_env = os.environ.get("PUBLIC_API_PORT")
    public_mcp_port_env = os.environ.get("PUBLIC_MCP_PORT")
    public_api_port = int(public_api_port_env) if public_api_port_env else None
    public_mcp_port = int(public_mcp_port_env) if public_mcp_port_env else None

    _ensure_data_dir(settings)

    auth = LocalAuthProvider()
    storage = community_storage_provider(settings.upload_dir)
    scheduler_control = SingletonSchedulerControl()

    # Combined lifespan: seed data, preload embeddings, start KG workers,
    # register the MCP session factory so the mounted sub-app finds the DB.
    async def combined_lifespan(app_instance) -> AsyncGenerator[None, None]:
        await init_db()

        # Rehydrate the composed settings snapshot immediately after schema
        # initialization. First-boot seeding materializes the demo graph, so it
        # must observe persisted graph-memory limits just like every later
        # background task and graph constructor.
        from okto_pulse.community.adapters.sqlalchemy_runtime_settings_service import (
            apply_persisted_settings_to_core_settings,
        )

        await apply_persisted_settings_to_core_settings()
        _log_native_runtime_budget()

        from okto_pulse.community.adapters.kg_events import (
            register_community_kg_events_reader,
        )

        register_community_kg_events_reader(get_session_factory())

        primary_commit_delivered = False

        def _on_primary_committed(board, agent, api_key) -> None:
            nonlocal primary_commit_delivered
            primary_commit_delivered = True
            print(f"\n{'=' * 60}")
            print("  Okto Pulse Community — First Boot Setup")
            print(f"{'=' * 60}")
            print(f"  Board created: {board.name} ({board.id})")
            print(f"  Agent created: {agent.name}")
            print(f"  API Key: {api_key}")
            print(f"  MCP URL: http://localhost:{mcp_port}/mcp?api_key={api_key}")
            print(f"{'=' * 60}\n")

        async with database_runtime.cancel_safe_session_scope() as db:
            result = await seed_community_defaults(
                db,
                on_primary_committed=_on_primary_committed,
            )
            if result and not primary_commit_delivered:
                # Compatibility with an older external seed implementation.
                board, agent, api_key = result
                _on_primary_committed(board, agent, api_key)

        # Self-heal: Q&A respondidas herdadas sem answered_at viravam
        # falso-abertas no badge open_qa_count (a herança não copiava o
        # timestamp). O combined_lifespan SUBSTITUI o _default_lifespan do
        # core, então o backfill precisa rodar aqui também. Idempotente.
        try:
            from okto_pulse.core.services.application_startup import (
                backfill_qa_answered_at,
            )

            async with database_runtime.cancel_safe_session_scope() as _qa_db:
                _qa_fixed = await backfill_qa_answered_at(_qa_db)
            if _qa_fixed:
                _STARTUP_LOGGER.info(
                    "qa.answered_at.backfilled %s",
                    _qa_fixed,
                )
        except Exception as _qa_exc:
            _STARTUP_LOGGER.warning(
                "qa.answered_at.backfill_failed err=%s",
                _qa_exc,
            )

        # NC-10 parity with create_app's default lifespan.  The productive
        # combined lifespan replaces that default completely, so it must run
        # the idempotent per-board KG schema sweep itself.  Keep this before
        # every worker and the decay scheduler so they never observe a
        # pre-migration board graph.
        try:
            await run_startup_schema_sweep(
                uow_factory=app_instance.state.runtime_composition.uow_factory,
                logger=_STARTUP_LOGGER,
            )
        except Exception as _schema_exc:
            _STARTUP_LOGGER.debug(
                "kg.schema.migration_skipped err=%s",
                _schema_exc,
                extra={"event": "kg.schema.migration_skipped"},
            )

        # Self-heal AFG (investigacao 2026-06-10): materializa finding runs
        # de arquitetura para designs nunca avaliados (gate avaliava tabela
        # vazia). Em background para nao atrasar o boot; o combined_lifespan
        # substitui o lifespan default do core, entao o wiring vive aqui.
        async def _afg_backfill_task() -> None:
            try:
                from okto_pulse.core.services.architecture import (
                    backfill_architecture_finding_runs,
                )

                async with database_runtime.cancel_safe_session_scope() as _afg_db:
                    _afg_stats = await backfill_architecture_finding_runs(
                        _afg_db,
                        only_missing=True,
                    )
                _STARTUP_LOGGER.info(
                    "architecture.finding_backfill.completed %s",
                    _afg_stats,
                )
            except Exception as _afg_exc:
                _STARTUP_LOGGER.warning(
                    "architecture.finding_backfill.failed err=%s",
                    _afg_exc,
                )

        afg_backfill_task = asyncio.create_task(
            _afg_backfill_task(),
            name="community.startup.architecture_backfill",
        )

        # Preload the embedding model before serving requests so the first
        # KG search doesn't pay the multi-second model-load cost synchronously.
        await _preload_embedding_model(settings)

        metrics_beacon_task = None

        worker_registry = app_instance.state.runtime_composition.worker_registry
        await worker_registry.start_all()

        # Core owns the KG decay job policy as JobSpec; Community owns the
        # APScheduler runtime and maps the JobSpec through SchedulerControl.
        await register_kg_daily_tick_job(
            scheduler_control,
            logger=_STARTUP_LOGGER,
        )

        # R10-B/R10-C/R10-D: register the Community telemetry providers BEFORE the
        # metrics beacon loop starts, so the core telemetry runtime resolves the
        # Community store / product aggregator / publish-health sources / beacon
        # sender through their ports (not the gated fallback shims).
        from okto_pulse.community.adapters.telemetry_composition import (
            register_community_telemetry_runtime,
        )

        register_community_telemetry_runtime()

        metrics_beacon_task = asyncio.create_task(
            _metrics_beacon_loop(settings),
            name="community.metrics_beacon",
        )

        recovery_runtime = _compose_global_discovery_recovery_runtime(settings)
        app_instance.state.global_discovery_recovery_runtime = recovery_runtime

        yield

        if metrics_beacon_task is not None:
            metrics_beacon_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await metrics_beacon_task
        if not afg_backfill_task.done():
            afg_backfill_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await afg_backfill_task
        await scheduler_control.shutdown(wait=False)
        worker_stop_failures = ()
        if worker_registry is not None:
            worker_stop_failures = await worker_registry.stop_all()
            for failure in worker_stop_failures:
                _STARTUP_LOGGER.warning(
                    "community.worker.stop_failed family=%s err=%s",
                    failure.family,
                    failure.message,
                )
        recovery_drained = await _drain_global_discovery_recovery_runtime(
            recovery_runtime,
            timeout_seconds=_RECOVERY_NATIVE_DRAIN_TIMEOUT_S,
        )
        blocked_families = sorted(
            failure.family
            for failure in worker_stop_failures
            if failure.resource_close_unsafe
        )
        if not recovery_drained:
            blocked_families.append("global_discovery_recovery")
        blocked_families.sort()
        native_drain_incomplete = bool(blocked_families)
        if native_drain_incomplete:
            _STARTUP_LOGGER.critical(
                "community.shutdown.native_drain_incomplete "
                "families=%s graph_close_skipped=true db_close_skipped=true",
                ",".join(blocked_families),
                extra={
                    "event": "community.shutdown.native_drain_incomplete",
                    "families": blocked_families,
                    "graph_close_skipped": True,
                    "db_close_skipped": True,
                },
            )
        from okto_pulse.core.application.kg_events_hub import shutdown_kg_events_hub

        await shutdown_kg_events_hub()
        # KGD-01 (FR2/BR3/TR7): paridade com o _default_lifespan do core
        # (shutdown_kg_then_db) — checkpoint+close de todos os board graphs
        # DEPOIS da parada dos workers e ANTES do close do SQLite; sem isto o
        # graph.lbug.wal fica como único portador dos commits recentes.
        if not native_drain_incomplete:
            await _close_graphs_on_teardown()
            await close_db()

    from okto_pulse.community.adapters.sqlalchemy_database import (
        configure_community_database,
    )
    from okto_pulse.community.adapters.coordination import (
        register_community_coordination_providers,
    )
    from okto_pulse.community.adapters.mcp_host import (
        configure_community_mcp_admission,
        register_community_mcp_host,
    )

    register_community_coordination_providers()
    configure_community_mcp_admission(settings)
    register_community_mcp_host()
    database_runtime = configure_community_database(
        settings.database_url,
        echo=settings.debug,
    )
    from okto_pulse.community.adapters.relational_application import (
        CommunityRelationalApplicationAdapter,
    )
    from okto_pulse.core.ports.relational_application import (
        register_relational_application_adapter,
    )

    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    from okto_pulse.community.adapters.relational_effects import (
        register_community_relational_effects,
    )

    register_community_relational_effects(
        settings=settings,
        api_base_url=f"http://127.0.0.1:{api_port}",
    )

    # R01C REPLAN-IMP4 (FR3/FR5): register the Community relational SCHEMA-LIFECYCLE
    # orchestrator on the core seam BEFORE the lifespan runs init_db, so the core
    # delegates the WHOLE migrate->create_all->seed lifecycle to the Community
    # migrator (R16-B) + bootstrapper (R16-C). The orchestrator runs against the live engine injected by the Community
    # SQLAlchemy adapter above.
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )

    register_community_relational_schema_lifecycle()

    from okto_pulse.community.adapters.data import CommunityOutboxEventBus
    from okto_pulse.community.adapters.content_ingestion import (
        CommunityContentIngestionResolver,
    )
    from okto_pulse.community.adapters.runtime_composition import (
        build_community_runtime_composition,
    )
    from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
        build_community_unit_of_work_factory,
    )

    _rc_session_factory = get_session_factory()
    from okto_pulse.community.adapters.sqlalchemy_quality_assessment import (
        CommunitySqlAlchemyQualityAssessmentPreflightReader,
    )
    from okto_pulse.core.ports.quality_assessment import (
        register_quality_assessment_preflight_reader,
    )

    register_quality_assessment_preflight_reader(
        CommunitySqlAlchemyQualityAssessmentPreflightReader(_rc_session_factory)
    )
    from okto_pulse.community.adapters.sqlalchemy_validation_cycle import (
        CommunitySqlAlchemyValidationCycleReader,
    )
    from okto_pulse.core.ports.validation_cycle import (
        register_validation_cycle_reader,
    )

    register_validation_cycle_reader(
        CommunitySqlAlchemyValidationCycleReader(_rc_session_factory)
    )

    def _cancel_safe_rc_scope():
        return database_runtime.cancel_safe_session_scope(
            _rc_session_factory,
            owner="mcp.auth",
        )

    # MKG-A-S1 (FR4): durable append-only source for cognitive nodes.
    # Registered BEFORE the lifespan so the consolidation commit path can
    # resolve it fail-closed from its first cognitive commit.
    from okto_pulse.community.adapters.sqlalchemy_kg_cognitive_source import (
        CommunitySqlAlchemyCognitiveSourceStore,
    )
    from okto_pulse.core.ports.kg_cognitive_source import (
        register_cognitive_source_store,
    )

    register_cognitive_source_store(
        CommunitySqlAlchemyCognitiveSourceStore(_rc_session_factory)
    )

    # MKG-C-S1 (FR1): off-graph equivalence ledger for reversible curation.
    from okto_pulse.community.adapters.sqlalchemy_kg_equivalence_ledger import (
        CommunitySqlAlchemyEquivalenceLedger,
    )
    from okto_pulse.core.ports.kg_equivalence_ledger import (
        register_equivalence_ledger,
    )

    register_equivalence_ledger(
        CommunitySqlAlchemyEquivalenceLedger(_rc_session_factory)
    )
    _community_uow_factory = build_community_unit_of_work_factory(_rc_session_factory)
    _community_worker_registry = build_community_worker_registry(
        _rc_session_factory,
        kg_cleanup_enabled=getattr(settings, "kg_cleanup_enabled", True),
        kg_cleanup_interval_seconds=getattr(
            settings,
            "kg_cleanup_interval_seconds",
            60,
        ),
        kg_queue_recovery_scan_interval_seconds=getattr(
            settings,
            "kg_queue_recovery_scan_interval_s",
            60,
        ),
        kg_queue_max_concurrent_workers=getattr(
            settings,
            "kg_queue_max_concurrent_workers",
            1,
        ),
    )
    runtime_composition = build_community_runtime_composition(
        settings=settings,
        auth_provider=auth,
        storage_provider=storage,
        event_bus=CommunityOutboxEventBus(_rc_session_factory),
        scheduler_control=scheduler_control,
        uow_factory=_community_uow_factory,
        worker_registry=_community_worker_registry,
        content_ingestion_resolver=CommunityContentIngestionResolver(),
    )

    # SQLite PRAGMAs are installed by the Community engine adapter. There is no
    # second partial listener in this module and no core fallback listener.

    # Bootstrap the KG provider registry via the Community composition root.
    # (R05-D/R-P2-02) The Community composition registers event_bus, audit_repo
    # and config EXPLICITLY (Community data adapters). The core registry no
    # longer owns a relational session_factory auto-wire and fails closed if one
    # of these slots is missing.
    #
    # R06/R08-B: the Community composition root injects the AuthContext factory
    # bound to the MCP server's current agent/db providers, so the KG query tools
    # resolve agent_id + accessible boards via the AuthContext port. Board ACL
    # still flows through the public application_agents facade inside the
    # Community adapter; api_key path untouched. The core server module is
    # imported lazily (per first call) to avoid pulling the heavy MCP module at
    # boot.
    async def _mcp_auth_get_agent():
        from okto_pulse.core.mcp import get_authenticated_agent_for_mcp

        return await get_authenticated_agent_for_mcp()

    from okto_pulse.community.adapters.mcp_auth import (
        create_mcp_auth_factory,
    )

    from okto_pulse.core.composition import runtime_composition_scope

    with runtime_composition_scope(runtime_composition):
        from okto_pulse.core.runtime_registry import (
            register_content_ingestion_resolver,
            register_unit_of_work_factory,
        )

        register_unit_of_work_factory(_community_uow_factory)
        register_content_ingestion_resolver(
            runtime_composition.content_ingestion_resolver
        )

        configure_community_kg_registry(
            _rc_session_factory,
            settings=settings,
            auth_context_factory=create_mcp_auth_factory(
                _mcp_auth_get_agent,
                _rc_session_factory,
                session_scope_factory=_cancel_safe_rc_scope,
            ),
        )

        from okto_pulse.core.mcp import (
            register_mcp_authenticator,
            register_scheduler_control_for_mcp,
        )
        from okto_pulse.community.adapters.mcp_auth import (
            make_community_mcp_authenticator,
        )

        register_mcp_authenticator(
            make_community_mcp_authenticator(
                session_factory=_rc_session_factory,
                session_scope_factory=_cancel_safe_rc_scope,
            )
        )
        register_scheduler_control_for_mcp(scheduler_control)

        from okto_pulse.community.adapters.resources import (
            register_and_freeze_community_resource_catalog,
        )

        mcp_cold_start_transaction = register_and_freeze_community_resource_catalog(
            runtime_composition.runtime_values
        )

    try:
        app = create_app(
            settings=settings,
            auth_provider=auth,
            storage_provider=storage,
            cors_origins=settings.cors_origins_list,
            lifespan=combined_lifespan,
            composition=runtime_composition,
            strict_runtime=True,
            edition_routers=(metrics_router,),
        )
        app.state.mcp_cold_start_transaction = mcp_cold_start_transaction

        # System flags endpoint — used by the frontend to honor CLI/env terms
        # pre-acceptance.
        from okto_pulse.community.acceptance import acceptance_status

        @app.get("/api/v1/me/system-flags")
        def get_system_flags():
            """Surface env/CLI-driven flags the SPA needs at boot time."""
            return {"terms_acceptance": acceptance_status()}

        # Mount frontend after API routes so /api/v1/* takes precedence.
        _mount_frontend(
            app,
            FRONTEND_DIR,
            api_port=api_port,
            mcp_port=mcp_port,
            public_host=public_host,
            explicit_public_host=public_host_env is not None,
            public_api_port=public_api_port,
            public_mcp_port=public_mcp_port,
        )
    except BaseException:
        mcp_cold_start_transaction.rollback()
        raise

    return app


def _build_module_app():
    """Construct the module ASGI boundary under the cold-start rollback."""

    raw_app = create_community_app()
    try:
        return AccessLogQueryRedactionMiddleware(raw_app)
    except BaseException:
        raw_app.state.mcp_cold_start_transaction.rollback()
        raise


# String annotations: tests monkeypatch AccessLogQueryRedactionMiddleware with
# a plain function and re-import this module; a runtime-evaluated union
# (`X | None`) would then raise TypeError at import.
_MODULE_APP: "AccessLogQueryRedactionMiddleware | None" = None


def get_module_app() -> "AccessLogQueryRedactionMiddleware":
    """Build (ONCE) and return the module ASGI boundary.

    Memoized because ``create_community_app()`` is NOT idempotent: it registers
    process runtime values (configure_community_database,
    register_relational_application_adapter, the KG/scheduler seams) and
    freezes the MCP catalog. Two calls would mean two engines and a second
    freeze attempt.

    Lazy (instead of the old module-level ``app = _build_module_app()``)
    because importing ``okto_pulse.community.main`` used to run the whole
    composition root at pytest collection time — creating an AsyncEngine with
    no running loop and publishing the relational adapter into a ContextVar
    binding that later tests replace. See the comment in
    tests/test_af09_mcp_trace_sink_adapter.py.
    """

    global _MODULE_APP
    if _MODULE_APP is None:
        _MODULE_APP = _build_module_app()
    return _MODULE_APP


def __getattr__(name: str):  # PEP 562 — covers `from ... import app` and `mod.app`
    if name == "app":
        return get_module_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def _wait_for_server_started(
    server_name: str,
    server: uvicorn.Server,
    task: asyncio.Task[None],
    timeout_seconds: float | None = None,
) -> None:
    if timeout_seconds is None:
        timeout_seconds = _startup_timeout_seconds()
    deadline = time.monotonic() + timeout_seconds
    while not server.started:
        if task.done():
            await task
            raise RuntimeError(
                f"{server_name} server stopped before startup completed."
            )
        if time.monotonic() >= deadline:
            server.should_exit = True
            raise TimeoutError(
                f"{server_name} server startup timed out after {timeout_seconds:.0f}s. "
                "If this machine is doing a slow cold start, set "
                "OKTO_PULSE_STARTUP_TIMEOUT_SECONDS to a larger value."
            )
        await asyncio.sleep(0.05)


def _log_ready_servers(api_port: int, mcp_port: int) -> None:
    public_host = os.environ.get("PUBLIC_HOST", "127.0.0.1")
    public_api_port = int(os.environ.get("PUBLIC_API_PORT") or api_port)
    public_mcp_port = int(os.environ.get("PUBLIC_MCP_PORT") or mcp_port)

    _STARTUP_LOGGER.info(
        "API Server initialized successfully - http://%s:%s/api/v1",
        public_host,
        public_api_port,
    )
    _STARTUP_LOGGER.info(
        "UI Server initialized successfully - http://%s:%s",
        public_host,
        public_api_port,
    )
    _STARTUP_LOGGER.info(
        "MCP Server initialized successfully - http://%s:%s/mcp",
        public_host,
        public_mcp_port,
    )
    _STARTUP_LOGGER.info("Startup complete - The application is ready")


async def _shutdown_server_pair(
    api_server: uvicorn.Server,
    mcp_server: uvicorn.Server,
    api_task: asyncio.Task[None],
    mcp_task: asyncio.Task[None],
    *,
    timeout_seconds: float | None = None,
) -> None:
    """Stop both uvicorn servers without letting open streams hang forever."""
    timeout = (
        _shutdown_timeout_seconds() if timeout_seconds is None else timeout_seconds
    )
    set_shutdown_log_suppression(True)
    try:
        api_server.should_exit = True
        mcp_server.should_exit = True

        _, pending = await asyncio.wait({api_task, mcp_task}, timeout=timeout + 1.0)
        if not pending:
            return

        _STARTUP_LOGGER.warning(
            "Shutdown timeout exceeded after %.0fs; forcing API/UI and MCP servers to stop.",
            timeout,
        )

        api_server.force_exit = True
        mcp_server.force_exit = True
        api_server.should_exit = True
        mcp_server.should_exit = True
        for task in (api_task, mcp_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(api_task, mcp_task, return_exceptions=True)
    finally:
        set_shutdown_log_suppression(False)


async def _shutdown_launched_server_tasks(
    api_server: uvicorn.Server,
    mcp_server: uvicorn.Server,
    api_task: asyncio.Task[None] | None,
    mcp_task: asyncio.Task[None] | None,
    *,
    timeout_seconds: float,
) -> None:
    """Drain a partially launched listener pair without orphaning one task."""

    if api_task is not None and mcp_task is not None:
        await _shutdown_server_pair(
            api_server,
            mcp_server,
            api_task,
            mcp_task,
            timeout_seconds=timeout_seconds,
        )
        return

    api_server.should_exit = True
    mcp_server.should_exit = True
    tasks = tuple(task for task in (api_task, mcp_task) if task is not None)
    if not tasks:
        return
    set_shutdown_log_suppression(True)
    try:
        # An incomplete pair has never crossed the listener publication
        # boundary. Cancel synchronously before the event loop can run the one
        # scheduled coroutine and expose a single listener.
        api_server.force_exit = True
        mcp_server.force_exit = True
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        set_shutdown_log_suppression(False)


def _install_shutdown_signal_handlers(
    request_shutdown: Callable[[int], None],
) -> list[Callable[[], None]]:
    """KGD-01 (FR2): shutdown ORDENADO também para SIGTERM/SIGBREAK.

    A captura de sinais do uvicorn está desabilitada nos dois servidores
    (dois ``Server`` no mesmo loop — o último sobrescreveria o handler); o
    SIGINT chega pelo KeyboardInterrupt do ``asyncio.Runner``. Sem este
    instalador, SIGTERM (POSIX) e CTRL_BREAK_EVENT (Windows) terminavam o
    processo SEM teardown — nenhum checkpoint/close dos grafos.

    POSIX: ``loop.add_signal_handler(SIGTERM, …)`` dispara o MESMO caminho
    ordenado do Ctrl+C. Windows: ``add_signal_handler`` não é suportado —
    usa ``signal.signal(SIGBREAK, …)`` + ``call_soon_threadsafe`` (que
    acorda o selector bloqueado). Limitações Windows (por design do SO):
    ``os.kill(pid, SIGTERM)`` vira TerminateProcess e NUNCA é interceptável;
    o console-close (CTRL_CLOSE) dá ~5s de budget antes do TerminateProcess
    do console host — o teardown de grafos precisa ser best-effort nesse
    canal. Retorna callbacks de cleanup que restauram a disposição anterior.
    """
    loop = asyncio.get_running_loop()
    cleanups: list[Callable[[], None]] = []
    candidates: list[signal.Signals] = []
    if hasattr(signal, "SIGTERM"):
        candidates.append(signal.SIGTERM)
    if hasattr(signal, "SIGBREAK"):  # Windows: entregue pelo CTRL_BREAK_EVENT
        candidates.append(signal.SIGBREAK)

    for sig in candidates:
        try:
            loop.add_signal_handler(sig, request_shutdown, int(sig))
        except (NotImplementedError, RuntimeError, ValueError, OSError):
            pass  # Windows / loop sem suporte — tenta o handler síncrono
        else:
            cleanups.append(lambda s=sig, lp=loop: lp.remove_signal_handler(s))
            continue
        try:

            def _sync_handler(
                signum: int,
                _frame: object,
                _loop: asyncio.AbstractEventLoop = loop,
                _cb: Callable[[int], None] = request_shutdown,
            ) -> None:
                # Roda na main thread entre bytecodes; call_soon_threadsafe
                # acorda o loop mesmo bloqueado no selector.
                _loop.call_soon_threadsafe(_cb, signum)

            previous = signal.signal(sig, _sync_handler)
        except (ValueError, OSError, RuntimeError) as exc:
            # ex.: fora da main thread — resta o SIGINT do Runner.
            _STARTUP_LOGGER.debug(
                "shutdown signal handler not installed for %s: %s", sig, exc
            )
        else:
            cleanups.append(lambda s=sig, p=previous: signal.signal(s, p))
    return cleanups


async def _serve_dual(api_port: int, mcp_port: int) -> None:
    """Run API+UI on `api_port` and MCP on `mcp_port` inside a single
    Python process via two uvicorn `Server` instances driven by
    ``asyncio.gather``.

    Single-process is required to keep the Kùzu lock owned by exactly one
    process (the embedded DB does not support multiple writers). The two
    listeners share the same composed runtime state — including the Global
    Discovery runtime handle and the MCP runtime ports registered by the API
    lifespan, and the request-scoped MCP credential provider — so the MCP sub-app
    sees a fully-initialised runtime.
    """
    # Build here, already inside the server loop (PEP 562 lazy module app);
    # _build_module_app/create_community_app roll back on BaseException.
    module_app = get_module_app()
    composition = module_app.state.runtime_composition
    transaction = module_app.state.mcp_cold_start_transaction

    try:
        from okto_pulse.community.adapters.mcp_host import (
            build_community_mcp_asgi_app,
        )
        from okto_pulse.community.adapters.resources import (
            CommunityMcpColdStartTransaction,
        )
        from okto_pulse.community.adapters.mcp_trace import (
            build_mcp_trace_sink_from_env,
        )
        from okto_pulse.core.composition import runtime_composition_scope
        from okto_pulse.core.mcp import mcp

        if not isinstance(transaction, CommunityMcpColdStartTransaction):
            raise RuntimeError("community_mcp_cold_start_transaction_missing")

        settings = CommunitySettings()
        uvicorn_log_config = build_uvicorn_log_config()
        shutdown_timeout = _shutdown_timeout_seconds()
        uvicorn_shutdown_timeout = int(math.ceil(shutdown_timeout))

        with runtime_composition_scope(composition):
            frozen_resources, projection_identity = (
                transaction.require_frozen_projection()
            )
            mcp_asgi_app = build_community_mcp_asgi_app(
                catalog=mcp,
                resource_catalog=frozen_resources,
                projection_identity=projection_identity,
                trace_sink=build_mcp_trace_sink_from_env(),
                composition=composition,
            )

        api_config = uvicorn.Config(
            module_app,
            host=settings.host,
            port=api_port,
            ws="wsproto",
            log_level="info",
            log_config=uvicorn_log_config,
            timeout_keep_alive=1,
            timeout_graceful_shutdown=uvicorn_shutdown_timeout,
        )
        api_server = uvicorn.Server(api_config)
        # Disable uvicorn's per-server signal capture — with two Servers in the
        # same loop the last one wins the process signal handler, which can leave
        # the other listener waiting forever. asyncio.Runner cancels _serve_dual on
        # Ctrl+C; we coordinate both listeners in the except block below.
        api_server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]

        mcp_config = uvicorn.Config(
            AccessLogQueryRedactionMiddleware(mcp_asgi_app),
            # Read host from environment (set by Docker / compose) or fall back to
            # loopback so a stray process doesn't accidentally expose the MCP
            # server. Override via MCP_HOST=0.0.0.0 in docker-compose.yml when
            # port-mapping is required from outside the container.
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=mcp_port,
            ws="wsproto",
            log_level="info",
            log_config=uvicorn_log_config,
            timeout_keep_alive=1,
            timeout_graceful_shutdown=uvicorn_shutdown_timeout,
        )
        mcp_server = uvicorn.Server(mcp_config)
        mcp_server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]
    except BaseException:
        rollback = getattr(transaction, "rollback", None)
        if callable(rollback):
            rollback()
        raise

    def _request_ordered_shutdown(signum: int) -> None:
        # KGD-01 (FR2): mesmo caminho ordenado do Ctrl+C — pede o drain dos
        # dois listeners; o wait(FIRST_COMPLETED) abaixo acorda e o teardown
        # do lifespan (checkpoint+close dos grafos) roda antes do exit.
        _STARTUP_LOGGER.info(
            "shutdown signal received (signum=%s) — starting ordered shutdown",
            signum,
        )
        api_server.should_exit = True
        mcp_server.should_exit = True

    api_task: asyncio.Task[None] | None = None
    mcp_task: asyncio.Task[None] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    signal_cleanups: tuple[Callable[[], None], ...] = ()
    try:
        signal_cleanups = tuple(
            _install_shutdown_signal_handlers(_request_ordered_shutdown)
        )

        heartbeat_coroutine = _lock_heartbeat_loop()
        try:
            heartbeat_task = asyncio.create_task(
                heartbeat_coroutine,
                name="serve_lock_heartbeat",
            )
        except BaseException:
            heartbeat_coroutine.close()
            raise

        api_coroutine = api_server.serve()
        try:
            api_task = asyncio.create_task(api_coroutine, name="api_ui_server")
        except BaseException:
            api_coroutine.close()
            raise

        mcp_coroutine = mcp_server.serve()
        try:
            mcp_task = asyncio.create_task(mcp_coroutine, name="mcp_server")
        except BaseException:
            mcp_coroutine.close()
            raise

        await _wait_for_server_started("API/UI", api_server, api_task)
        await _wait_for_server_started("MCP", mcp_server, mcp_task)
        _log_ready_servers(api_port, mcp_port)
        transaction.commit()
        done, _ = await asyncio.wait(
            {api_task, mcp_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
        await _shutdown_server_pair(
            api_server,
            mcp_server,
            api_task,
            mcp_task,
            timeout_seconds=shutdown_timeout,
        )
    except asyncio.CancelledError:
        # Ctrl+C reached the loop before our handler could flip should_exit.
        # Ask both servers to drain and swallow the cancel — this is the
        # expected shutdown path, not an error.
        try:
            await _shutdown_launched_server_tasks(
                api_server,
                mcp_server,
                api_task,
                mcp_task,
                timeout_seconds=shutdown_timeout,
            )
        finally:
            transaction.rollback()
    except BaseException:
        try:
            await _shutdown_launched_server_tasks(
                api_server,
                mcp_server,
                api_task,
                mcp_task,
                timeout_seconds=shutdown_timeout,
            )
        finally:
            transaction.rollback()
        raise
    finally:
        for _cleanup in signal_cleanups:
            with contextlib.suppress(Exception):
                _cleanup()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                # Heartbeat failures are already logged inside the loop; we
                # never let them block shutdown.
                pass


async def _lock_heartbeat_loop() -> None:
    """Refresh the serve lock file's `heartbeat_at` while the server runs.

    The interval is read from `serve_lock.HEARTBEAT_INTERVAL_SECONDS`. If
    the lock owner can't be found (no active lock for this process) the
    loop is a no-op — it stays alive in case `acquire_serve_lock` runs
    later, but does nothing. Consecutive write failures are logged as
    warnings; the heartbeat keeps trying. A peer that sees the heartbeat
    go stale (older than `HEARTBEAT_TTL_SECONDS`) will take the lock over
    on its next startup attempt.
    """
    from okto_pulse.community import serve_lock as _serve_lock

    interval = max(1, int(_serve_lock.HEARTBEAT_INTERVAL_SECONDS))
    consecutive_failures = 0
    try:
        while True:
            await asyncio.sleep(interval)
            owner = _serve_lock.get_active_lock()
            if owner is None:
                continue
            try:
                owner.refresh_heartbeat()
                if consecutive_failures:
                    _LOCK_LOGGER.info(
                        "serve lock heartbeat recovered after %d failures",
                        consecutive_failures,
                    )
                consecutive_failures = 0
            except OSError as exc:
                consecutive_failures += 1
                if consecutive_failures in (1, 3, 10):
                    _LOCK_LOGGER.warning(
                        "serve lock heartbeat refresh failed (%d in a row): %s",
                        consecutive_failures,
                        exc,
                    )
    except asyncio.CancelledError:
        raise


def run() -> int:
    """Run the community API + Frontend + MCP server (single process,
    two ports). Reads ``OKTO_PULSE_PORT`` / ``OKTO_PULSE_MCP_PORT`` env
    vars (set by the CLI) and falls back to the settings defaults.
    """
    from okto_pulse.community.serve_lock import (
        ServeAlreadyRunningError,
        acquire_serve_lock,
    )
    from okto_pulse.community.data_home import (
        UninitializedDefaultDataHomeError,
        assert_serve_data_home_ready,
    )

    settings = CommunitySettings()
    try:
        assert_serve_data_home_ready(settings)
    except UninitializedDefaultDataHomeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _enable_native_crash_diagnostics()
    api_port = int(
        os.environ.get("PORT", os.environ.get("OKTO_PULSE_PORT", str(settings.port)))
    )
    mcp_port = int(
        os.environ.get(
            "MCP_PORT", os.environ.get("OKTO_PULSE_MCP_PORT", str(settings.mcp_port))
        )
    )
    try:
        serve_lock = acquire_serve_lock(settings)
    except ServeAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    with serve_lock:
        try:
            run_async_server(_serve_dual(api_port, mcp_port))
        except KeyboardInterrupt:
            # Ctrl+C / SIGINT — shutdown is graceful from here; suppress the
            # default Python traceback for a clean CLI exit.
            print("\nOkto Pulse stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
