"""Bounded file transport failures must not replay internal admission."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_global_discovery_recovery_installed_e2e as harness


def _client(root):
    return SimpleNamespace(
        actor_id="fixture-actor",
        server=SimpleNamespace(
            control_dir=root,
            process=SimpleNamespace(poll=lambda: None),
            log_tail=lambda: "owned server",
        ),
    )


@pytest.mark.asyncio
async def test_response_sharing_violation_retries_read_without_replaying(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.secrets, "token_hex", lambda _: "request")
    response = tmp_path / "request.response.json"
    response.write_text('{"result":{"run_id":"same-run"}}', encoding="utf-8")
    original_read = Path.read_text
    reads = 0

    def read(path, *args, **kwargs):
        nonlocal reads
        if path == response:
            reads += 1
            if reads == 1:
                raise PermissionError(13, "temporary sharing violation", str(path))
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    result = await harness._internal_payload(_client(tmp_path), "prepare")
    assert result == {"run_id": "same-run"}
    assert reads == 2
    requests = list(tmp_path.glob("*.request.json"))
    assert len(requests) == 1
    assert json.loads(requests[0].read_text())["operation"] == "prepare"


@pytest.mark.asyncio
async def test_persistent_denial_keeps_original_deadline_and_cause(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.secrets, "token_hex", lambda _: "request")
    (tmp_path / "request.response.json").write_text("{}", encoding="utf-8")
    ticks = iter((0, 0, 46))
    monkeypatch.setattr(harness, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    denial = PermissionError(13, "denied")

    def read(*args, **kwargs):
        raise denial

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(AssertionError, match="internal prepare timed out") as error:
        await harness._internal_payload(_client(tmp_path), "prepare")
    assert error.value.__cause__ is denial
    assert len(list(tmp_path.glob("*.request.json"))) == 1


@pytest.mark.asyncio
async def test_invalid_response_is_not_treated_as_transient(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.secrets, "token_hex", lambda _: "request")
    (tmp_path / "request.response.json").write_text("invalid", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        await harness._internal_payload(_client(tmp_path), "prepare")


@pytest.mark.asyncio
async def test_unexpected_runtime_failure_is_not_a_declared_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.secrets, "token_hex", lambda _: "request")
    (tmp_path / "request.response.json").write_text(
        '{"unexpected_error":"unexpected runtime failure"}', encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="unexpected runtime failure"):
        await harness._internal_payload(_client(tmp_path), "start")
