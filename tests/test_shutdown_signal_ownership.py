import asyncio
import signal

import pytest


@pytest.mark.asyncio
async def test_sigint_handler_is_owned_and_prior_handler_restored():
    from okto_pulse.community import main

    previous = signal.getsignal(signal.SIGINT)
    calls = []
    cleanups = main._install_shutdown_signal_handlers(calls.append)
    try:
        current = signal.getsignal(signal.SIGINT)
        assert current is not previous
        current(int(signal.SIGINT), None)
        current(int(signal.SIGINT), None)
        await asyncio.sleep(0)
        assert calls == [int(signal.SIGINT), int(signal.SIGINT)]
    finally:
        for cleanup in cleanups:
            cleanup()
    assert signal.getsignal(signal.SIGINT) is previous
