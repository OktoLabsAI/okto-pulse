from __future__ import annotations

import asyncio
import logging

from okto_pulse.community.main import _metrics_publish_cycle
from okto_pulse.core.infra.config import CoreSettings


def test_scheduler_cycle_publishes_delta_and_product_snapshot() -> None:
    calls: list[str] = []

    class Sender:
        def send_pending(self):
            calls.append("delta")
            return {"sent": True, "batch_seq": 4}

        def publish_product_snapshot(self):
            calls.append("product_snapshot")
            return {"sent": True, "batch_seq": 2}

    delta, snapshot = asyncio.run(
        _metrics_publish_cycle(CoreSettings(), sender=Sender())
    )

    assert calls == ["delta", "product_snapshot"]
    assert delta == {"sent": True, "batch_seq": 4}
    assert snapshot == {"sent": True, "batch_seq": 2}


def test_scheduler_cycle_runs_snapshot_when_delta_raises(caplog) -> None:
    calls: list[str] = []

    class Sender:
        def send_pending(self):
            calls.append("delta")
            raise RuntimeError("must-not-reach-logs")

        def publish_product_snapshot(self):
            calls.append("product_snapshot")
            return {"sent": True, "batch_seq": 9}

    caplog.set_level(logging.WARNING, logger="okto_pulse.community.metrics")
    delta, snapshot = asyncio.run(
        _metrics_publish_cycle(CoreSettings(), sender=Sender())
    )

    assert calls == ["delta", "product_snapshot"]
    assert delta == {
        "sent": False,
        "reason": "unhandled_exception",
        "error_class": "RuntimeError",
    }
    assert snapshot == {"sent": True, "batch_seq": 9}
    assert "must-not-reach-logs" not in caplog.text


def test_scheduler_cycle_preserves_delta_when_snapshot_raises(caplog) -> None:
    calls: list[str] = []

    class Sender:
        def send_pending(self):
            calls.append("delta")
            return {"sent": True, "batch_seq": 7}

        def publish_product_snapshot(self):
            calls.append("product_snapshot")
            raise ValueError("sensitive-payload")

    caplog.set_level(logging.WARNING, logger="okto_pulse.community.metrics")
    delta, snapshot = asyncio.run(
        _metrics_publish_cycle(CoreSettings(), sender=Sender())
    )

    assert calls == ["delta", "product_snapshot"]
    assert delta == {"sent": True, "batch_seq": 7}
    assert snapshot == {
        "sent": False,
        "reason": "unhandled_exception",
        "error_class": "ValueError",
    }
    assert "sensitive-payload" not in caplog.text
