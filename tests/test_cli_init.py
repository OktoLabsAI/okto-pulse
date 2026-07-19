"""Unit tests for the `okto-pulse init` CLI command.

Tests argparse wiring via subprocess (matching the pattern in
test_cli_kg_backfill.py).  Full cmd_init flow requires a real DB
so we only test the subparser shape here.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_SRC = Path(__file__).parent.parent / "src"
WORKSPACE_ROOT = Path(__file__).parent.parent.parent
CORE_SRC_CANDIDATES = (
    WORKSPACE_ROOT / "okto_labs_pulse_core" / "src",
    WORKSPACE_ROOT / "okto-pulse-core" / "src",
)

for p in (str(REPO_SRC), *(str(path) for path in CORE_SRC_CANDIDATES if path.exists())):
    if p not in sys.path:
        sys.path.insert(0, p)

# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def test_init_subparser_has_agents_flag():
    """The init subparser exposes --agents (nargs='*')."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, r'{}'); "
            "from okto_pulse.community.cli import main; main()".format(str(REPO_SRC)),
            "init",
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--agents" in result.stdout


def test_init_subparser_no_args_shows_help(tmp_path):
    """Running `okto-pulse init` with no subcommand prints help and exits 1."""
    env = dict(os.environ)
    env["DATA_DIR"] = str(tmp_path / "data")
    env["OKTO_PULSE_HOME"] = str(tmp_path / "home")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, r'{}'); "
            "from okto_pulse.community.cli import main; main()".format(str(REPO_SRC)),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 1


def test_init_registers_community_kg_before_demo_skip_and_fails_closed(
    tmp_path, monkeypatch, capsys
):
    """A skipped demo cannot be the hidden owner of KG composition wiring."""
    import okto_pulse.core as core
    import okto_pulse.community.adapters.composition as composition
    import okto_pulse.community.adapters.kg_shutdown as kg_shutdown
    import okto_pulse.community.adapters.relational_schema_lifecycle as lifecycle
    import okto_pulse.community.adapters.sqlalchemy_database as database
    import okto_pulse.community.auth as community_auth
    import okto_pulse.community.cli as cli
    import okto_pulse.community.config as community_config
    import okto_pulse.community.main as community_main
    import okto_pulse.community.seed as community_seed
    import okto_pulse.core.services.application_kg as application_kg

    events: list[str] = []
    factory = lambda: _FakeSession([])  # noqa: E731 - identity asserted below

    class Settings:
        def __init__(self):
            self.data_dir = str(tmp_path / "pulse")
            self.upload_dir = str(tmp_path / "pulse" / "uploads")
            self.database_url = "sqlite+aiosqlite:///:memory:"
            self.mcp_port = 8101

    async def fake_init_db():
        events.append("init_db")

    async def fake_close_db():
        events.append("close_db")

    def fake_close_graphs():
        events.append("kg_shutdown")
        return {"boards_closed": 0, "boards_failed": 0, "duration_ms": 0}

    def fake_configure_kg(received_factory, *, settings):
        assert events == ["init_db"]
        assert received_factory is factory
        assert isinstance(settings, Settings)
        events.append("configure_community_kg")

    async def fake_seed(_db):
        assert os.environ[community_seed.DEMO_SKIP_ENV] == "1"
        assert events[-1] == "configure_community_kg"
        events.append("seed_demo_skipped")
        return (
            SimpleNamespace(id="board-1", name="My Board"),
            SimpleNamespace(name="Local Agent"),
            "dash_revealed_once",
        )

    class FailingGraphSchema:
        async def ensure_bootstrapped(self, board_id):
            assert board_id == "board-1"
            events.append("bootstrap")
            raise RuntimeError("kg bootstrap failed")

    monkeypatch.setenv(community_seed.DEMO_SKIP_ENV, "1")
    monkeypatch.setattr(cli, "_fail_fast_if_server_running", lambda _op: None)
    monkeypatch.setattr(community_config, "CommunitySettings", Settings)
    monkeypatch.setattr(community_main, "_ensure_data_dir", lambda _settings: None)
    monkeypatch.setattr(core, "configure_settings", lambda _settings: None)
    monkeypatch.setattr(core, "configure_auth", lambda _provider: None)
    monkeypatch.setattr(core, "configure_storage", lambda _provider: None)
    monkeypatch.setattr(community_auth, "LocalAuthProvider", lambda: object())
    monkeypatch.setattr(composition, "community_storage_provider", lambda _path: None)
    monkeypatch.setattr(
        composition,
        "configure_community_kg_registry",
        fake_configure_kg,
    )
    monkeypatch.setattr(
        cli, "_configure_community_relational_runtime", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(database, "init_db", fake_init_db)
    monkeypatch.setattr(database, "close_db", fake_close_db)
    monkeypatch.setattr(database, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        kg_shutdown,
        "close_all_graphs_on_shutdown",
        fake_close_graphs,
    )
    monkeypatch.setattr(community_seed, "seed_community_defaults", fake_seed)
    monkeypatch.setattr(
        lifecycle, "register_community_relational_schema_lifecycle", lambda: None
    )
    monkeypatch.setattr(
        application_kg,
        "get_current_provider_registry",
        lambda: SimpleNamespace(graph_schema_manager=FailingGraphSchema()),
    )

    with pytest.raises(RuntimeError, match="kg bootstrap failed"):
        cli.cmd_init(SimpleNamespace(mcp_port=8101, agents=None))

    assert events == [
        "init_db",
        "configure_community_kg",
        "seed_demo_skipped",
        "bootstrap",
        "kg_shutdown",
        "close_db",
        # Post-async barrier: runs after the loop drains late executor work.
        "kg_shutdown",
    ]
    captured = capsys.readouterr()
    assert "bootstrap skipped" not in captured.out
    assert "bootstrap skipped" not in captured.err


def test_init_real_engine_closes_wals_and_reopens_every_graph_strictly_offline(
    tmp_path,
):
    """First boot is durable without WAL replay recovery or an HF download."""
    import gc

    ladybug = pytest.importorskip("ladybug")

    pulse_home = tmp_path / "pulse-home"
    hf_home = tmp_path / "empty-hf-cache"
    env = dict(os.environ)
    source_paths = [REPO_SRC, *(p for p in CORE_SRC_CANDIDATES if p.exists())]
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        [
            *(str(path) for path in source_paths),
            *([existing_pythonpath] if existing_pythonpath else []),
        ]
    )
    env.update(
        {
            "OKTO_PULSE_HOME": str(pulse_home),
            "OKTO_PULSE_SKIP_DEMO_SEED": "0",
            # Keep the normal runtime semantic. The demo must override this in
            # its isolated provider scope and therefore succeed with a brand-new,
            # offline cache containing no model weights.
            "KG_EMBEDDING_MODE": "sentence-transformers",
            "HF_HOME": str(hf_home),
            "SENTENCE_TRANSFORMERS_HOME": str(hf_home),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "okto_pulse.community.cli", "init"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "community.seed.demo_failed" not in output
    assert "Knowledge Graph:" in output
    assert not [path for path in hf_home.rglob("*") if path.is_file()]

    conn = sqlite3.connect(pulse_home / "data" / "pulse.db")
    try:
        boards = conn.execute("SELECT id, name FROM boards ORDER BY name").fetchall()
        cards = conn.execute(
            "SELECT b.name, c.title, c.status, c.card_type "
            "FROM cards c JOIN boards b ON b.id = c.board_id "
            "ORDER BY b.name, c.position"
        ).fetchall()
    finally:
        conn.close()

    assert [name for _board_id, name in boards] == ["Demo", "My Board"]
    assert cards == [
        ("Demo", "Demo Normal Card", "not_started", "normal"),
        ("Demo", "Demo Bug Card", "not_started", "bug"),
        ("Demo", "Demo Test Card", "not_started", "test"),
    ]

    expected_counts = {
        "Demo": (5, 3),  # BoardMeta + 4 cognitive nodes, all connected.
        "My Board": (1, 0),  # Schema bootstrap materializes BoardMeta only.
    }
    for board_id, board_name in boards:
        graph_path = pulse_home / "boards" / board_id / "graph.lbug"
        wal_path = graph_path.with_name(f"{graph_path.name}.wal")
        assert graph_path.exists()
        assert not wal_path.exists() or wal_path.stat().st_size == 0

        db = ladybug.Database(
            str(graph_path),
            throw_on_wal_replay_failure=True,
        )
        graph = ladybug.Connection(db)
        try:
            node_count = graph.execute("MATCH (n) RETURN count(n)").get_next()[0]
            edge_count = graph.execute("MATCH ()-[r]->() RETURN count(r)").get_next()[0]
            assert (node_count, edge_count) == expected_counts[board_name]
            if board_name == "Demo":
                relates_to = graph.execute(
                    "MATCH ()-[r:relates_to]->(d:Decision) RETURN count(r)"
                ).get_next()[0]
                assert relates_to == 3
        finally:
            graph.close()
            db.close()
            del graph, db
            gc.collect()

    global_path = pulse_home / "global" / "discovery.lbug"
    global_wal = global_path.with_name(f"{global_path.name}.wal")
    assert global_path.exists()
    assert not global_wal.exists() or global_wal.stat().st_size == 0
    global_db = ladybug.Database(
        str(global_path),
        throw_on_wal_replay_failure=True,
    )
    global_graph = ladybug.Connection(global_db)
    try:
        tables_result = global_graph.execute("CALL SHOW_TABLES() RETURN name")
        tables = set()
        while tables_result.has_next():
            tables.add(tables_result.get_next()[0])
        assert {"Board", "Topic", "Entity", "DecisionDigest"} <= tables
    finally:
        global_graph.close()
        global_db.close()
        del global_graph, global_db
        gc.collect()


# ---------------------------------------------------------------------------
# AF14 TS6 - deferred Community migration contract
# ---------------------------------------------------------------------------


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        return _FakeScalarResult(self._rows)


def _patch_mcp_export_runtime(monkeypatch, rows):
    import okto_pulse.core as core
    import okto_pulse.community.adapters.composition as composition
    import okto_pulse.community.adapters.relational_schema_lifecycle as lifecycle
    import okto_pulse.community.adapters.sqlalchemy_database as database
    import okto_pulse.community.cli as cli

    async def fake_init_db():
        return None

    async def fake_close_db():
        return None

    monkeypatch.setattr(core, "configure_settings", lambda _settings: None)
    monkeypatch.setattr(core, "configure_auth", lambda _provider: None)
    monkeypatch.setattr(core, "configure_storage", lambda _provider: None)
    monkeypatch.setattr(composition, "community_storage_provider", lambda _path: None)
    monkeypatch.setattr(
        cli,
        "_configure_community_relational_runtime",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(database, "init_db", fake_init_db)
    monkeypatch.setattr(database, "close_db", fake_close_db)
    monkeypatch.setattr(
        database, "get_session_factory", lambda: lambda: _FakeSession(rows)
    )
    monkeypatch.setattr(
        lifecycle, "register_community_relational_schema_lifecycle", lambda: None
    )


def test_af14_ts6_mcp_export_skips_deferred_markers_but_exports_revealed_and_legacy(
    tmp_path, monkeypatch, capsys
):
    from okto_pulse.community.cli import _generate_mcp_json

    rows = [
        SimpleNamespace(name="Deferred Agent", api_key="sha256:deadbeef"),
        SimpleNamespace(name="Legacy Agent", api_key="dash_legacy_plaintext"),
    ]
    _patch_mcp_export_runtime(monkeypatch, rows)
    monkeypatch.chdir(tmp_path)

    _generate_mcp_json(
        8101,
        ["Deferred Agent", "Legacy Agent", "New Agent"],
        revealed_agents=[("New Agent", "dash_revealed_once")],
    )

    output = capsys.readouterr().out
    config = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    urls = {name: server["url"] for name, server in config["mcpServers"].items()}

    assert "reveal-once only: Deferred Agent" in output
    assert "deferred-agent" not in urls
    assert urls["legacy-agent"].endswith("?api_key=dash_legacy_plaintext")
    assert urls["new-agent"].endswith("?api_key=dash_revealed_once")


def test_af14_ts6_mcp_export_marker_only_writes_no_config(
    tmp_path, monkeypatch, capsys
):
    from okto_pulse.community.cli import _generate_mcp_json

    rows = [SimpleNamespace(name="Deferred Agent", api_key="sha256:deadbeef")]
    _patch_mcp_export_runtime(monkeypatch, rows)
    monkeypatch.chdir(tmp_path)

    _generate_mcp_json(8101, ["Deferred Agent"], revealed_agents=[])

    output = capsys.readouterr().out
    assert "No recoverable agent API keys found" in output
    assert "reveal-once; regenerate one in the UI/API if needed" in output
    assert not (tmp_path / ".mcp.json").exists()


def test_af14_ts6_api_key_cli_refuses_reveal_once_marker(tmp_path, monkeypatch, capsys):
    import okto_pulse.community.config as community_config
    from okto_pulse.community.cli import cmd_api_key

    data_dir = tmp_path / "pulse-data"
    db_dir = data_dir / "data"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "pulse.db")
    try:
        conn.execute("CREATE TABLE agents (api_key TEXT, created_at TEXT)")
        conn.execute(
            "INSERT INTO agents (api_key, created_at) VALUES (?, ?)",
            ("sha256:deferred-marker", "2026-07-03T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    Settings = type("Settings", (), {"data_dir": str(data_dir)})
    monkeypatch.setattr(community_config, "CommunitySettings", Settings)

    with pytest.raises(SystemExit) as exc_info:
        cmd_api_key(SimpleNamespace())

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.out == ""
    assert "reveal-once and is not recoverable" in captured.err


# ---------------------------------------------------------------------------
# A5: `okto-pulse init` Global Discovery LITERAL four-state x two-invocation
# matrix with typed codes, exact bootstrap counts, per-call observed state, a
# complete Windows-stable physical fingerprint, a single exact lease object, and
# the acquire/guard/release unwind matrix (Nexus
# msg_424bda2a9a5c44d5b3a8e52efd9d5a6e + durable INV-E2 marker contract).
# ---------------------------------------------------------------------------

import hashlib
import os as _os
from contextlib import contextmanager

import okto_pulse.core.services.application_kg as _application_kg
import okto_pulse.community.cli as _cli
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime as _GDRuntime,
)
from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
    bootstrap_marker_present as _marker_present,
    write_bootstrap_marker as _write_marker,
)
from okto_pulse.core.kg.global_discovery_writer import (
    GlobalDiscoveryWriterContention as _WriterContention,
    GlobalDiscoveryWriterLease as _WriterLease,
)
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeObservationState as _ObsState,
)


class _A5Lock:
    def is_owner(self, _board_id, _owner_token):
        return True

    def release(self, *, board_id, owner_token):
        del board_id, owner_token
        return True


class _RecordingLease(_WriterLease):
    """Blocker 13: ONE lease object — the acquired instance itself enters
    ``guard()``, is the active token used by the runtime fence assertion, exits
    on EVERY path, and is released.  No delegation to a different instance."""

    @contextmanager
    def guard(self):
        self.events.append("guard_enter")
        try:
            with super().guard():
                yield
        finally:
            # guard exit is recorded on every path (success and failure).
            self.events.append("guard_exit")

    def release(self):
        self.events.append("release")
        if self.release_raises is not None:
            raise self.release_raises
        if self.release_result is not None:
            # Still perform the real release side effects, then override return.
            super().release()
            return self.release_result
        return super().release()


def _make_recording_lease(*, release_result=None, release_raises=None):
    lease = _RecordingLease(
        lock=_A5Lock(),  # type: ignore[arg-type]
        owner_token="a5-token",
        operation="init_global_discovery",
    )
    lease.events = []
    lease.release_result = release_result
    lease.release_raises = release_raises
    return lease


class _A5CountingRuntime(_GDRuntime):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bootstrap_calls = 0
        # Blocker 11/15: instrument the states the CLI ACTUALLY consumed
        # (recorded from inside state()) rather than an external pre-call probe.
        self.state_calls = []

    def bootstrap(self):
        self.bootstrap_calls += 1
        return super().bootstrap()

    def state(self, *, generation=None):
        observed = super().state(generation=generation)
        # Record the FULL observation the CLI consumed (blocker 11/C1) so the test
        # can rebuild the exact expected message from the CLI's single call rather
        # than probing state() again — a CLI that calls state twice fails.
        self.state_calls.append(observed)
        return observed


def _expected_init_message(observation, code: str) -> str:
    """Reproduce the CLI's EXACT typed-error message (blocker 12): full-literal
    comparison, no regex or prefix.  Mirrors ``_bootstrap_global_discovery_graph``
    string construction character-for-character from the live observation."""

    # Fixed-literal codes need no observation.
    if code == "global_discovery_init_refused_marker_present":
        return (
            "global_discovery_init_refused: incomplete-bootstrap"
            " marker with a present/partial primary requires the"
            " recovery ceremony before re-running init (zero"
            " mutation)"
        )
    if code == "global_discovery_init_release_failed":
        return (
            "global_discovery_init_release_failed: writer lease release"
            " returned false (fence loss); init fails closed"
        )
    reason = (
        f" reason={observation.reason_code}" if observation.reason_code else ""
    )
    if code == "global_discovery_provider_unavailable":
        return (
            "global_discovery_provider_unavailable: Global Discovery "
            f"runtime is unavailable{reason}; init made zero mutation"
        )
    if code == "global_discovery_init_refused":
        obs_state = observation.state
        return (
            "global_discovery_init_refused: refusing to bootstrap over"
            f" unreadable/residual state={obs_state.value if obs_state else 'unknown'}"
            f"{reason}; resolve interrupted recovery through the recovery"
            " ceremony before re-running init (zero mutation)"
        )
    raise AssertionError(f"no literal template for code {code!r}")


# Windows file-attribute bits (winnt.h).  A REAL reparse point sets the
# REPARSE_POINT bit; ordinary files/dirs have ``st_reparse_tag == 0`` (NOT None),
# so classification MUST key off this attribute bit, never ``st_reparse_tag is
# not None``.  Only stable, classification-relevant bits are recorded — volatile
# flags (ARCHIVE 0x20, NOT_CONTENT_INDEXED 0x2000, …) are masked out.
_FILE_ATTRIBUTE_READONLY = 0x1
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_A5_STABLE_ATTR_MASK = (
    _FILE_ATTRIBUTE_READONLY
    | _FILE_ATTRIBUTE_HIDDEN
    | _FILE_ATTRIBUTE_SYSTEM
    | _FILE_ATTRIBUTE_DIRECTORY
    | _FILE_ATTRIBUTE_REPARSE_POINT
)


def _a5_fingerprint(root: Path) -> tuple:
    """Complete, Windows-stable physical fingerprint (no volatile timestamps):
    lexically-normalized root (NOT symlink-resolved) + exists/absent; every
    file/sidecar/dir/empty dir with its entry kind, lstat mode + masked stable
    Windows attributes, stable identity (dev/ino/nlink), reparse tag + junction
    target WITHOUT following, size + SHA-256 for files, and normalized relative
    path."""

    def _describe(path: Path) -> tuple:
        st = path.lstat()
        raw_attrs = getattr(st, "st_file_attributes", None)
        masked_attrs = (
            (raw_attrs & _A5_STABLE_ATTR_MASK) if raw_attrs is not None else None
        )
        # Stable identity across calls: device + inode + link count. On volumes
        # that don't populate st_ino it degrades to None (still stable).
        identity = (st.st_dev, st.st_ino, st.st_nlink) if st.st_ino else None
        is_reparse = bool(
            raw_attrs is not None and (raw_attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
        )
        # Record the reparse tag ONLY for a real reparse point (else it is 0).
        reparse_tag = getattr(st, "st_reparse_tag", 0) if is_reparse else None
        base = (st.st_mode, masked_attrs, identity, reparse_tag)
        if path.is_symlink():
            return base + ("symlink", _os.readlink(path))
        if is_reparse:
            # Junction / mount point: classified by tag + literal target, NEVER
            # followed (no recursion into the reparse target).
            try:
                target = _os.readlink(path)
            except OSError:
                target = None
            return base + ("reparse", target)
        if path.is_dir():
            return base + ("dir", None)
        if path.is_file():
            return base + (
                "file",
                st.st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return base + ("other", None)

    # Lexical absolute normalization (NOT ``resolve()`` — that would follow the
    # root itself through a symlink/junction).
    root_abs = Path(_os.path.abspath(str(root)))
    if not root_abs.exists():
        return ("absent", str(root_abs))
    # Root metadata is part of the fingerprint (exists + kind + identity).  A
    # reparse/junction/symlink root is described WITHOUT recursing into it.
    def _is_reparse_dir(path: Path) -> bool:
        st = path.lstat()
        raw = getattr(st, "st_file_attributes", None)
        return bool(raw is not None and (raw & _FILE_ATTRIBUTE_REPARSE_POINT))

    def _walk(base: Path):
        # Manual walk that NEVER descends into a symlink or reparse point/junction
        # (unlike ``rglob``, which would follow a junction into its target).
        for child in sorted(base.iterdir(), key=lambda p: p.name):
            yield child
            if (
                child.is_dir()
                and not child.is_symlink()
                and not _is_reparse_dir(child)
            ):
                yield from _walk(child)

    # describe() layout: (st_mode, masked_attrs, identity, reparse_tag, KIND, ...)
    # so with the leading relative-path element the KIND sits at index 5.
    root_describe = _describe(root_abs)
    root_entry = ("", *root_describe)
    if root_describe[4] in ("symlink", "reparse"):
        return ("present", str(root_abs), (root_entry,))
    entries = [root_entry]
    for path in _walk(root_abs):
        rel = path.relative_to(root_abs).as_posix()
        entries.append((rel, *_describe(path)))
    return ("present", str(root_abs), tuple(entries))


def _a5_install(monkeypatch, runtime, *, lease):
    """Wire the CLI to ``runtime`` + ``lease``; record acquire kwargs, the
    require/acquire order, and the exact runtime returned by the registry."""

    rec = {"kwargs": None, "order": [], "runtime": None, "lease": lease}

    def _require():
        rec["order"].append("require_runtime")
        rec["runtime"] = runtime
        return runtime

    monkeypatch.setattr(
        _application_kg,
        "get_current_provider_registry",
        lambda: SimpleNamespace(require_global_discovery_runtime=_require),
    )

    def _fake_acquire(cls, **kwargs):
        rec["order"].append("acquire")
        rec["kwargs"] = dict(kwargs)
        if lease is None:
            raise rec["acquire_error"]
        return lease

    monkeypatch.setattr(_WriterLease, "acquire", classmethod(_fake_acquire))
    return rec


# --- Literal 4-state x 2-invocation matrix -------------------------------

# Per state: two calls, each (observed_state_before, kind, code_or_outcome,
# bootstrap_count_delta).  ``kind`` is "outcome" (typed success string) or
# "error" (GlobalDiscoveryInitError with an exact ``.code``).
_A5_MATRIX = {
    "CONFIRMED_ABSENT": [
        (_ObsState.CONFIRMED_ABSENT, "outcome", "global_discovery_materialized", 1),
        (
            _ObsState.PRESENT_READABLE_CANDIDATE,
            "outcome",
            "global_discovery_already_present",
            0,
        ),
    ],
    "PRESENT_READABLE_CANDIDATE": [
        (
            _ObsState.PRESENT_READABLE_CANDIDATE,
            "outcome",
            "global_discovery_already_present",
            0,
        ),
        (
            _ObsState.PRESENT_READABLE_CANDIDATE,
            "outcome",
            "global_discovery_already_present",
            0,
        ),
    ],
    "PRESENT_UNREADABLE_OR_ERROR": [
        (
            _ObsState.PRESENT_UNREADABLE_OR_ERROR,
            "error",
            "global_discovery_init_refused",
            0,
        ),
        (
            _ObsState.PRESENT_UNREADABLE_OR_ERROR,
            "error",
            "global_discovery_init_refused",
            0,
        ),
    ],
    "PROVIDER_UNAVAILABLE": [
        (
            _ObsState.PROVIDER_UNAVAILABLE,
            "error",
            "global_discovery_provider_unavailable",
            0,
        ),
        (
            _ObsState.PROVIDER_UNAVAILABLE,
            "error",
            "global_discovery_provider_unavailable",
            0,
        ),
    ],
}


@pytest.mark.parametrize("state_name", sorted(_A5_MATRIX))
def test_a5_four_state_two_invocation_matrix(tmp_path, monkeypatch, state_name):
    legacy = tmp_path / "global" / "discovery.lbug"
    root = legacy.parent

    if state_name == "PROVIDER_UNAVAILABLE":
        def _raise() -> Path:
            raise RuntimeError("provider unavailable")

        runtime = _A5CountingRuntime(graph_path_provider=_raise)
        # Blocker 13: a real sentinel tree so the refusal's zero-mutation claim
        # is non-tautological — the fingerprint is a present tree, not "absent".
        root.mkdir(parents=True)
        (root / "sentinel.keep").write_bytes(b"provider-unavailable-sentinel")
        (root / "nested").mkdir()
        (root / "nested" / "leaf.bin").write_bytes(b"leaf")
    else:
        runtime = _A5CountingRuntime(graph_path_provider=lambda: legacy)

    if state_name == "PRESENT_READABLE_CANDIDATE":
        # Blocker 11: build the READABLE state DIRECTLY under a real writer guard
        # (NOT via a hidden third CLI bootstrap), then reset the counters so the
        # matrix observes exactly the two target CLI invocations.
        mat_lease = _make_recording_lease()
        with mat_lease.guard():
            runtime.bootstrap()
        assert runtime.bootstrap_calls == 1
        runtime.bootstrap_calls = 0
        runtime.state_calls = []
    elif state_name == "PRESENT_UNREADABLE_OR_ERROR":
        root.mkdir(parents=True)
        (root / (legacy.name + ".corrupt-residue")).write_bytes(b"x")

    # Blocker 13: PRE-call baseline (before either target invocation).
    baseline = _a5_fingerprint(root)

    fingerprints = []
    for observed, kind, code_or_outcome, count_delta in _A5_MATRIX[state_name]:
        before = runtime.bootstrap_calls
        state_calls_before = len(runtime.state_calls)
        lease = _make_recording_lease()
        _a5_install(monkeypatch, runtime, lease=lease)
        if kind == "outcome":
            result = _cli._bootstrap_global_discovery_graph()
            assert result == code_or_outcome
            # C1: the CLI consumed state() EXACTLY once, and it was the expected
            # observed state — a CLI that probes state twice fails here.
            consumed = runtime.state_calls[state_calls_before:]
            assert len(consumed) == 1
            assert consumed[0].state == observed
        else:
            with pytest.raises(_cli.GlobalDiscoveryInitError) as exc_info:
                _cli._bootstrap_global_discovery_graph()
            exc = exc_info.value
            # C1: exactly one CLI-consumed state(); rebuild the expected message
            # from THAT captured observation — never a second state() probe.
            consumed = runtime.state_calls[state_calls_before:]
            assert len(consumed) == 1
            assert consumed[0].state == observed
            # Blocker 12: exact class, exact code, exact full-literal message.
            assert type(exc) is _cli.GlobalDiscoveryInitError
            assert exc.code == code_or_outcome
            expected = _expected_init_message(consumed[0], code_or_outcome)
            assert str(exc) == expected
        # guard exits on every path; release always runs.
        assert lease.events == ["guard_enter", "guard_exit", "release"]
        assert runtime.bootstrap_calls - before == count_delta
        fingerprints.append(_a5_fingerprint(root))

    # Zero mutation across the two calls (for ABSENT, call-2 no-op preserves the
    # materialized fingerprint; for the others, both calls preserve the initial).
    assert fingerprints[0] == fingerprints[1]
    if state_name == "CONFIRMED_ABSENT":
        # Call-1 materialized: baseline was absent, post-call stable thereafter.
        assert baseline == ("absent", str(root.resolve()))
    else:
        # Zero-mutation states: PRE-call baseline == after-1 == after-2.
        assert baseline == fingerprints[0] == fingerprints[1]


# --- C3: physical fingerprint classification ------------------------------


def test_a5_fingerprint_classifies_file_and_dir(tmp_path):
    """C3: an ordinary file records ('file', size, sha) — NOT misclassified as a
    reparse point via ``st_reparse_tag == 0`` — and an empty dir records ('dir').
    This case never skips (it is not an OS-capability probe)."""

    root = tmp_path / "fproot"
    root.mkdir()
    (root / "ordinary.bin").write_bytes(b"hello-fingerprint")
    (root / "empty").mkdir()

    fp = _a5_fingerprint(root)
    assert fp[0] == "present"
    by_rel = {e[0]: e for e in fp[2]}

    # entry layout: (rel, st_mode, masked_attrs, identity, reparse_tag, KIND, ...)
    file_entry = by_rel["ordinary.bin"]
    assert file_entry[5] == "file"
    assert file_entry[6] == len(b"hello-fingerprint")
    assert file_entry[7] == hashlib.sha256(b"hello-fingerprint").hexdigest()
    # A real file is NOT a reparse point: the reparse-tag slot is None.
    assert file_entry[4] is None

    assert by_rel["empty"][5] == "dir"
    assert by_rel["empty"][4] is None

    # The known empty dir is present and stable across calls.
    assert _a5_fingerprint(root) == fp


def test_a5_fingerprint_symlink_classified_without_following(tmp_path):
    """C3 OS-capability probe: a symlink is classified ('symlink', target) WITHOUT
    following.  Skipped only when the OS forbids unprivileged symlink creation."""

    root = tmp_path / "fproot"
    root.mkdir()
    (root / "ordinary.bin").write_bytes(b"payload-bytes")
    link = root / "link.bin"
    try:
        link.symlink_to(root / "ordinary.bin")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable on this OS/privilege: {exc!r}")

    fp = _a5_fingerprint(root)
    by_rel = {e[0]: e for e in fp[2]}
    link_entry = by_rel["link.bin"]
    assert link_entry[5] == "symlink"
    # Recorded as the link TARGET, never followed into bytes/sha.
    assert "ordinary.bin" in str(link_entry[6])
    assert len(link_entry) == 7  # no size/sha appended for a symlink


@pytest.mark.skipif(_os.name != "nt", reason="Windows junction probe requires nt")
def test_a5_fingerprint_junction_classified_without_following(tmp_path):
    """C3 OS-capability probe: a Windows directory junction (a non-symlink reparse
    point) is classified ('reparse') with a NONZERO reparse tag, WITHOUT following
    or recursing into its target.  Skipped when mklink is unavailable."""

    root = tmp_path / "fproot"
    root.mkdir()
    target = root / "target"
    target.mkdir()
    (target / "inside.txt").write_bytes(b"inside-junction-target")
    junction = root / "junc"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0 or not junction.exists():
        pytest.skip(f"junction unavailable: {made.stderr.strip()!r}")

    fp = _a5_fingerprint(root)
    by_rel = {e[0]: e for e in fp[2]}
    junc_entry = by_rel["junc"]
    assert junc_entry[5] == "reparse"
    # A real reparse point records a NONZERO reparse tag (ordinary entries: None).
    assert junc_entry[4] not in (None, 0)
    # Not recursed: the junction's target contents are NOT re-listed under 'junc'.
    assert "junc/inside.txt" not in by_rel


# --- Marker-variant retry/refusal ----------------------------------------


def test_a5_marker_absent_primary_retries_once_then_noop(tmp_path, monkeypatch):
    legacy = tmp_path / "global" / "discovery.lbug"
    _write_marker(legacy)
    assert not legacy.exists()
    runtime = _A5CountingRuntime(graph_path_provider=lambda: legacy)

    lease1 = _make_recording_lease()
    _a5_install(monkeypatch, runtime, lease=lease1)
    assert _cli._bootstrap_global_discovery_graph() == "global_discovery_materialized"
    assert runtime.bootstrap_calls == 1
    assert _marker_present(legacy) is False
    assert lease1.events == ["guard_enter", "guard_exit", "release"]

    lease2 = _make_recording_lease()
    _a5_install(monkeypatch, runtime, lease=lease2)
    assert _cli._bootstrap_global_discovery_graph() == "global_discovery_already_present"
    assert runtime.bootstrap_calls == 1


def test_a5_marker_partial_primary_ceremony_refused_both_times(tmp_path, monkeypatch):
    legacy = tmp_path / "global" / "discovery.lbug"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"partial-primary")
    _write_marker(legacy)
    runtime = _A5CountingRuntime(graph_path_provider=lambda: legacy)
    fp = _a5_fingerprint(legacy.parent)

    for _ in range(2):
        lease = _make_recording_lease()
        _a5_install(monkeypatch, runtime, lease=lease)
        with pytest.raises(_cli.GlobalDiscoveryInitError) as exc_info:
            _cli._bootstrap_global_discovery_graph()
        # C2: exact class + code + full-literal message (fixed literal).
        assert type(exc_info.value) is _cli.GlobalDiscoveryInitError
        assert exc_info.value.code == "global_discovery_init_refused_marker_present"
        assert str(exc_info.value) == _expected_init_message(
            None, "global_discovery_init_refused_marker_present"
        )
        assert runtime.bootstrap_calls == 0
        assert legacy.read_bytes() == b"partial-primary"
        assert _marker_present(legacy) is True
        assert lease.events == ["guard_enter", "guard_exit", "release"]
        assert _a5_fingerprint(legacy.parent) == fp


# --- Exact lease acquisition + wiring identity (blockers 12/13) -----------


def test_a5_acquire_exact_operation_admin_lane_and_registration_before_acquire(
    tmp_path, monkeypatch
):
    legacy = tmp_path / "global" / "discovery.lbug"
    runtime = _A5CountingRuntime(graph_path_provider=lambda: legacy)
    lease = _make_recording_lease()
    rec = _a5_install(monkeypatch, runtime, lease=lease)

    assert _cli._bootstrap_global_discovery_graph() == "global_discovery_materialized"
    # Exact acquisition kwargs.
    assert rec["kwargs"] == {
        "operation": "init_global_discovery",
        "admin_lane": True,
    }
    # Registration/require happens BEFORE acquire.
    assert rec["order"] == ["require_runtime", "acquire"]
    # The runtime required from the registry is the exact registered object.
    assert rec["runtime"] is runtime
    # ONE object: the acquired lease itself entered guard, exited, and released.
    assert lease.events == ["guard_enter", "guard_exit", "release"]


def test_a5_configure_installs_runtime_causally_then_cli_resolves_it(
    tmp_path, monkeypatch
):
    """C4/Blocker 14: the runtime is installed CAUSALLY through the real
    ``configure_kg_registry`` singleton, and the CLI resolves it through the real
    ``get_current_provider_registry()`` -> ``require_global_discovery_runtime``
    path — NOT a hand-rolled accessor/stub.  Before configuration the registry is
    unavailable (fail-closed), and an empty slot fails closed at the porta."""

    from okto_pulse.core.kg.interfaces.registry import (
        KGProviderRegistry,
        configure_kg_registry,
        reset_registry_for_tests,
    )
    from okto_pulse.core.composition import RuntimeProviderMissing

    legacy = tmp_path / "global" / "discovery.lbug"
    runtime = _A5CountingRuntime(graph_path_provider=lambda: legacy)

    reset_registry_for_tests()
    # Before configuration the singleton is UNAVAILABLE — fail-closed, not a
    # silent None.  The CLI's accessor is the real get_current_provider_registry.
    with pytest.raises(RuntimeError, match="KG registry not configured"):
        _application_kg.get_current_provider_registry()

    # A composed base registry whose Global Discovery slot holds the exact
    # runtime; the other required slots are opaque sentinels (configure only
    # checks presence, not type).
    required_slots = (
        "config",
        "event_bus",
        "audit_repo",
        "graph_store",
        "cypher_executor",
        "graph_transaction",
        "graph_schema_manager",
        "graph_lifecycle",
        "graph_runtime_store",
        "board_source_reader",
    )
    base = KGProviderRegistry(
        global_discovery_runtime=runtime,
        **{slot: object() for slot in required_slots},
    )
    try:
        # CAUSAL install: after this the process singleton IS the base registry.
        configure_kg_registry(base_registry=base)
        installed = _application_kg.get_current_provider_registry()
        assert installed is base
        # The real require path returns the exact installed object.
        assert installed.require_global_discovery_runtime() is runtime

        lease = _make_recording_lease()

        def _fake_acquire(cls, **kwargs):
            assert kwargs == {
                "operation": "init_global_discovery",
                "admin_lane": True,
            }
            return lease

        monkeypatch.setattr(_WriterLease, "acquire", classmethod(_fake_acquire))

        assert (
            _cli._bootstrap_global_discovery_graph()
            == "global_discovery_materialized"
        )
        # The CLI drove bootstrap on the exact registry-installed runtime object.
        assert runtime.bootstrap_calls == 1
        assert lease.events == ["guard_enter", "guard_exit", "release"]
    finally:
        reset_registry_for_tests()

    # Non-tautological: an empty slot fails closed through the same real require.
    empty = KGProviderRegistry()
    with pytest.raises(RuntimeProviderMissing):
        empty.require_global_discovery_runtime()


# --- Unwind matrix (blocker 14) ------------------------------------------


def _a5_acquire_failure_sentinel_tree(root: Path) -> None:
    """A non-trivial pre-existing tree under the KG root so a zero-mutation claim
    is a REAL fingerprint comparison (not just ``not legacy.exists()``)."""

    root.mkdir(parents=True)
    (root / "sentinel.keep").write_bytes(b"acquire-failure-sentinel")
    (root / "empty-dir").mkdir()
    (root / "discovery.lbug.residue-sidecar").write_bytes(b"residue")
    (root / "generations").mkdir()
    (root / "generations" / "active.pointer").write_bytes(b'{"pointer":"x"}')


def test_a5_acquire_contention_zero_bootstrap(tmp_path, monkeypatch):
    legacy = tmp_path / "global" / "discovery.lbug"
    _a5_acquire_failure_sentinel_tree(legacy.parent)
    baseline = _a5_fingerprint(legacy.parent)
    runtime = _A5CountingRuntime(graph_path_provider=lambda: legacy)
    rec = _a5_install(monkeypatch, runtime, lease=None)
    original = _WriterContention(None)
    rec["acquire_error"] = original
    with pytest.raises(_WriterContention) as exc_info:
        _cli._bootstrap_global_discovery_graph()
    # Blocker 15/C5: acquire failure -> zero bootstrap AND zero state() (the CLI
    # never entered the guard); the exact exception object survives; and a FULL
    # physical fingerprint baseline (sentinel/empty dir/sidecar/pointer) is
    # byte-for-byte unchanged.
    assert exc_info.value is original
    assert str(exc_info.value) == str(original)
    assert runtime.bootstrap_calls == 0
    assert runtime.state_calls == []
    assert _a5_fingerprint(legacy.parent) == baseline
    assert not legacy.exists()


def test_a5_acquire_generic_exception_zero_bootstrap(tmp_path, monkeypatch):
    legacy = tmp_path / "global" / "discovery.lbug"
    _a5_acquire_failure_sentinel_tree(legacy.parent)
    baseline = _a5_fingerprint(legacy.parent)
    runtime = _A5CountingRuntime(graph_path_provider=lambda: legacy)
    rec = _a5_install(monkeypatch, runtime, lease=None)
    original = RuntimeError("acquire boom")
    rec["acquire_error"] = original
    with pytest.raises(RuntimeError) as exc_info:
        _cli._bootstrap_global_discovery_graph()
    assert exc_info.value is original
    assert str(exc_info.value) == "acquire boom"
    assert runtime.bootstrap_calls == 0
    assert runtime.state_calls == []
    assert _a5_fingerprint(legacy.parent) == baseline
    assert not legacy.exists()


def test_a5_release_false_after_success_fails_closed(tmp_path, monkeypatch):
    legacy = tmp_path / "global" / "discovery.lbug"
    runtime = _A5CountingRuntime(graph_path_provider=lambda: legacy)
    lease = _make_recording_lease(release_result=False)
    _a5_install(monkeypatch, runtime, lease=lease)
    with pytest.raises(_cli.GlobalDiscoveryInitError) as exc_info:
        _cli._bootstrap_global_discovery_graph()
    # C2: exact class + code + full-literal message (fixed literal).
    assert type(exc_info.value) is _cli.GlobalDiscoveryInitError
    assert exc_info.value.code == "global_discovery_init_release_failed"
    assert str(exc_info.value) == _expected_init_message(
        None, "global_discovery_init_release_failed"
    )
    # Guard exited, release attempted once.
    assert lease.events == ["guard_enter", "guard_exit", "release"]


def test_a5_release_exception_after_success_propagates(tmp_path, monkeypatch):
    legacy = tmp_path / "global" / "discovery.lbug"
    runtime = _A5CountingRuntime(graph_path_provider=lambda: legacy)
    release_error = RuntimeError("release boom")
    lease = _make_recording_lease(release_raises=release_error)
    _a5_install(monkeypatch, runtime, lease=lease)
    with pytest.raises(RuntimeError) as exc_info:
        _cli._bootstrap_global_discovery_graph()
    # C2: the injected release exception propagates by exact object identity.
    assert exc_info.value is release_error
    assert str(exc_info.value) == "release boom"
    assert lease.events == ["guard_enter", "guard_exit", "release"]


def test_a5_release_false_preserves_earlier_guarded_error(tmp_path, monkeypatch):
    # Residue -> refusal inside the guard; release returns False -> the EARLIER
    # refusal is preserved (not replaced by the release-failed error).
    legacy = tmp_path / "global" / "discovery.lbug"
    legacy.parent.mkdir(parents=True)
    (legacy.parent / (legacy.name + ".corrupt-residue")).write_bytes(b"x")
    runtime = _A5CountingRuntime(graph_path_provider=lambda: legacy)
    lease = _make_recording_lease(release_result=False)
    _a5_install(monkeypatch, runtime, lease=lease)
    with pytest.raises(_cli.GlobalDiscoveryInitError) as exc_info:
        _cli._bootstrap_global_discovery_graph()
    # C2: exact class + code + full-literal message built from the CLI-consumed
    # observation (the EARLIER refusal, not the release-failed error).
    assert type(exc_info.value) is _cli.GlobalDiscoveryInitError
    assert exc_info.value.code == "global_discovery_init_refused"
    assert str(exc_info.value) == _expected_init_message(
        runtime.state_calls[-1], "global_discovery_init_refused"
    )
    assert runtime.bootstrap_calls == 0
    assert lease.events == ["guard_enter", "guard_exit", "release"]


def test_a5_release_exception_preserves_earlier_guarded_error(tmp_path, monkeypatch):
    legacy = tmp_path / "global" / "discovery.lbug"
    legacy.parent.mkdir(parents=True)
    (legacy.parent / (legacy.name + ".corrupt-residue")).write_bytes(b"x")
    runtime = _A5CountingRuntime(graph_path_provider=lambda: legacy)
    lease = _make_recording_lease(release_raises=RuntimeError("release boom"))
    _a5_install(monkeypatch, runtime, lease=lease)
    with pytest.raises(_cli.GlobalDiscoveryInitError) as exc_info:
        _cli._bootstrap_global_discovery_graph()
    # C2: the earlier refusal is preserved (class + code + full-literal), not the
    # release error.
    assert type(exc_info.value) is _cli.GlobalDiscoveryInitError
    assert exc_info.value.code == "global_discovery_init_refused"
    assert str(exc_info.value) == _expected_init_message(
        runtime.state_calls[-1], "global_discovery_init_refused"
    )
    assert lease.events == ["guard_enter", "guard_exit", "release"]


class _RaisingStateRuntime(_A5CountingRuntime):
    """Runtime whose state() raises a caller-provided error OBJECT so the test
    can assert the guarded body's original exception survives by identity."""

    def __init__(self, *, error, **kwargs):
        super().__init__(**kwargs)
        self._injected_error = error

    def state(self, *, generation=None):
        self.state_calls.append("raised")
        raise self._injected_error


def test_a5_release_false_preserves_earlier_error_by_identity(tmp_path, monkeypatch):
    # Blocker 16: a PRE-CREATED error object raised inside the guarded body must
    # be preserved by object identity even when release() then fails closed.
    legacy = tmp_path / "global" / "discovery.lbug"
    original_error = RuntimeError("guarded-body original failure")
    runtime = _RaisingStateRuntime(
        error=original_error, graph_path_provider=lambda: legacy
    )
    lease = _make_recording_lease(release_result=False)
    _a5_install(monkeypatch, runtime, lease=lease)
    with pytest.raises(RuntimeError) as exc_info:
        _cli._bootstrap_global_discovery_graph()
    assert exc_info.value is original_error
    assert runtime.bootstrap_calls == 0
    assert lease.events == ["guard_enter", "guard_exit", "release"]


def test_a5_release_exception_preserves_earlier_error_by_identity(
    tmp_path, monkeypatch
):
    legacy = tmp_path / "global" / "discovery.lbug"
    original_error = RuntimeError("guarded-body original failure")
    runtime = _RaisingStateRuntime(
        error=original_error, graph_path_provider=lambda: legacy
    )
    lease = _make_recording_lease(release_raises=RuntimeError("release boom"))
    _a5_install(monkeypatch, runtime, lease=lease)
    with pytest.raises(RuntimeError) as exc_info:
        _cli._bootstrap_global_discovery_graph()
    # The earlier body error wins by identity; the release error is suppressed.
    assert exc_info.value is original_error
    assert lease.events == ["guard_enter", "guard_exit", "release"]


# --- Integrated cmd_init event-order (blocker 15) -------------------------


def _cmd_init_order_harness(
    tmp_path, monkeypatch, events, *, init_db_error=None, gd_bootstrap_error=None
):
    import okto_pulse.core as core
    import okto_pulse.community.adapters.composition as composition
    import okto_pulse.community.adapters.coordination as coordination
    import okto_pulse.community.adapters.kg_shutdown as kg_shutdown
    import okto_pulse.community.adapters.relational_schema_lifecycle as lifecycle
    import okto_pulse.community.adapters.sqlalchemy_database as database
    import okto_pulse.community.auth as community_auth
    import okto_pulse.community.config as community_config
    import okto_pulse.community.main as community_main
    import okto_pulse.community.seed as community_seed
    import okto_pulse.core.services.application_kg as application_kg

    factory = lambda: _FakeSession([])  # noqa: E731

    class Settings:
        def __init__(self):
            self.data_dir = str(tmp_path / "pulse")
            self.upload_dir = str(tmp_path / "pulse" / "uploads")
            self.database_url = "sqlite+aiosqlite:///:memory:"
            self.mcp_port = 8101

    async def fake_init_db():
        events.append("init_db")
        if init_db_error is not None:
            raise init_db_error

    async def fake_close_db():
        events.append("close_db")

    def fake_close_graphs():
        events.append("graph_shutdown")
        return {"boards_closed": 0, "boards_failed": 0, "duration_ms": 0}

    # C6: the registry is INSTALLED CAUSALLY by configure_kg — before it runs,
    # get_current_provider_registry is unavailable, so the CLI cannot resolve a
    # pre-supplied object tautologically.
    registry_holder: dict = {"value": None}

    def fake_configure_kg(received_factory, *, settings):
        events.append("configure_kg")
        registry_holder["value"] = registry

    async def fake_seed(_db):
        events.append("seed")
        return (
            SimpleNamespace(id="board-1", name="My Board"),
            SimpleNamespace(name="Local Agent"),
            "dash_x",
        )

    class _GSM:
        async def ensure_bootstrapped(self, board_id):
            events.append("kg_board_bootstrap")

        async def current_version(self, board_id):
            return 1

    class _GDRuntime:
        def state(self):
            return SimpleNamespace(
                state=_ObsState.CONFIRMED_ABSENT, reason_code=None, details={}
            )

        def bootstrap(self):
            events.append("gd_bootstrap")
            if gd_bootstrap_error is not None:
                raise gd_bootstrap_error

    gd_runtime = _GDRuntime()

    def _require_gd():
        events.append("require_runtime")
        return gd_runtime

    registry = SimpleNamespace(
        graph_schema_manager=_GSM(),
        require_global_discovery_runtime=_require_gd,
        config=SimpleNamespace(kg_base_dir=str(tmp_path / "kgbase")),
    )

    class _Lease:
        @contextmanager
        def guard(self):
            events.append("guard_enter")
            try:
                yield
            finally:
                events.append("guard_exit")

        def release(self):
            events.append("release")
            return True

    probe: dict = {
        "acquire_kwargs": None,
        "acquire_count": 0,
        "lease": None,
        "runtime": gd_runtime,
        "registry": registry,
    }

    def _fake_acquire(cls, **kwargs):
        probe["acquire_count"] += 1
        probe["acquire_kwargs"] = dict(kwargs)
        events.append("acquire")
        lease = _Lease()
        probe["lease"] = lease
        return lease

    def _get_current_provider_registry():
        # Causal: unavailable until configure_kg installs it.
        if registry_holder["value"] is None:
            raise RuntimeError("KG registry not configured")
        return registry_holder["value"]

    monkeypatch.setenv(community_seed.DEMO_SKIP_ENV, "1")
    monkeypatch.setattr(_cli, "_fail_fast_if_server_running", lambda _op: None)
    monkeypatch.setattr(community_config, "CommunitySettings", Settings)
    monkeypatch.setattr(community_main, "_ensure_data_dir", lambda _s: None)
    monkeypatch.setattr(core, "configure_settings", lambda _s: None)
    monkeypatch.setattr(core, "configure_auth", lambda _p: None)
    monkeypatch.setattr(core, "configure_storage", lambda _p: None)
    monkeypatch.setattr(community_auth, "LocalAuthProvider", lambda: object())
    monkeypatch.setattr(composition, "community_storage_provider", lambda _p: None)
    monkeypatch.setattr(
        composition, "configure_community_kg_registry", fake_configure_kg
    )
    monkeypatch.setattr(
        _cli, "_configure_community_relational_runtime", lambda *_a, **_k: None
    )
    monkeypatch.setattr(database, "init_db", fake_init_db)
    monkeypatch.setattr(database, "close_db", fake_close_db)
    monkeypatch.setattr(database, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        kg_shutdown, "close_all_graphs_on_shutdown", fake_close_graphs
    )
    monkeypatch.setattr(community_seed, "seed_community_defaults", fake_seed)
    monkeypatch.setattr(
        lifecycle, "register_community_relational_schema_lifecycle", lambda: None
    )
    monkeypatch.setattr(
        coordination,
        "register_community_coordination_providers",
        lambda: events.append("register_coordination"),
    )
    monkeypatch.setattr(
        application_kg,
        "get_current_provider_registry",
        _get_current_provider_registry,
    )
    monkeypatch.setattr(_WriterLease, "acquire", classmethod(_fake_acquire))
    return probe


# Blocker 17: the COMPLETE, exact cmd_init event trace — no extras, no gaps, no
# duplicates beyond the two deliberate graph_shutdown barriers (in-loop +
# post-async).  Success and global-ceremony-failure share the identical trace:
# the ceremony error propagates only AFTER guard exit, release, and full cleanup.
_A5_CMD_INIT_FULL_TRACE = [
    "init_db",
    "configure_kg",
    "register_coordination",
    "seed",
    "kg_board_bootstrap",
    "require_runtime",
    "acquire",
    "guard_enter",
    "gd_bootstrap",
    "guard_exit",
    "release",
    "graph_shutdown",
    "close_db",
    "graph_shutdown",
]


def test_a5_cmd_init_event_order_success(tmp_path, monkeypatch):
    events: list[str] = []
    probe = _cmd_init_order_harness(tmp_path, monkeypatch, events)
    _cli.cmd_init(SimpleNamespace(mcp_port=8101, agents=None))

    # Exact, complete trace — not a subsequence.
    assert events == _A5_CMD_INIT_FULL_TRACE
    # C6: the causally-installed registry's runtime is the one the CLI bootstrapped
    # (identity), and the writer lease acquire carried the EXACT full kwargs, once.
    assert probe["acquire_count"] == 1
    assert probe["acquire_kwargs"] == {
        "operation": "init_global_discovery",
        "admin_lane": True,
    }
    assert probe["lease"] is not None
    assert probe["registry"].require_global_discovery_runtime() is probe["runtime"]


def test_a5_cmd_init_event_order_global_ceremony_failure(tmp_path, monkeypatch):
    events: list[str] = []
    original = RuntimeError("gd boom")
    _cmd_init_order_harness(
        tmp_path, monkeypatch, events, gd_bootstrap_error=original
    )
    with pytest.raises(RuntimeError) as exc_info:
        _cli.cmd_init(SimpleNamespace(mcp_port=8101, agents=None))

    # Injected error preserved by identity; the trace is identical to success
    # (guard exit + release + full cleanup all run before it surfaces).
    assert exc_info.value is original
    assert events == _A5_CMD_INIT_FULL_TRACE


def test_a5_cmd_init_event_order_partial_init_db_failure(tmp_path, monkeypatch):
    events: list[str] = []
    original = RuntimeError("init boom")
    _cmd_init_order_harness(
        tmp_path, monkeypatch, events, init_db_error=original
    )
    with pytest.raises(RuntimeError) as exc_info:
        _cli.cmd_init(SimpleNamespace(mcp_port=8101, agents=None))

    # Injected error by identity; a partial init_db failure aborts before the
    # graph ceremony (no require_runtime/acquire/seed) yet still runs the exact
    # graph-runtime -> DB -> post-async cleanup barriers, in full.
    assert exc_info.value is original
    assert events == ["init_db", "graph_shutdown", "close_db", "graph_shutdown"]
