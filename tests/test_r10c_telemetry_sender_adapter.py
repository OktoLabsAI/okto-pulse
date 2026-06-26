"""R10-C (Community side) — telemetry beacon SENDER adapter (TelemetrySink).
(Updated for R10-E Pass 2: TelemetryBeaconSender removed from core.)

  ts_312bfd67 (TS02) — CommunityTelemetryBeaconSender isinstance TelemetrySink +
        EXERCISES send_pending() through a fake handshake/usage session; the
        transport methods are defined STANDALONE on the Community class (R10-E
        PASS 1 absorbed them — no inheritance from any core base), byte-identical
        protocol.
  ts_b2a15459 (TS03) — the reason-code matrix (success / 5xx / INVALID_SIGNATURE /
        DUPLICATE / TOKEN_EXPIRED / UNKNOWN_INSTALL) is PRESERVED on
        CommunityTelemetryBeaconSender standalone + secret-free.
  ts_60f2c35c (TS04) — watermark/events-not-lost on UNKNOWN_INSTALL rehandshake.
  ts_6f6c03ba (TS05) — the beacon lifecycle resolves the REGISTERED sender.
  ts_ac2738c0 (TS06) — the product snapshot is built via the R10-D
        ProductAggregationPort + the R10-B EventStore (no concrete bypass).
  + composed-path (real composition root) + anti-claim-guard (all sender files).
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from okto_pulse.community.adapters.telemetry_sender import (
    CommunityTelemetryBeaconSender,
    build_community_telemetry_sender,
    register_community_telemetry_sender,
)
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.ports.telemetry import TelemetrySink
from okto_pulse.core.telemetry import failure_state as fs
from okto_pulse.core.telemetry import sender_registry as registry
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION
from okto_pulse.core.telemetry.sender_registry import (
    get_telemetry_sender,
    reset_telemetry_sender_factory_for_tests,
)
from okto_pulse.core.telemetry.service import TelemetryService

FIXED_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_factory():
    from okto_pulse.core.telemetry.event_store_registry import reset_telemetry_event_store_factory_for_tests
    reset_telemetry_sender_factory_for_tests()
    reset_telemetry_event_store_factory_for_tests()
    try:
        yield
    finally:
        reset_telemetry_sender_factory_for_tests()
        reset_telemetry_event_store_factory_for_tests()


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class FakeSession:
    def __init__(self, *, handshake: FakeResponse | None = None, usage: FakeResponse | None = None):
        self._handshake = handshake
        self._usage = usage
        self.calls: list[str] = []

    def post(self, url, *args, **kwargs):
        self.calls.append(url)
        if url.endswith("/v1/handshake"):
            assert self._handshake is not None, "unexpected handshake call"
            return self._handshake
        if url.endswith("/v1/usage"):
            assert self._usage is not None, "unexpected usage call"
            return self._usage
        raise AssertionError(f"unexpected url {url}")


def _prepare(tmp_path: Path, monkeypatch, sub: str, *, install_token: str, expires_in_hours: float) -> CoreSettings:
    import okto_pulse.community.adapters.telemetry_sender as _community_sender_mod
    from okto_pulse.community.adapters.telemetry_store import register_community_telemetry_event_store
    from okto_pulse.core.telemetry.event_store_registry import reset_telemetry_event_store_factory_for_tests

    # R10-E Pass 2: core sender module is now a stub (no _utcnow). Only patch the
    # community sender module's time helpers so tests are deterministic.
    monkeypatch.setattr(_community_sender_mod, "_utcnow", lambda: FIXED_NOW)
    monkeypatch.setattr(_community_sender_mod, "_backoff_jitter", lambda: 0.0)
    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / sub / "install_id"))

    # R10-E Pass 2: event store registry is fail-closed; register community store so
    # TelemetryService.record_event() can persist the test seed event.
    reset_telemetry_event_store_factory_for_tests()
    register_community_telemetry_event_store()

    settings = CoreSettings(metrics_dir=str(tmp_path / sub / "metrics"), metrics_mode="")
    service = TelemetryService(settings)
    service.update_settings(
        mode="anonymous_beacon", source="cli",
        policy_version="2026-05-11", schema_version=CURRENT_SCHEMA_VERSION,
    )
    state_path = tmp_path / sub / "metrics" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["install_token"] = install_token
    state["install_token_expires_at"] = _iso(FIXED_NOW + timedelta(hours=expires_in_hours))
    state["next_batch_seq"] = 5
    state_path.write_text(json.dumps(state), encoding="utf-8")
    service.record_event("cli", {"command": "serve"})
    return settings


def _state(settings: CoreSettings) -> dict:
    return json.loads((Path(settings.metrics_dir) / "state.json").read_text(encoding="utf-8"))


# ===========================================================================
# ts_312bfd67 (TS02) — conformance + exercise + standalone transport.
# ===========================================================================
def test_ts_312bfd67_conformance_and_handshake_usage(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="okto_pulse.telemetry.sender")
    settings = _prepare(tmp_path, monkeypatch, "c", install_token="old-token", expires_in_hours=1)
    sender = build_community_telemetry_sender(settings)
    assert isinstance(sender, CommunityTelemetryBeaconSender)
    assert isinstance(sender, TelemetrySink)

    session = FakeSession(
        handshake=FakeResponse(200, {"install_token": "fresh-token", "token_ttl_seconds": 2592000,
                                     "accepted_schema_version": CURRENT_SCHEMA_VERSION}),
        usage=FakeResponse(200, {}),
    )
    sender = CommunityTelemetryBeaconSender(settings, session=session)  # type: ignore[arg-type]
    result = sender.send_pending()

    assert result["sent"] is True
    assert session.calls == [
        f"{settings.metrics_beacon_url.rstrip('/')}/v1/handshake",
        f"{settings.metrics_beacon_url.rstrip('/')}/v1/usage",
    ]
    assert _state(settings)["install_token"] == "fresh-token"
    # secret-free logs.
    blob = "\n".join(r.getMessage() + json.dumps(r.__dict__, default=str) for r in caplog.records)
    assert "fresh-token" not in blob and "old-token" not in blob


def test_transport_methods_standalone_conformance(tmp_path, monkeypatch):
    """R10-E Pass 2: TelemetryBeaconSender removed from core. All transport methods
    are defined directly on CommunityTelemetryBeaconSender (standalone, no inheritance)."""
    for method_name in ("send_once", "handshake", "hourly_batch", "publish_product_snapshot"):
        method = getattr(CommunityTelemetryBeaconSender, method_name, None)
        assert method is not None, f"missing {method_name}"
        # Defined directly on Community — owned, not inherited from some core class.
        assert method_name in CommunityTelemetryBeaconSender.__dict__, (
            f"{method_name} must be defined in Community __dict__, not inherited"
        )
    # _sign_and_post_usage is owned by Community (absorbed).
    assert "_sign_and_post_usage" in CommunityTelemetryBeaconSender.__dict__
    # send_pending delegates to send_once (TelemetrySink contract).
    src = inspect.getsource(CommunityTelemetryBeaconSender.send_pending)
    assert "self.send_once()" in src

    # R10-E Pass 2: Community no longer inherits from any core concrete.
    # (TelemetryBeaconSender deleted — no class to inherit from.)
    assert CommunityTelemetryBeaconSender.__bases__ == (object,), (
        "CommunityTelemetryBeaconSender must be a standalone class with no core base"
    )


# ===========================================================================
# ts_b2a15459 (TS03) — reason-code matrix preserved on Community standalone.
# ===========================================================================
def test_ts_b2a15459_reason_code_matrix_preserved(tmp_path, monkeypatch):
    """R10-E Pass 2: exercises reason-code matrix directly on
    CommunityTelemetryBeaconSender standalone (core base removed)."""
    import requests as _requests

    from okto_pulse.community.adapters.telemetry_sender import CommunityTelemetryBeaconSender

    def settings_ready(sub):
        return _prepare(tmp_path, monkeypatch, sub, install_token="tok", expires_in_hours=72)

    def state_of(settings):
        return _state(settings)

    # disabled: no beacon mode configured.
    from okto_pulse.core.infra.config import CoreSettings as _CS
    _s_disabled = _CS(metrics_dir=str(tmp_path / "disabled" / "metrics"), metrics_mode="")
    result = CommunityTelemetryBeaconSender(_s_disabled).send_pending()
    assert result == {"sent": False, "reason": "not_enabled"}

    # empty: all events confirmed, nothing pending.
    _s_empty = settings_ready("empty")
    CommunityTelemetryBeaconSender(_s_empty, session=FakeSession(usage=FakeResponse(200, {}))).send_pending()
    result = CommunityTelemetryBeaconSender(_s_empty).send_pending()
    assert result == {"sent": False, "reason": "empty"}

    # success (2xx).
    _s_ok = settings_ready("success")
    result = CommunityTelemetryBeaconSender(
        _s_ok, session=FakeSession(usage=FakeResponse(200, {}))
    ).send_pending()
    assert result["sent"] is True
    assert fs.read_failure_state(state_of(_s_ok)).status == fs.STATUS_OK
    assert "install_token" not in fs.public_status_projection(state_of(_s_ok))

    # 5xx (retryable, DEGRADED).
    _s_5xx = settings_ready("fivexx")
    result = CommunityTelemetryBeaconSender(
        _s_5xx, session=FakeSession(usage=FakeResponse(503))
    ).send_pending()
    assert result == {"sent": False, "reason": "retryable"}
    _fstate = fs.read_failure_state(state_of(_s_5xx))
    assert _fstate.status == fs.STATUS_DEGRADED
    assert _fstate.http_status == 503
    assert "install_token" not in fs.public_status_projection(state_of(_s_5xx))

    # transport network failure (RequestException).
    class _NetFail:
        def post(self, *a, **k):
            raise _requests.RequestException("timeout")

    _s_net = settings_ready("transport")
    result = CommunityTelemetryBeaconSender(_s_net, session=_NetFail()).send_pending()
    assert result == {"sent": False, "reason": "network"}
    assert fs.read_failure_state(state_of(_s_net)).status == fs.STATUS_DEGRADED

    # TOKEN_EXPIRED (recoverable, DEGRADED).
    _s_te = settings_ready("token_expired")
    result = CommunityTelemetryBeaconSender(
        _s_te, session=FakeSession(usage=FakeResponse(401, {"code": "TOKEN_EXPIRED"}))
    ).send_pending()
    assert result == {"sent": False, "reason": "token_expired"}
    _fstate_te = fs.read_failure_state(state_of(_s_te))
    assert _fstate_te.reason_code == "TOKEN_EXPIRED"
    assert _fstate_te.status == fs.STATUS_DEGRADED
    assert "install_token" not in fs.public_status_projection(state_of(_s_te))

    # INVALID_SIGNATURE (fatal).
    _s_is = settings_ready("invalid_sig")
    result = CommunityTelemetryBeaconSender(
        _s_is, session=FakeSession(usage=FakeResponse(401, {"code": "INVALID_SIGNATURE"}))
    ).send_pending()
    assert result == {"sent": False, "reason": "invalid_signature"}
    _fstate_is = fs.read_failure_state(state_of(_s_is))
    assert _fstate_is.status == fs.STATUS_FATAL
    assert _fstate_is.reason_code == "INVALID_SIGNATURE"
    assert "install_token" not in fs.public_status_projection(state_of(_s_is))

    # DUPLICATE (idempotent confirmation, OK, no fatal).
    _s_dup = settings_ready("duplicate")
    result = CommunityTelemetryBeaconSender(
        _s_dup,
        session=FakeSession(usage=FakeResponse(409, {"code": "DUPLICATE_NONCE_OR_BATCH_SEQ"})),
    ).send_pending()
    assert result["reason"] == "duplicate"
    assert fs.read_failure_state(state_of(_s_dup)).status == fs.STATUS_OK

    # UNKNOWN_INSTALL with handshake failing → rehandshake_failed (DEGRADED).
    _s_ui = settings_ready("unknown_install")
    result = CommunityTelemetryBeaconSender(
        _s_ui,
        session=FakeSession(
            handshake=FakeResponse(503),
            usage=FakeResponse(401, {"code": "UNKNOWN_INSTALL"}),
        ),
    ).send_pending()
    assert result == {"sent": False, "reason": "rehandshake_failed"}
    assert fs.read_failure_state(state_of(_s_ui)).status == fs.STATUS_DEGRADED
    assert "install_token" not in fs.public_status_projection(state_of(_s_ui))


# ===========================================================================
# ts_60f2c35c (TS04) — watermark/events-not-lost on UNKNOWN_INSTALL rehandshake.
# (R10-E Pass 2: parity-against-core-base removed; Community is the canonical impl.)
# ===========================================================================
def test_ts_60f2c35c_unknown_install_rehandshake_events_not_lost(tmp_path, monkeypatch):
    """UNKNOWN_INSTALL → rehandshake (bounded); events not lost."""
    def session():
        return FakeSession(
            handshake=FakeResponse(200, {"install_token": "rehand", "token_ttl_seconds": 2592000,
                                         "accepted_schema_version": CURRENT_SCHEMA_VERSION}),
            usage=FakeResponse(401, {"code": "UNKNOWN_INSTALL"}),
        )

    comm_settings = _prepare(tmp_path, monkeypatch, "uki_comm", install_token="tok", expires_in_hours=72)

    comm_result = CommunityTelemetryBeaconSender(comm_settings, session=session()).send_pending()
    assert comm_result["sent"] is False
    assert comm_result["reason"] in ("rehandshake_failed", "unknown_install_unresolved")
    # events not lost: the local store still holds the pending event after a
    # non-confirming response.
    comm_events = list(
        CommunityTelemetryBeaconSender(comm_settings)._store().iter_events()
    )
    assert len(comm_events) >= 1


# ===========================================================================
# ts_6f6c03ba (TS05) — the lifecycle resolves the REGISTERED sender.
# ===========================================================================
def test_ts_6f6c03ba_loop_resolves_registered_sender(tmp_path, monkeypatch):
    reset_telemetry_sender_factory_for_tests()
    register_community_telemetry_sender()
    settings = CoreSettings(metrics_dir=str(tmp_path / "m"), metrics_mode="")
    resolved = get_telemetry_sender(settings)
    assert isinstance(resolved, CommunityTelemetryBeaconSender)

    # the beacon loop resolves via the registry (not a concrete import).
    import okto_pulse.community.main as main_mod
    loop_src = inspect.getsource(main_mod._metrics_beacon_loop)
    assert "get_telemetry_sender" in loop_src
    assert ".send_pending" in loop_src
    assert "TelemetryBeaconSender(settings)" not in loop_src


# ===========================================================================
# ts_ac2738c0 (TS06) — product snapshot via R10-D/R10-B registries (no bypass).
# ===========================================================================
def test_ts_ac2738c0_product_snapshot_via_registries_no_bypass(tmp_path, monkeypatch):
    from okto_pulse.core.ports.telemetry import ProductState
    from okto_pulse.core.telemetry.product_aggregator_registry import (
        register_product_aggregator_factory,
        reset_product_aggregator_factory_for_tests,
    )

    monkeypatch.setenv("OKTO_PULSE_INSTALL_ID_PATH", str(tmp_path / "install_id"))
    settings = CoreSettings(metrics_dir=str(tmp_path / "metrics"), metrics_mode="anonymous_beacon")

    calls = {"n": 0}

    class _Agg:
        def __init__(self, settings, metrics_dir):
            ...

        def aggregate(self) -> ProductState:
            calls["n"] += 1
            return ProductState.from_dict({"product_feature_usage_counts": {"x": 1}})

    reset_product_aggregator_factory_for_tests()
    register_product_aggregator_factory(lambda s, md: _Agg(s, md))
    try:
        snapshot = CommunityTelemetryBeaconSender(settings).build_product_snapshot()
    finally:
        reset_product_aggregator_factory_for_tests()

    # the sender consumed the REGISTERED ProductAggregationPort (no concrete bypass).
    assert calls["n"] >= 1
    assert snapshot is not None
    assert snapshot["metrics"] == {"product_feature_usage_counts": {"x": 1}}

    # Community sender uses the registries (not direct concrete imports).
    community_src = inspect.getsource(CommunityTelemetryBeaconSender)
    assert "get_telemetry_event_store" in community_src
    assert "get_product_aggregator" in community_src
    assert "LocalTelemetryStore(" not in community_src
    assert "ProductTelemetryAggregator(" not in community_src


def test_composed_root_registers_sender(tmp_path, monkeypatch):
    import okto_pulse.core.infra.config as _config
    import okto_pulse.core.kg.interfaces.registry as _reg
    from okto_pulse.community.adapters.composition import configure_community_kg_registry

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KG_BASE_DIR", str(tmp_path / "boards"))
    saved_settings = _config._settings_instance
    saved_reg = (_reg._registry, _reg._configured)
    _config.configure_settings(CoreSettings())
    _reg.reset_registry_for_tests()
    try:
        reset_telemetry_sender_factory_for_tests()
        assert registry._telemetry_sender_factory is None
        configure_community_kg_registry(None)
        resolved = get_telemetry_sender(CoreSettings(metrics_dir=str(tmp_path / "m")))
        # R10-E: bind at assertion time (robust to sys.modules purges; isinstance stays strict).
        from okto_pulse.community.adapters.telemetry_sender import CommunityTelemetryBeaconSender
        assert isinstance(resolved, CommunityTelemetryBeaconSender)
    finally:
        reset_telemetry_sender_factory_for_tests()
        _config._settings_instance = saved_settings
        _reg._registry, _reg._configured = saved_reg


# ===========================================================================
# anti-claim-guard — extended to the sender domain (present/past/gerund).
# ===========================================================================
_FALSE_MOVE_PATTERNS = (
    r"\bmoves to the community",
    r"\bmoving to the community",
    r"has moved",
    r"have moved",
    r"has been moved",
    r"\bmoved to the community",
    r"concrete\s+\w+\s+moved",
    r"moves out of",
)


def test_guard_no_false_move_claims_in_sender_files():
    """R10-E Pass 2: TelemetryBeaconSender removed from core. Community IS the sole
    authoritative sender. Anti-claim guard ensures no stale move-claim language."""
    import okto_pulse.community.adapters.telemetry_sender as _c
    import okto_pulse.core.application.boundary.telemetry_sender_ownership_gate as _g
    import okto_pulse.core.telemetry.sender_registry as _r

    pats = [re.compile(p, re.IGNORECASE) for p in _FALSE_MOVE_PATTERNS]
    offenders: dict[str, list[str]] = {}
    for mod in (_r, _g, _c):
        text = Path(mod.__file__).read_text(encoding="utf-8")
        hits = [p.pattern for p in pats if p.search(text)]
        if hits:
            offenders[Path(mod.__file__).name] = hits
    assert offenders == {}, offenders
    community_text = Path(_c.__file__).read_text(encoding="utf-8")
    # Post-absorb: Community OWNS (not stays-as-shim).
    assert "Community edition OWNS" in community_text, "expected Community ownership framing"
    # R10-E Pass 2: the core concrete is REMOVED — no "still a shim / pending Pass 2"
    # framing may survive (the anti-claim guard rejects the stale-shim vocabulary).
    for _stale in ("STAYS in core", "shim is still", "shim remains", "remains until PASS 2",
                   "stays as shim", "pending R10-E", "is non-destructive"):
        assert _stale.lower() not in community_text.lower(), (
            f"stale shim claim must not be present: {_stale!r}"
        )

    # R10-E Pass 2: Community class is standalone (no core base to inherit from).
    assert CommunityTelemetryBeaconSender.__bases__ == (object,), (
        "CommunityTelemetryBeaconSender must be a standalone class with no core base"
    )

    # teeth: present/past/gerund full-move claims are all caught.
    for synthetic in (
        "the sender moves to the Community adapter",
        "the sender moving to the Community adapter",
        "the concrete sender has moved to the Community",
    ):
        assert any(p.search(synthetic) for p in pats), synthetic
