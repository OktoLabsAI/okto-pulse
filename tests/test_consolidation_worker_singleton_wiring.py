"""Regressão: o worker de consolidação iniciado pelo lifespan É o singleton.

Bug corrigido em 2026-07-10 (hotfix direto, fora do fluxo do board): o
combined_lifespan iniciava um ConsolidationWorker AVULSO via
create_consolidation_worker(), enquanto queue_health (worker_mode),
kg_health (dlq drain stats), o process_now do dead_letter_reprocess e
signal_consolidation_worker() leem o singleton de get_consolidation_worker().
Resultado: worker_mode="stopped" permanente no drilldown, métricas lidas de
uma instância nunca iniciada e — pior — o wake signal dos enqueue sites era
no-op, fazendo a fila andar apenas no heartbeat (30s) em vez de imediatamente.
"""

from __future__ import annotations

import asyncio

import pytest

from okto_pulse.core.kg.workers import consolidation as consolidation_mod
from okto_pulse.core.kg.workers.consolidation import (
    ConsolidationWorker,
    get_consolidation_worker,
    reset_consolidation_worker_for_tests,
    signal_consolidation_worker,
)

from okto_pulse.community.adapters.workers import _start_consolidation_worker


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_consolidation_worker_for_tests()
    yield
    reset_consolidation_worker_for_tests()


class _DummySessionFactory:
    def __call__(self, *args, **kwargs):  # pragma: no cover - nunca invocado
        raise AssertionError("session factory não deve ser usada neste teste")


def test_lifespan_started_worker_is_the_singleton(monkeypatch):
    """O worker retornado pelo start do lifespan é o mesmo objeto que
    get_consolidation_worker() — a fonte lida por worker_mode/signal."""
    started = []

    async def _fake_start(self):
        started.append(self)
        self._running = True  # espelha o efeito observável de start()

    monkeypatch.setattr(ConsolidationWorker, "start", _fake_start)

    factory = _DummySessionFactory()
    worker = asyncio.run(_start_consolidation_worker(factory))

    assert started == [worker], "start() deve ter sido chamado no worker do lifespan"
    singleton = get_consolidation_worker()
    assert worker is singleton, (
        "regressão do split-brain: o worker iniciado pelo lifespan não é o "
        "singleton — worker_mode voltará a reportar 'stopped' e o wake signal "
        "voltará a ser no-op"
    )


def test_signal_reaches_the_started_worker(monkeypatch):
    """signal_consolidation_worker() acorda o worker iniciado pelo lifespan."""
    signals = []

    async def _fake_start(self):
        self._running = True

    monkeypatch.setattr(ConsolidationWorker, "start", _fake_start)

    worker = asyncio.run(_start_consolidation_worker(_DummySessionFactory()))

    # is_running deve refletir o start (propriedade ou atributo interno)
    if not getattr(worker, "is_running", False):
        monkeypatch.setattr(
            type(worker), "is_running", property(lambda self: True)
        )

    monkeypatch.setattr(
        type(worker), "signal_new_work", lambda self: signals.append(self)
    )

    signal_consolidation_worker()

    assert signals == [worker], (
        "o wake signal dos enqueue sites deve alcançar o worker REAL iniciado "
        "pelo lifespan (antes do fix ia para um singleton nunca iniciado)"
    )


def test_module_singleton_slot_is_populated_by_lifespan_start(monkeypatch):
    """O slot _singleton do módulo é preenchido pelo start do lifespan (sem
    criação preguiçosa posterior de uma segunda instância)."""

    async def _fake_start(self):
        self._running = True

    monkeypatch.setattr(ConsolidationWorker, "start", _fake_start)

    worker = asyncio.run(_start_consolidation_worker(_DummySessionFactory()))
    assert consolidation_mod._singleton is worker
