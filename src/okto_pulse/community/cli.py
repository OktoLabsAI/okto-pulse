"""Okto Pulse Community CLI — setup and run the local-first edition."""

# ruff: noqa: E402

import warnings

warnings.filterwarnings(
    "ignore",
    message=r"urllib3.*or chardet.*doesn't match a supported version",
    category=Warning,
)

import argparse
import asyncio
import json
import logging
import os
import socket
import stat
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal
from uuid import uuid4

from okto_pulse.community.metrics_limits import DEFAULT_WINDOW_DAYS, validate_window_days

# Default ports
DEFAULT_API_PORT = 8100
DEFAULT_MCP_PORT = 8101

_BANNER_PATH = Path(__file__).parent / "banner.txt"
_METRICS_CLI_LOGGER = logging.getLogger("okto_pulse.community.metrics.cli")
_BOOTSTRAP_KEY_HEX_LENGTH = 48
_BOOTSTRAP_HANDOFF_MAX_BYTES = 128

_CredentialSource = Literal["governed_legacy_plaintext", "reveal_once"]


@dataclass(frozen=True)
class _ExportableAgentCredential:
    name: str
    plaintext: str
    source: _CredentialSource


def _is_bootstrap_credential(value: str) -> bool:
    if not value.startswith("dash_"):
        return False
    suffix = value.removeprefix("dash_")
    return len(suffix) == _BOOTSTRAP_KEY_HEX_LENGTH and all(
        character in "0123456789abcdef" for character in suffix
    )


def _validated_bootstrap_handoff_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("bootstrap credential handoff path must be absolute")
    parent = path.parent
    if not parent.is_dir():
        raise ValueError(
            f"bootstrap credential handoff directory does not exist: {parent}"
        )
    if parent.is_symlink():
        raise ValueError(
            f"bootstrap credential handoff directory must not be a symlink: {parent}"
        )
    return path


class _BootstrapKeyHandoffReservation:
    """Hidden sibling reserved before initialization mutates persistent state.

    POSIX creates the pending file with mode ``0600``. On Windows, the caller
    must provide a directory protected by a private ACL; chmod-style mode bits
    do not provide the same confidentiality guarantee.
    """

    def __init__(self, path: Path, pending_path: Path, descriptor: int) -> None:
        self.path = path
        self.pending_path = pending_path
        self._descriptor: int | None = descriptor
        self._payload_ready = False
        self._published = False

    @property
    def published(self) -> bool:
        return self._published

    def publish(self, credential: str) -> Path:
        if not _is_bootstrap_credential(credential):
            raise ValueError("refusing to hand off an invalid bootstrap credential")
        if self._descriptor is None:
            raise RuntimeError("bootstrap credential handoff reservation is closed")

        payload = f"{credential}\n".encode("ascii")
        view = memoryview(payload)
        try:
            while view:
                written = os.write(self._descriptor, view)
                if written <= 0:
                    raise OSError("bootstrap credential handoff write made no progress")
                view = view[written:]
            os.fsync(self._descriptor)
            os.close(self._descriptor)
            self._descriptor = None
            self._payload_ready = True

            # Publish only a complete, fsynced payload. POSIX hard-link
            # creation is atomic and refuses an existing destination. Windows
            # os.rename is same-volume atomic and refuses an existing target.
            if os.name == "nt":
                os.rename(self.pending_path, self.path)
                self._published = True
            else:
                os.link(
                    self.pending_path,
                    self.path,
                    follow_symlinks=False,
                )
                self._published = True
                self.pending_path.unlink()
        except FileExistsError as exc:
            raise RuntimeError(
                "bootstrap credential handoff destination appeared during "
                f"initialization; complete pending handoff retained at "
                f"{self.pending_path}"
            ) from exc
        except BaseException:
            self.discard()
            raise
        return self.path

    def discard(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        if self._published or not self._payload_ready:
            self.pending_path.unlink(missing_ok=True)


def _reserve_bootstrap_key_handoff(
    destination: str | Path,
) -> _BootstrapKeyHandoffReservation:
    path = _validated_bootstrap_handoff_path(destination)
    if os.path.lexists(path):
        raise FileExistsError(
            f"bootstrap credential handoff destination already exists: {path}"
        )

    pending_path = path.with_name(f".{path.name}.pending-{os.getpid()}-{uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(pending_path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(
                "bootstrap credential handoff destination is not a regular file"
            )
    except BaseException:
        os.close(descriptor)
        pending_path.unlink(missing_ok=True)
        raise
    return _BootstrapKeyHandoffReservation(path, pending_path, descriptor)


def _write_bootstrap_key_handoff(
    destination: str | Path,
    credential: str,
) -> Path:
    """Write a freshly generated credential to an exclusive private file.

    This is an opt-in automation bridge for the exact ``init`` invocation that
    receives the reveal-once value from ``seed_community_defaults``. It never
    reads the database and refuses to overwrite an existing handoff. POSIX
    handoffs use mode ``0600``; on Windows the destination directory must have
    a private ACL because chmod-style mode bits are not a privacy boundary.
    """
    reservation = _reserve_bootstrap_key_handoff(destination)
    try:
        return reservation.publish(credential)
    except BaseException:
        reservation.discard()
        raise


def _consume_bootstrap_key_handoff(source: str | Path) -> str:
    """Atomically claim, validate and remove one reveal-once handoff."""
    path = _validated_bootstrap_handoff_path(source)
    claimed = path.with_name(f".{path.name}.claimed-{os.getpid()}-{uuid4().hex}")
    try:
        os.rename(path, claimed)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "bootstrap credential handoff is missing or was already consumed"
        ) from exc

    descriptor: int | None = None
    try:
        try:
            claimed_metadata = os.lstat(claimed)
        except (FileNotFoundError, PermissionError) as exc:
            # Windows may report a successful racing rename to more than one
            # caller while only one claim remains addressable. The vanished
            # claim surfaces as FileNotFoundError, or — while the winner's
            # unlink leaves it delete-pending — as PermissionError. Either
            # way this is the losing consumer; it must never fall back to
            # the DB. A genuine POSIX permission problem stays fatal.
            if isinstance(exc, PermissionError) and os.name != "nt":
                raise
            raise RuntimeError(
                "bootstrap credential handoff is missing or was already consumed"
            ) from exc
        if not stat.S_ISREG(claimed_metadata.st_mode):
            raise RuntimeError("bootstrap credential handoff is not a regular file")
        if os.name != "nt" and claimed_metadata.st_mode & 0o077:
            raise RuntimeError(
                "bootstrap credential handoff permissions are broader than 0600"
            )

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(claimed, flags)
        except (FileNotFoundError, PermissionError) as exc:
            # Same Windows racing-rename anomaly handled at the lstat above:
            # the losing consumer's claim can also vanish (FileNotFoundError)
            # or turn delete-pending (PermissionError) between lstat and
            # open. It must never fall back to the DB. A genuine POSIX
            # permission problem stays fatal.
            if isinstance(exc, PermissionError) and os.name != "nt":
                raise
            raise RuntimeError(
                "bootstrap credential handoff is missing or was already consumed"
            ) from exc
        opened_metadata = os.fstat(descriptor)
        if (
            opened_metadata.st_dev != claimed_metadata.st_dev
            or opened_metadata.st_ino != claimed_metadata.st_ino
        ):
            raise RuntimeError("bootstrap credential handoff changed while opening")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            payload = stream.read(_BOOTSTRAP_HANDOFF_MAX_BYTES + 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        claimed.unlink(missing_ok=True)

    if len(payload) > _BOOTSTRAP_HANDOFF_MAX_BYTES:
        raise RuntimeError("bootstrap credential handoff is unexpectedly large")
    try:
        credential = payload.decode("ascii").rstrip("\r\n")
    except UnicodeDecodeError as exc:
        raise RuntimeError("bootstrap credential handoff is not ASCII") from exc
    if not _is_bootstrap_credential(credential):
        raise RuntimeError("bootstrap credential handoff is invalid")
    return credential


def _is_recoverable_agent_key(value: str | None) -> bool:
    return _stored_agent_credential_source(value) == "governed_legacy_plaintext"


def _stored_agent_credential_source(value: str | None) -> _CredentialSource | None:
    if value and value.startswith("dash_"):
        return "governed_legacy_plaintext"
    return None


def _exportable_credential_from_legacy_agent(
    agent,
) -> _ExportableAgentCredential | None:
    plaintext = _field(agent, "api_key")
    if _stored_agent_credential_source(plaintext) != "governed_legacy_plaintext":
        return None
    return _ExportableAgentCredential(
        name=_field(agent, "name"),
        plaintext=plaintext,
        source="governed_legacy_plaintext",
    )


def _exportable_credential_from_reveal_once(
    name: str, plaintext: str
) -> _ExportableAgentCredential | None:
    if not plaintext.startswith("dash_"):
        return None
    return _ExportableAgentCredential(
        name=name, plaintext=plaintext, source="reveal_once"
    )


def _field(record, name: str, default=None):
    if isinstance(record, dict):
        return record.get(name, default)
    mapping = getattr(record, "_mapping", None)
    if mapping is not None and name in mapping:
        return mapping[name]
    return getattr(record, name, default)


def _result_records(result):
    mappings = getattr(result, "mappings", None)
    if callable(mappings):
        return list(mappings().all())
    scalars = getattr(result, "scalars", None)
    if callable(scalars):
        return list(scalars().all())
    return list(result.all())


def _package_version(package_name: str) -> str:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "unknown"


def _format_version() -> str:
    return (
        f"okto-pulse {_package_version('okto-pulse')} "
        f"(okto-pulse-core {_package_version('okto-pulse-core')})"
    )


def _print_banner() -> None:
    """Print the Okto Pulse ASCII banner to stderr (kept off stdout to
    avoid corrupting JSON pipes). Suppressed when ``OKTO_PULSE_NO_BANNER``
    is set or the banner file is missing."""
    if os.environ.get("OKTO_PULSE_NO_BANNER"):
        return
    try:
        sys.stderr.write(_BANNER_PATH.read_text(encoding="utf-8"))
        sys.stderr.write("\n")
        sys.stderr.write(
            f"Version {_package_version('okto-pulse')} "
            f"({_package_version('okto-pulse-core')})\n\n"
        )
        sys.stderr.flush()
    except OSError:
        pass


def _is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


def _configure_community_relational_runtime(settings, *, echo: bool = False) -> None:
    from okto_pulse.community.adapters.sqlalchemy_database import (
        configure_community_database,
    )

    configure_community_database(settings.database_url, echo=echo)
    from okto_pulse.community.adapters.relational_effects import (
        register_community_relational_effects,
    )

    # CLI commands run outside the FastAPI composition root.  Register the
    # same relational ports required by seeds, health reads and governed
    # writes so `init` and offline maintenance fail closed only for genuine
    # missing infrastructure, not because the CLI omitted edition wiring.
    register_community_relational_effects(
        settings=settings,
        api_base_url=f"http://127.0.0.1:{settings.port}",
    )


def _fail_fast_if_server_running(operation: str) -> None:
    """Reject a live server before composing an offline operation.

    This is a point-in-time admission check, not a lock held for the caller's
    entire operation. Long-lived exclusion requires an owned runtime fence.
    """
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.community.serve_lock import (
        ServeAlreadyRunningError,
        assert_no_live_server,
    )

    try:
        assert_no_live_server(CommunitySettings().data_dir, operation=operation)
    except ServeAlreadyRunningError as exc:
        print(
            f"ERROR [serve-lock]: refusing '{operation}' while an okto-pulse "
            f"server is running.\n{exc}",
            file=sys.stderr,
        )
        sys.exit(2)


class GlobalDiscoveryInitError(RuntimeError):
    """Typed ``okto-pulse init`` Global Discovery failure/refusal.

    Carries a stable ``.code`` so callers/tests key off the exact typed contract
    rather than free-form ``RuntimeError`` text (blocker 10).
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class BoardGraphInitError(RuntimeError):
    """Typed ``okto-pulse init`` board Knowledge Graph failure/refusal.

    Same contract as :class:`GlobalDiscoveryInitError`: a stable ``.code`` so
    callers and tests key off the typed outcome instead of message text.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


async def _bootstrap_board_graph(board_id: str) -> tuple[str, str]:
    """Bootstrap a board's Knowledge Graph and prove it is really there.

    Returns the ``(storage token, schema version)`` pair that ``init`` reports.

    Fail-closed, and deliberately backend-neutral.  ``init`` used to name the
    board's graph by resolving a Community-local file path, which quietly
    assumed one storage engine: where a board uses a different one that path
    describes nothing, so the operator-facing line could name a file that does
    not exist while init still claimed success.  The board's own runtime is
    asked instead, through the registered port, and it answers the same way
    whichever engine is behind it.

    The diagnosis is non-opening -- the runtime reports from metadata alone, so
    proving the graph exists costs no handle and cannot itself fail the boot it
    is checking.
    """

    from okto_pulse.core.kg.interfaces.graph_runtime_store import (
        GraphRuntimeObservationState,
    )
    from okto_pulse.core.services.application_kg import (
        get_current_provider_registry,
    )

    from okto_pulse.community.adapters.composition import (
        require_community_routed_graph_composition,
    )

    registry = get_current_provider_registry()
    require_community_routed_graph_composition(registry).initialize_board_route(
        board_id
    )
    await registry.graph_schema_manager.ensure_bootstrapped(board_id)

    # ``getattr`` rather than attribute access: a registry that does not carry
    # the slot at all must reach the typed refusal below, not raise an
    # AttributeError that reads like a crash instead of a fail-closed decision.
    runtime = getattr(registry, "graph_runtime_store", None)
    if runtime is None:
        raise BoardGraphInitError(
            "board_graph_provider_unavailable: no graph runtime store is "
            "registered, so init cannot prove the board graph exists after "
            "bootstrap",
            code="board_graph_provider_unavailable",
        )

    observation = runtime.graph_state(board_id)
    # ``normalized_state`` is the fail-closed reading: a legacy adapter that
    # only reports a negative ``exists`` is treated as unavailable rather than
    # promoted to a confirmed absence.
    observed = observation.normalized_state
    if observed is not GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE:
        reason = f" reason={observation.reason_code}" if observation.reason_code else ""
        raise BoardGraphInitError(
            "board_graph_init_refused: bootstrap did not leave a readable board "
            f"graph (state={observed.value}{reason}); refusing to report a "
            "successful init",
            code="board_graph_init_refused",
        )

    version = await registry.graph_schema_manager.current_version(board_id)
    # The shared ``board:<id>`` storage reference identifies the graph without
    # naming a backend, a file or a path.
    return observation.storage_ref.token, str(version)


def _bootstrap_global_discovery_graph() -> str:
    """Materialize the Global Discovery graph during ``okto-pulse init``.

    Fail-closed. Acquire the public Global Discovery writer lease and enter its
    guard, which installs both the exact active lease and the Core safe-write
    context that ``runtime.require_write_token()`` demands (the private
    ``community_global_discovery_writer_fence`` does not activate the Core
    context barrier and is deliberately not used here). Inside the guard,
    inspect the runtime state once and act on the closed four-state matrix:

    - ``CONFIRMED_ABSENT``: first materialization — ``bootstrap()`` exactly once.
    - ``PRESENT_READABLE_CANDIDATE``: typed success no-op
      (``global_discovery_already_present``) — ``bootstrap()`` zero times and the
      physical fingerprint/metadata is left identical. ``init`` never silently
      migrates existing Global Discovery; schema migration has its own owner.
    - ``PRESENT_UNREADABLE_OR_ERROR`` (including residue): typed refusal naming
      the recovery ceremony, zero mutation.
    - ``PROVIDER_UNAVAILABLE``: the exact missing-binding reason enters the
      composition-owned initializer under this lease; every other reason is a
      typed failure with zero mutation.

    On a mid-DDL failure the lease is released and handles are closed by the
    caller's shutdown barrier, and the partial graph is preserved (never
    auto-deleted) so the residue detector can quarantine it.

    Productive graph work runs synchronously in the current context.  A Core
    renewal guard carries the short durable lease through long native calls;
    only its heartbeat runs in a context-propagating helper thread. Returns the
    typed outcome code.
    """
    from okto_pulse.core.kg.interfaces.graph_runtime_store import (
        GraphRuntimeObservationState,
    )
    from okto_pulse.core.ports.global_discovery_recovery_control import (
        GlobalDiscoveryWriterLease,
    )
    from okto_pulse.core.services.application_kg import (
        get_current_provider_registry,
    )

    registry = get_current_provider_registry()
    runtime = registry.require_global_discovery_runtime()

    lease = GlobalDiscoveryWriterLease.acquire(
        operation="init_global_discovery",
        admin_lane=True,
    )
    guarded_error: BaseException | None = None
    outcome = ""
    try:
        with lease.renewing_guard():
            observation = runtime.state()
            obs_state = observation.state
            if obs_state == GraphRuntimeObservationState.CONFIRMED_ABSENT:
                runtime.bootstrap()
                outcome = "global_discovery_materialized"
            elif obs_state == GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE:
                # Typed success no-op: never re-bootstrap or migrate an existing
                # readable Global Discovery graph. Zero physical mutation.
                outcome = "global_discovery_already_present"
            elif obs_state == GraphRuntimeObservationState.PROVIDER_UNAVAILABLE:
                if observation.reason_code == "graph_route_binding_missing":
                    from okto_pulse.community.adapters.composition import (
                        require_community_routed_graph_composition,
                    )

                    # The routed initializer owns both durable binding publication
                    # and physical bootstrap.  Calling it only under the active
                    # writer lease avoids an unfenced first materialization; do
                    # not call runtime.bootstrap() again after it returns.
                    require_community_routed_graph_composition(
                        registry
                    ).initialize_global_route()
                    outcome = "global_discovery_materialized"
                else:
                    reason = (
                        f" reason={observation.reason_code}"
                        if observation.reason_code
                        else ""
                    )
                    raise GlobalDiscoveryInitError(
                        "global_discovery_provider_unavailable: Global Discovery "
                        f"runtime is unavailable{reason}; init made zero mutation",
                        code="global_discovery_provider_unavailable",
                    )
            else:
                # PRESENT_UNREADABLE_OR_ERROR, including
                # global_discovery_residue_without_primary and the durable
                # incomplete-bootstrap marker.
                from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (  # noqa: E501
                    BOOTSTRAP_INCOMPLETE_REASON,
                )

                reason = (
                    f" reason={observation.reason_code}"
                    if observation.reason_code
                    else ""
                )
                marker_details = observation.details or {}
                primary_confirmed_absent = (
                    marker_details.get("primary_confirmed_absent") is True
                )
                if (
                    observation.reason_code == BOOTSTRAP_INCOMPLETE_REASON
                    and primary_confirmed_absent
                ):
                    # Narrow exception (Nexus msg_20533dbbce3741248416fc0e53b7ea4e
                    # / msg_08ef262ec7b744c496e742bb6b42d45a): a marker whose
                    # primary is *physically* CONFIRMED_ABSENT (the previous
                    # process died before creating any graph artifact) may be
                    # retried.  ``state()`` stays authoritative — CLI keys off the
                    # exact reason plus the exact metadata-only
                    # ``primary_confirmed_absent`` boolean, never a Core-typed
                    # marker-bypass method.  bootstrap() rewrites a fresh marker
                    # and clears it only after durable completion plus readback.
                    # Any primary, including a partial one, is a typed
                    # ceremony-only refusal (this branch is not taken).
                    runtime.bootstrap()
                    outcome = "global_discovery_materialized"
                elif observation.reason_code == BOOTSTRAP_INCOMPLETE_REASON:
                    raise GlobalDiscoveryInitError(
                        "global_discovery_init_refused: incomplete-bootstrap"
                        " marker with a present/partial primary requires the"
                        " recovery ceremony before re-running init (zero"
                        " mutation)",
                        code="global_discovery_init_refused_marker_present",
                    )
                else:
                    raise GlobalDiscoveryInitError(
                        "global_discovery_init_refused: refusing to bootstrap over"
                        f" unreadable/residual state={obs_state.value if obs_state else 'unknown'}"
                        f"{reason}; resolve interrupted recovery through the recovery"
                        " ceremony before re-running init (zero mutation)",
                        code="global_discovery_init_refused",
                    )
    except BaseException as exc:
        guarded_error = exc
        raise
    finally:
        try:
            released_ok = lease.release()
        except BaseException:
            # Surface a release/fence failure only when the guarded body did
            # not already raise; otherwise preserve the earlier exception.
            if guarded_error is None:
                raise
        else:
            # A false release return is a fence loss: fail closed when the
            # guarded body succeeded; preserve an earlier exception otherwise.
            if not released_ok and guarded_error is None:
                raise GlobalDiscoveryInitError(
                    "global_discovery_init_release_failed: writer lease release"
                    " returned false (fence loss); init fails closed",
                    code="global_discovery_init_release_failed",
                )
    if outcome == "global_discovery_materialized":
        print("  Global Discovery: materialized")
    else:
        print("  Global Discovery: already present (idempotent no-op)")
    return outcome


def cmd_init(args):
    """Initialize ~/.okto-pulse/ directory and seed the database."""
    from okto_pulse.community.config import CommunitySettings

    settings = CommunitySettings()

    _fail_fast_if_server_running("init")

    handoff_argument = getattr(args, "bootstrap_key_handoff", None)
    handoff_path = (
        _validated_bootstrap_handoff_path(handoff_argument)
        if handoff_argument
        else None
    )
    if handoff_path is not None and getattr(args, "agents", None) is not None:
        print(
            "--bootstrap-key-handoff cannot be combined with --agents: "
            "choose exactly one credential destination.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if handoff_path is not None and os.path.lexists(handoff_path):
        print(
            "Unable to reserve bootstrap credential handoff: destination "
            f"already exists: {handoff_path}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    from okto_pulse.community.main import _ensure_data_dir

    mcp_port = getattr(args, "mcp_port", DEFAULT_MCP_PORT) or DEFAULT_MCP_PORT

    if mcp_port != DEFAULT_MCP_PORT:
        settings.mcp_port = mcp_port
    _ensure_data_dir(settings)

    data_path = Path(settings.data_dir)
    print(f"Okto Pulse Community initialized at: {data_path}")
    print(f"  Database: {data_path / 'data' / 'pulse.db'}")
    print(f"  Uploads:  {data_path / 'uploads'}")

    from okto_pulse.core import configure_settings
    from okto_pulse.community.adapters.sqlalchemy_database import (
        close_db,
        get_session_factory,
        init_db,
    )
    from okto_pulse.core import configure_auth
    from okto_pulse.core import configure_storage
    from okto_pulse.community.adapters.composition import community_storage_provider
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.community.adapters.kg_shutdown import (
        close_all_graphs_on_shutdown,
    )
    from okto_pulse.community.auth import LocalAuthProvider
    from okto_pulse.community.seed import seed_community_defaults
    from sqlalchemy import text as sa_text

    configure_settings(settings)
    configure_auth(LocalAuthProvider())
    configure_storage(community_storage_provider(settings.upload_dir))

    # R01C REPLAN-IMP4 (FR3/FR5): register the Community schema-lifecycle
    # orchestrator BEFORE init_db so the core delegates the migrate->create_all->seed
    # lifecycle to the edition (same migrator+bootstrapper as the serve path).
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )

    register_community_relational_schema_lifecycle()
    _configure_community_relational_runtime(settings, echo=False)

    handoff_reservation: _BootstrapKeyHandoffReservation | None = None
    if handoff_path is not None:
        try:
            # Reserve a hidden sibling before init_db/seed can mutate
            # persistent state. The final path stays absent until a complete,
            # fsynced credential is published atomically.
            handoff_reservation = _reserve_bootstrap_key_handoff(handoff_path)
        except (OSError, RuntimeError, ValueError) as exc:
            print(
                f"Unable to reserve bootstrap credential handoff: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc

    async def _init():
        revealed_agents: list[tuple[str, str]] = []
        # Blocker 9: init_db() is INSIDE the try so a partial failure still runs
        # the graph-runtime -> DB cleanup boundary (never strand graph handles /
        # a half-open engine).  Cleanup order stays graph runtime -> DB -> the
        # outer post-async barrier on success and on every failure.
        try:
            await init_db()
            session_factory = get_session_factory()

            # The demo seed is optional and may be skipped. Register the full
            # Community composition independently of that path, after the
            # relational schema exists and before any seed/bootstrap work.
            # Passing settings explicitly avoids resolving an implicit Core
            # fallback configuration.
            configure_community_kg_registry(
                session_factory,
                settings=settings,
            )

            # Global Discovery materialization acquires the durable writer lease,
            # which resolves the Community write-lock port; register the local
            # coordination providers before that acquisition.
            from okto_pulse.community.adapters.coordination import (
                register_community_coordination_providers,
            )

            register_community_coordination_providers()

            board_id = None
            primary_commit_delivered = False

            def _on_primary_committed(board, agent, api_key) -> None:
                nonlocal board_id, primary_commit_delivered
                if handoff_reservation is not None:
                    handoff_reservation.publish(api_key)
                revealed_agents.append((agent.name, api_key))
                board_id = board.id
                primary_commit_delivered = True
                print(f"\n  Board created: {board.name}")
                print(f"  Agent created: {agent.name}")
                if handoff_path is None:
                    print(f"  API Key: {api_key}")
                else:
                    print("  API Key: reserved for one-time automation handoff")

            async with session_factory() as db:
                result = await seed_community_defaults(
                    db,
                    on_primary_committed=_on_primary_committed,
                )
                if result:
                    board, agent, api_key = result
                    # Compatibility with a test double or older external seed
                    # implementation that returns the legacy tuple without
                    # invoking the new sink.
                    if not primary_commit_delivered:
                        _on_primary_committed(board, agent, api_key)
                else:
                    print("\n  Already initialized (seed exists).")
                    # Fetch the default board for KG bootstrap
                    board_result = await db.execute(
                        sa_text("SELECT id FROM boards ORDER BY created_at, id LIMIT 1")
                    )
                    board_row = board_result.mappings().first()
                    if board_row:
                        board_id = board_row["id"]

            # Bootstrap the board Knowledge Graph so the graph schema and
            # vector indexes are ready before the first agent call.  Both the
            # bootstrap and the proof that it worked cross registered ports, so
            # this says nothing about which engine stores the board.
            if board_id:
                _kg_token, _kg_ver = await _bootstrap_board_graph(board_id)
                print(f"  Knowledge Graph: {_kg_token} (schema {_kg_ver})")

            # Materialize the Global Discovery graph (``global/discovery.lbug``)
            # under the public writer-lease fence so cross-board discovery is
            # ready before the first global write. Fail-closed + idempotent.
            _bootstrap_global_discovery_graph()

            return revealed_agents
        finally:
            # ``init`` is a complete runtime lifecycle, not just a relational
            # migration command.  The demo consolidation and the primary-board
            # bootstrap both leave graph Database handles in the process-wide
            # cache.  Closing only SQLite lets interpreter teardown strand recent
            # commits in graph.lbug.wal (and can make strict WAL replay reject the
            # fresh Demo graph).  Reuse the same checkpoint+close boundary as the
            # server shutdown, off the event loop, before disposing SQLite.
            try:
                await asyncio.to_thread(close_all_graphs_on_shutdown)
            finally:
                await close_db()

    try:
        try:
            revealed_agents = asyncio.run(_init())
        finally:
            # ``asyncio.run`` drains/cancels tasks and shuts down its default
            # executor only after ``_init`` returns. A final synchronous barrier
            # therefore closes any graph handle opened by a late Global Discovery
            # task after the in-loop teardown. Idempotent on the normal path.
            close_all_graphs_on_shutdown()

        if handoff_reservation is not None:
            if not handoff_reservation.published:
                print(
                    "No freshly generated bootstrap credential is available for "
                    "handoff. Reveal-once credentials cannot be recovered from "
                    "the database.",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            print(f"  Bootstrap credential handoff ready: {handoff_reservation.path}")
    finally:
        if handoff_reservation is not None:
            handoff_reservation.discard()

    print("\nRun 'okto-pulse serve' to start the server.")

    # Handle --agents flag: generate .mcp.json with specified agents
    agents_param = getattr(args, "agents", None)
    if (
        agents_param is not None
    ):  # None = not specified, [] = specified but empty (all agents)
        _generate_mcp_json(
            settings.mcp_port, agents_param, revealed_agents=revealed_agents
        )


def _generate_mcp_json(
    mcp_port: int,
    agent_names: list[str] | None,
    revealed_agents: list[tuple[str, str]] | None = None,
):
    """Generate .mcp.json with specified agents (or all if agent_names is empty)."""
    import asyncio
    from sqlalchemy import text as sa_text
    from okto_pulse.community.adapters.sqlalchemy_database import (
        close_db,
        get_session_factory,
        init_db,
    )
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core import configure_auth
    from okto_pulse.core import configure_settings
    from okto_pulse.community.auth import LocalAuthProvider
    from okto_pulse.core import configure_storage
    from okto_pulse.community.adapters.composition import community_storage_provider

    settings = CommunitySettings()
    configure_settings(settings)
    configure_auth(LocalAuthProvider())
    configure_storage(community_storage_provider(settings.upload_dir))
    _configure_community_relational_runtime(settings, echo=False)
    # R01C REPLAN-IMP4: Community owns the schema lifecycle here too — register
    # the orchestrator so this command's init_db delegates to the edition
    # migrator+bootstrapper (idempotent; same lifecycle as serve/init).
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )

    register_community_relational_schema_lifecycle()

    async def _fetch_agents():
        await init_db()
        async with get_session_factory()() as db:
            # Fetch all active agents with API keys
            result = await db.execute(
                sa_text(
                    "SELECT name, api_key FROM agents "
                    "WHERE api_key IS NOT NULL ORDER BY name"
                )
            )
            all_agents = _result_records(result)
            exportable_by_name: dict[str, _ExportableAgentCredential] = {}
            for agent in all_agents:
                credential = _exportable_credential_from_legacy_agent(agent)
                if credential is not None:
                    exportable_by_name[credential.name] = credential
            for name, key in revealed_agents or []:
                credential = _exportable_credential_from_reveal_once(name, key)
                if credential is not None:
                    exportable_by_name[credential.name] = credential
            exportable_agents = list(exportable_by_name.values())
            all_agent_names = {_field(a, "name") for a in all_agents} | {
                name for name, _key in revealed_agents or []
            }

            if not exportable_agents:
                print("\n  ⚠ No recoverable agent API keys found.")
                print(
                    "  Newly created keys are reveal-once; regenerate one in the UI/API if needed."
                )
                await close_db()
                return None

            # Filter by name if specified
            if agent_names:  # Specific names provided
                name_set = {name.strip() for name in agent_names}
                found_agents = [a for a in exportable_agents if a.name in name_set]
                missing = name_set - all_agent_names
                unrecoverable = (all_agent_names & name_set) - set(exportable_by_name)

                if not found_agents:
                    print(
                        f"\n  ⚠ No matching agents found: {', '.join(sorted(name_set))}"
                    )
                    print(
                        f"  Available exportable agents: {', '.join(a.name for a in exportable_agents)}"
                    )
                    await close_db()
                    return None

                if missing:
                    print(f"\n  ⚠ Agents not found: {', '.join(sorted(missing))}")
                if unrecoverable:
                    print(
                        "\n  ⚠ Agents skipped because their keys are reveal-once only: "
                        f"{', '.join(sorted(unrecoverable))}"
                    )

                agents_to_export = found_agents
            else:  # No names provided = export all
                agents_to_export = exportable_agents

            await close_db()
            return agents_to_export

    agents = asyncio.run(_fetch_agents())
    if agents is None:
        return

    # Build mcp.json with multiple agents
    mcp_config = {"mcpServers": {}}
    for agent in agents:
        # Use a sanitized name for the server key (replace spaces with hyphens)
        server_key = agent.name.lower().replace(" ", "-").replace("_", "-")
        mcp_config["mcpServers"][server_key] = {
            "url": f"http://127.0.0.1:{mcp_port}/mcp?api_key={agent.plaintext}"
        }

    mcp_json_path = Path.cwd() / ".mcp.json"
    mcp_json_path.write_text(json.dumps(mcp_config, indent=2))

    agent_list = ", ".join(f'"{a.name}"' for a in agents)
    print(f"\n  ✓ .mcp.json generated at: {mcp_json_path}")
    print(f"  Agents exported: {agent_list}")


def cmd_serve(args):
    """Start the API + Frontend server and the MCP server.

    Both servers run inside a single Python process (so the embedded Kùzu
    DB is owned by exactly one OS process), but listen on two different
    ports — ``--api-port`` for the REST API + UI, ``--mcp-port`` for the
    MCP transport. Each port has its own uvicorn ``Server`` instance
    driven concurrently via ``asyncio.gather``.
    """
    api_port = args.api_port
    mcp_port = args.mcp_port

    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.community.data_home import (
        UninitializedDefaultDataHomeError,
        assert_serve_data_home_ready,
        data_home_banner_lines,
    )
    from okto_pulse.community.serve_lock import (
        ServeAlreadyRunningError,
        acquire_serve_lock,
    )

    settings = CommunitySettings()
    try:
        assert_serve_data_home_ready(settings)
    except UninitializedDefaultDataHomeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    if _is_port_in_use(api_port):
        print(
            f"Warning: Port {api_port} is already in use. API server may fail to start."
        )
    if _is_port_in_use(mcp_port):
        print(
            f"Warning: Port {mcp_port} is already in use. MCP server may fail to start."
        )

    # Ports go via env so create_community_app + the MCP runner read them.
    # MUST be set BEFORE _serve_dual calls get_module_app() — the lazy
    # composition build reads the env vars to inject /config.js with the
    # correct API_URL/MCP_URL.
    os.environ["OKTO_PULSE_PORT"] = str(api_port)
    os.environ["OKTO_PULSE_MCP_PORT"] = str(mcp_port)
    frontend_dir = Path(__file__).resolve().parent / "frontend_dist"
    has_frontend = frontend_dir.exists() and (frontend_dir / "index.html").exists()

    try:
        with acquire_serve_lock(settings):
            for line in data_home_banner_lines(settings):
                print(f"  {line}")

            # Terms-of-Use pre-acceptance via CLI flag or env var.
            if getattr(args, "accept_terms", False):
                os.environ["OKTO_PULSE_TERMS_ACCEPTED"] = "1"
                from okto_pulse.community.acceptance import write_acceptance

                rec = write_acceptance("cli")
                print(
                    f"Terms-of-Use pre-accepted via --accept-terms (version {rec['version']})."
                )
            elif (os.environ.get("OKTO_PULSE_TERMS_ACCEPTED") or "").strip() == "1":
                from okto_pulse.community.acceptance import (
                    write_acceptance,
                    read_acceptance,
                )

                if read_acceptance() is None:
                    rec = write_acceptance("env")
                    print(
                        f"Terms-of-Use pre-accepted via env (version {rec['version']})."
                    )

            print("Starting Okto Pulse Community...")
            if has_frontend:
                print(f"  App:  http://127.0.0.1:{api_port}  (API + Frontend)")
            else:
                print(f"  API:  http://127.0.0.1:{api_port}  (no frontend embedded)")
            print(f"  MCP:  http://127.0.0.1:{mcp_port}/mcp")
            print("  Press Ctrl+C to stop.\n")

            # Single-process, dual-port: run() spawns two uvicorn Server instances
            # via asyncio.gather. uvicorn signal capture is DISABLED for both; main.py handles SIGINT (asyncio.Runner) and installs SIGTERM/SIGBREAK handlers for the ordered shutdown (KGD-01).
            from okto_pulse.community.main import run

            exit_code = run()
            if exit_code:
                sys.exit(exit_code)
    except ServeAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)


def _emit_code_traceability_diagnostics(payload, *, emit_json: bool) -> None:
    """Render bounded diagnostics without exposing acquisition controls."""

    if emit_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    if isinstance(payload, list):
        if not payload:
            print("No persisted records matched the filters.")
            return
        for record in payload:
            subject = (
                f"{record.get('subject_type', '-')}:{record.get('subject_id', '-')}"
            )
            state = record.get("status", record.get("outcome", "-"))
            timestamp = record.get("created_at", record.get("received_at", "-"))
            print(f"{record.get('id', '-')}  {subject}  {state}  {timestamp}")
        return
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _configured_code_traceability_database_path(database_url: str) -> Path:
    """Resolve the configured Community SQLite database for diagnostics."""

    from urllib.parse import unquote

    from sqlalchemy.engine import make_url

    from okto_pulse.community.commands.code_traceability_diagnostics import (
        CodeTraceabilityDiagnosticsError,
    )

    try:
        url = make_url(str(database_url))
    except Exception as exc:
        raise CodeTraceabilityDiagnosticsError(
            "code_traceability_database_url_invalid",
            "The configured database URL is invalid",
        ) from exc
    database = unquote(str(url.database or ""))
    if url.get_backend_name() != "sqlite" or database in {"", ":memory:"}:
        raise CodeTraceabilityDiagnosticsError(
            "code_traceability_database_url_unsupported",
            "Diagnostics require a configured file-backed SQLite database",
        )
    if os.name == "nt" and database.startswith("/") and len(database) > 2:
        if database[2] == ":":
            database = database[1:]
    return Path(database).expanduser().resolve()


def cmd_code_traceability(args):
    """Inspect Pulse-owned Code Traceability state without investigating source."""

    from okto_pulse.community.commands.code_traceability_diagnostics import (
        CodeTraceabilityDiagnosticsError,
        diagnose,
        inspect_record,
        list_receipts,
        list_requests,
        open_read_only_database,
    )
    from okto_pulse.community.config import CommunitySettings

    settings = CommunitySettings()
    command = args.code_traceability_command
    emit_json = bool(getattr(args, "json", False))
    try:
        db_path = _configured_code_traceability_database_path(settings.database_url)
        with open_read_only_database(str(db_path)) as connection:
            if command == "requests":
                payload = list_requests(
                    connection,
                    board_id=args.board_id,
                    status=args.status,
                    limit=args.limit,
                )
            elif command == "receipts":
                payload = list_receipts(
                    connection,
                    board_id=args.board_id,
                    outcome=args.outcome,
                    limit=args.limit,
                )
            elif command == "inspect":
                payload = inspect_record(
                    connection,
                    board_id=args.board_id,
                    kind=args.kind,
                    record_id=args.record_id,
                )
            elif command == "diagnose":
                payload = diagnose(connection, board_id=args.board_id)
            else:
                raise CodeTraceabilityDiagnosticsError(
                    "code_traceability_diagnostics_command_invalid",
                    "A diagnostics subcommand is required",
                )
    except CodeTraceabilityDiagnosticsError as exc:
        error = {"code": exc.code, "message": str(exc)}
        if emit_json:
            print(json.dumps(error, sort_keys=True), file=sys.stderr)
        else:
            print(f"ERROR [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    _emit_code_traceability_diagnostics(payload, emit_json=emit_json)
    if command == "diagnose" and not payload["healthy"]:
        raise SystemExit(1)


def cmd_status(args):
    """Show status of Okto Pulse Community."""
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.community.commands.status import collect_status, render_status

    report = collect_status(
        CommunitySettings(),
        api_port=args.api_port,
        mcp_port=args.mcp_port,
        port_probe=_is_port_in_use,
    )
    exit_code = render_status(
        report,
        json_output=getattr(args, "json", False),
        api_port=args.api_port,
        mcp_port=args.mcp_port,
    )
    if exit_code:
        raise SystemExit(exit_code)


def _metrics_window_days(text: str) -> int:
    try:
        return validate_window_days(int(text))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def cmd_metrics(args):
    """Control metrics On/Off settings and local data."""
    if args.metrics_command == "status":
        try:
            validate_window_days(args.window_days)
        except ValueError as exc:
            print(f"okto-pulse metrics status: error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    from okto_pulse.community.adapters.telemetry_composition import (
        register_community_telemetry_runtime,
    )
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.telemetry.telemetry_port_registry import get_telemetry_port

    # Compose the same complete vertical used by the server. Partial registration
    # would leave fail-closed effect configuration unresolved for state/target refs.
    register_community_telemetry_runtime()

    settings = CommunitySettings()
    service = get_telemetry_port(settings)
    command = args.metrics_command

    if command == "status":
        print(
            json.dumps(
                service.summary(window_days=args.window_days), indent=2, sort_keys=True
            )
        )
        return

    if command == "enable-beacon":
        if not args.yes:
            print(
                "CONFIRMATION_REQUIRED: pass --yes after reviewing schema and privacy policy.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        acknowledged_items = [
            "hourly_aggregates",
            "local_control",
            "no_pii",
            "product_aggregates",
            "privacy_policy",
            "schema",
        ]
        result = service.update_settings(
            mode="anonymous_beacon",
            source="cli",
            policy_version=args.policy_version,
            schema_version=args.schema_version,
            acknowledged_items=acknowledged_items,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if command in {"local-only", "disable"}:
        if command == "local-only":
            _METRICS_CLI_LOGGER.info(
                "metrics.cli.legacy_local_only",
                extra={
                    "metric_name": "metrics_cli_legacy_local_only_total",
                    "outcome": "mapped_to_disabled",
                },
            )
        result = service.update_settings(mode="disabled", source="cli")
        if command == "local-only":
            result["legacy_alias"] = "local-only"
            result["message"] = (
                "The legacy local-only command is deprecated; metrics are now Off."
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if command == "export":
        result = service.export_local(args.output)
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if command == "purge-local":
        if not args.yes:
            print(
                "CONFIRMATION_REQUIRED: pass --yes to purge local metrics files.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        result = service.purge_local()
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    raise SystemExit(f"Unknown metrics command: {command}")


def cmd_api_key(args):
    """Consume a reveal-once bootstrap credential handoff.

    Used by release automation after ``okto-pulse init
    --bootstrap-key-handoff``. The handoff is atomically claimed and deleted
    whether it is valid or invalid. This handoff branch never reads the agent
    table: persisted credentials are hashes/markers and are intentionally
    non-recoverable. Without ``--handoff-file``, the governed legacy database
    fallback remains available only for installations that still contain a
    plaintext key.

    Exit codes:
      0 — key printed
      1 — handoff missing/invalid, DB unavailable, or persisted key is hashed

    Output format: a single line containing the key on stdout. Banner
    goes to stderr so this is safe to pipe.
    """
    handoff_file = getattr(args, "handoff_file", None)
    if handoff_file:
        try:
            credential = _consume_bootstrap_key_handoff(handoff_file)
        except (OSError, RuntimeError, ValueError) as exc:
            print(
                f"Unable to consume bootstrap credential handoff: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        print(credential)
        return

    import sqlite3
    from okto_pulse.community.config import CommunitySettings

    settings = CommunitySettings()
    db_path = Path(settings.data_dir) / "data" / "pulse.db"
    if not db_path.exists():
        print(
            f"Database not found at {db_path}. Run 'okto-pulse init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT api_key FROM agents WHERE api_key IS NOT NULL "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        print(
            f"Database not initialised: {exc}. Run 'okto-pulse init' first.",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        conn.close()

    if row is None or not row[0]:
        print("No bootstrap API key found in database.", file=sys.stderr)
        sys.exit(1)
    if not _is_recoverable_agent_key(row[0]):
        print(
            "Bootstrap API key is reveal-once and is not recoverable from the database.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(row[0])


def main():
    raw_argv = list(sys.argv[1:])
    metrics_legacy_local_only = (
        len(raw_argv) >= 2 and raw_argv[0] == "metrics" and raw_argv[1] == "local-only"
    )
    if metrics_legacy_local_only:
        raw_argv = ["metrics", "disable", *raw_argv[2:]]

    parser = argparse.ArgumentParser(
        prog="okto-pulse",
        description="Okto Pulse Community — local-first kanban board with MCP support for AI agents",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=_format_version(),
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init
    sub_init = subparsers.add_parser(
        "init", help="Initialize data directory and seed database"
    )
    sub_init.add_argument(
        "--agents",
        nargs="*",
        metavar="NAME",
        help="Export specific agents to .mcp.json (comma-separated names, or all if empty)",
    )
    sub_init.add_argument(
        "--bootstrap-key-handoff",
        metavar="ABSOLUTE_PATH",
        help="Write the freshly seeded reveal-once API key to a new private "
        "file for automation. The path must not exist; use a volatile "
        "filesystem and consume it with 'api-key --handoff-file'. POSIX uses "
        "mode 0600; on Windows provide a directory with a private ACL.",
    )
    sub_init.set_defaults(func=cmd_init)

    # serve
    sub_serve = subparsers.add_parser(
        "serve", help="Start API + Frontend + MCP servers"
    )
    sub_serve.add_argument(
        "--api-port",
        type=int,
        default=DEFAULT_API_PORT,
        help=f"API + Frontend server port (default: {DEFAULT_API_PORT})",
    )
    sub_serve.add_argument(
        "--mcp-port",
        type=int,
        default=DEFAULT_MCP_PORT,
        help=f"MCP server port (default: {DEFAULT_MCP_PORT})",
    )
    sub_serve.add_argument(
        "--accept-terms",
        action="store_true",
        help="Pre-accept the Terms-of-Use & License (skips the first-run modal). "
        "Equivalent to setting OKTO_PULSE_TERMS_ACCEPTED=1.",
    )
    sub_serve.set_defaults(func=cmd_serve)

    # status
    sub_status = subparsers.add_parser(
        "status", help="Show service status and DB metrics"
    )
    sub_status.add_argument(
        "--json", action="store_true", help="Emit one machine-readable status object"
    )
    sub_status.add_argument(
        "--api-port",
        type=int,
        default=DEFAULT_API_PORT,
        help=f"API server port (default: {DEFAULT_API_PORT})",
    )
    sub_status.add_argument(
        "--mcp-port",
        type=int,
        default=DEFAULT_MCP_PORT,
        help=f"MCP server port (default: {DEFAULT_MCP_PORT})",
    )
    sub_status.set_defaults(func=cmd_status)

    # Read-only Code Traceability operator diagnostics. Investigation and
    # source access remain responsibilities of the authenticated external agent.
    sub_traceability = subparsers.add_parser(
        "code-traceability",
        help="Inspect persisted investigation requests, receipts, schema, and policy",
    )
    traceability_sub = sub_traceability.add_subparsers(
        dest="code_traceability_command",
        help="Code Traceability diagnostics",
    )

    traceability_requests = traceability_sub.add_parser(
        "requests",
        help="List persisted investigation requests",
    )
    traceability_requests.add_argument("board_id", help="Board UUID")
    traceability_requests.add_argument(
        "--status",
        choices=("open", "consumed", "expired", "revoked"),
    )
    traceability_requests.add_argument("--limit", type=int, default=50)
    traceability_requests.add_argument("--json", action="store_true")
    traceability_requests.set_defaults(func=cmd_code_traceability)

    traceability_receipts = traceability_sub.add_parser(
        "receipts",
        help="List persisted agent-attested receipts",
    )
    traceability_receipts.add_argument("board_id", help="Board UUID")
    traceability_receipts.add_argument(
        "--outcome",
        choices=("accessible", "partial", "unavailable"),
    )
    traceability_receipts.add_argument("--limit", type=int, default=50)
    traceability_receipts.add_argument("--json", action="store_true")
    traceability_receipts.set_defaults(func=cmd_code_traceability)

    traceability_inspect = traceability_sub.add_parser(
        "inspect",
        help="Inspect one persisted request or receipt",
    )
    traceability_inspect.add_argument("board_id", help="Board UUID")
    traceability_inspect.add_argument("kind", choices=("request", "receipt"))
    traceability_inspect.add_argument("record_id", help="Request or receipt ID")
    traceability_inspect.add_argument("--json", action="store_true")
    traceability_inspect.set_defaults(func=cmd_code_traceability)

    traceability_diagnose = traceability_sub.add_parser(
        "diagnose",
        help="Validate persisted schema and board policy",
    )
    traceability_diagnose.add_argument("board_id", help="Board UUID")
    traceability_diagnose.add_argument("--json", action="store_true")
    traceability_diagnose.set_defaults(func=cmd_code_traceability)

    # metrics
    sub_metrics = subparsers.add_parser(
        "metrics",
        help="Control metrics On/Off, export, and purge",
    )
    metrics_sub = sub_metrics.add_subparsers(
        dest="metrics_command", help="Metrics commands"
    )

    metrics_status = metrics_sub.add_parser("status", help="Show metrics status")
    metrics_status.add_argument(
        "--window-days", type=_metrics_window_days, default=DEFAULT_WINDOW_DAYS
    )
    metrics_status.set_defaults(func=cmd_metrics)

    metrics_enable = metrics_sub.add_parser(
        "enable-beacon", help="Turn metrics On with anonymous hourly aggregates"
    )
    metrics_enable.add_argument("--policy-version", required=True)
    from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION

    metrics_enable.add_argument("--schema-version", default=CURRENT_SCHEMA_VERSION)
    metrics_enable.add_argument(
        "--yes", action="store_true", help="Confirm opt-in prerequisites"
    )
    metrics_enable.set_defaults(func=cmd_metrics)

    metrics_disable = metrics_sub.add_parser("disable", help="Turn metrics Off")
    metrics_disable.set_defaults(func=cmd_metrics)

    metrics_export = metrics_sub.add_parser("export", help="Export local metrics JSONL")
    metrics_export.add_argument("--output")
    metrics_export.set_defaults(func=cmd_metrics)

    metrics_purge = metrics_sub.add_parser(
        "purge-local", help="Purge local metrics files"
    )
    metrics_purge.add_argument("--yes", action="store_true", help="Confirm local purge")
    metrics_purge.set_defaults(func=cmd_metrics)

    # api-key — atomically consume an explicit reveal-once handoff.
    sub_apikey = subparsers.add_parser(
        "api-key",
        help="Consume a reveal-once bootstrap API key handoff",
    )
    sub_apikey.add_argument(
        "--handoff-file",
        metavar="ABSOLUTE_PATH",
        help="Private file created by 'init --bootstrap-key-handoff'; "
        "it is deleted after this read. Without this option, only governed "
        "legacy plaintext database keys remain exportable.",
    )
    sub_apikey.set_defaults(func=cmd_api_key)

    args = parser.parse_args(raw_argv)
    if metrics_legacy_local_only:
        args.metrics_command = "local-only"
    if not args.command:
        _print_banner()
        parser.print_help()
        sys.exit(1)
    if args.command == "metrics" and not getattr(args, "metrics_command", None):
        _print_banner()
        sub_metrics.print_help()
        sys.exit(1)
    if args.command == "code-traceability" and not getattr(
        args, "code_traceability_command", None
    ):
        _print_banner()
        sub_traceability.print_help()
        sys.exit(1)

    if not (args.command == "status" and getattr(args, "json", False)):
        _print_banner()
    args.func(args)


if __name__ == "__main__":
    main()
