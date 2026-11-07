"""Retain failed shutdown ownership until producers and final cleanup settle."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
import logging


logger = logging.getLogger('okto_pulse.community.shutdown')


@dataclass
class ShutdownOwnership:
    """Host-owned continuation after the bounded shutdown already failed."""

    failed_families: tuple[str, ...]
    deadline_failed: bool = True
    state: str = 'shutdown_incomplete'
    errors: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def start(
        self,
        *,
        observe_producers: Callable[[], Awaitable[None]],
        drain_cleanup: Callable[[], Awaitable[None]],
        close_graph: Callable[[], Awaitable[None]],
        close_database: Callable[[], Awaitable[None]],
    ) -> None:
        if self.task is not None:
            raise RuntimeError('shutdown_continuation_already_started')

        async def settle() -> None:
            # Observe existing ownership only; do not submit processing work,
            # renew a failed deadline, or issue a second cancellation request.
            try:
                await observe_producers()
            except BaseException as exc:
                self.errors.append(type(exc).__name__)
                self.state = 'shutdown_owner_observation_failed'
                logger.critical(
                    'community.shutdown.owner_observation_failed families=%s error_type=%s',
                    ','.join(self.failed_families), type(exc).__name__,
                )
                # No quiescence proof means no resource disposal or Runner exit.
                # This wait is event-based, not polling. Only explicit external
                # abnormal termination can stop an unobservable native owner.
                await asyncio.Event().wait()
                return
            self.state = 'draining_final_cleanup'
            try:
                await drain_cleanup()
            except BaseException as exc:
                self.errors.append(type(exc).__name__)
                self.state = 'shutdown_cleanup_observation_failed'
                logger.critical(
                    'community.shutdown.cleanup_observation_failed error_type=%s',
                    type(exc).__name__,
                )
                await asyncio.Event().wait()
                return
            self.state = 'closing_resources_after_failed_deadline'
            # A cancellation of this coordinator must not cancel the native
            # graph-close awaitable and enter DB disposal while its thread
            # remains live. Own each close phase and observe it to terminal.
            try:
                await _observe_owned_phase(close_graph(), 'community.shutdown.graph_close')
            except asyncio.CancelledError:
                self.errors.append('graph_close_cancelled_without_terminal_proof')
                self.state = 'shutdown_graph_close_unproven'
                await asyncio.Event().wait()
                return
            except Exception as exc:
                self.errors.append(type(exc).__name__)
            try:
                await _observe_owned_phase(close_database(), 'community.shutdown.database_close')
            except asyncio.CancelledError:
                self.errors.append('database_close_cancelled_without_terminal_proof')
                self.state = 'shutdown_database_close_unproven'
                await asyncio.Event().wait()
                return
            except Exception as exc:
                self.errors.append(type(exc).__name__)
            self.state = 'settled_after_failed_deadline'
            logger.warning(
                'community.shutdown.settled_after_failed_deadline families=%s errors=%d',
                ','.join(self.failed_families), len(self.errors),
            )

        self.task = asyncio.create_task(settle(), name='community.shutdown.retained_ownership')

    async def wait_before_runner_exit(self) -> None:
        """Hold host ownership through repeated cancellation of the host."""
        task = self.task
        if task is None:
            raise RuntimeError('shutdown_continuation_not_started')
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Ctrl+C on the host is not permission to cut a commit/cleanup
                # in half. Runner has not received control, so it cannot cancel
                # these child tasks directly while this host still waits.
                continue
        task.result()


async def _observe_owned_phase(operation: Awaitable[None], name: str) -> None:
    phase = asyncio.create_task(operation, name=name)
    while not phase.done():
        try:
            await asyncio.shield(phase)
        except asyncio.CancelledError:
            # Repeated cancellation of the coordinator is not proof that its
            # native phase stopped. A directly cancelled phase fails closed.
            continue
    phase.result()


async def observe_existing_tasks(tasks: Iterable[asyncio.Task[object]]) -> None:
    """Adapter helper: wait existing tasks without propagating cancellation."""
    owned = frozenset(tasks)
    pending = {task for task in owned if not task.done()}
    if pending:
        await asyncio.wait(pending)
    for task in owned:
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.error('community.shutdown.producer_terminal_error error_type=%s', type(error).__name__)


async def observe_cleanup_until_empty(
    snapshot: Callable[[], Iterable[asyncio.Task[object]]],
) -> None:
    """Only valid after every possible producer has been proven terminal."""
    empty_turns = 0
    while True:
        tasks = {task for task in snapshot() if not task.done()}
        if not tasks:
            empty_turns += 1
            if empty_turns == 2:
                return
            await asyncio.sleep(0)
            continue
        empty_turns = 0
        await observe_existing_tasks(tasks)


class ShutdownDeadlineFailed(RuntimeError):
    """Ownership settled eventually; the original shutdown deadline failed."""
