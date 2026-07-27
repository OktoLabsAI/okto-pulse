from __future__ import annotations

import logging

import okto_pulse.community.main as main_module


def test_native_crash_diagnostics_enable_all_threads(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(main_module.faulthandler, "is_enabled", lambda: False)
    monkeypatch.setattr(
        main_module.faulthandler,
        "enable",
        lambda *, all_threads: calls.append(all_threads),
    )

    main_module._enable_native_crash_diagnostics()

    assert calls == [True]


def test_native_crash_diagnostics_are_best_effort(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(main_module.faulthandler, "is_enabled", lambda: False)

    def _unavailable(*, all_threads: bool) -> None:
        assert all_threads is True
        raise RuntimeError("stderr unavailable")

    monkeypatch.setattr(main_module.faulthandler, "enable", _unavailable)

    with caplog.at_level(
        logging.WARNING,
        logger="okto_pulse.community.native_diagnostics",
    ):
        main_module._enable_native_crash_diagnostics()

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", "")
        == "community.native_diagnostics.unavailable"
    ]
    assert len(records) == 1
    assert records[0].error_type == "RuntimeError"
    assert "stderr unavailable" not in caplog.text
