"""Test-only filesystem channel to an owned installed runtime's internal control.

Copied into the disposable harness, never packaged or registered as an endpoint.
The parent test supplies the two fixture actor identities; this is deliberately
not authentication coverage. Public MCP absence is checked over real HTTP by
the harness separately. All admission, durable state and physical workers remain
the installed implementations.
"""
from __future__ import annotations

import contextvars
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import traceback


def _read_request_bytes(path: Path, *, max_seconds: float = 5.0) -> bytes:
    """Retry only transient Windows read sharing, strictly before admission."""
    deadline = time.monotonic() + max_seconds
    while True:
        try:
            return path.read_bytes()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def install() -> None:
    from okto_pulse.community.adapters.global_discovery_recovery_worker import (
        CommunityRecoveryRuntime,
    )
    from okto_pulse.core.ports.global_discovery_recovery_control import (
        GlobalDiscoveryRecoveryAdmissionService,
        resolve_global_discovery_recovery_runtime_dependencies,
    )

    root = Path(os.environ['OKTO_E2E_INTERNAL_CONTROL_DIR']).resolve(strict=True)
    actors = frozenset(json.loads(os.environ['OKTO_E2E_INTERNAL_CONTROL_ACTORS']))
    assert len(actors) == 2 and all(isinstance(actor, str) and actor for actor in actors)
    original_start = CommunityRecoveryRuntime.start

    def start(runtime):
        original_start(runtime)
        context = contextvars.copy_context()

        def execute(request):
            actor = request['actor_id']
            assert actor in actors, 'actor outside this fixture'
            operation, arguments = request['operation'], request['arguments']
            recovery, artifact_store = resolve_global_discovery_recovery_runtime_dependencies()
            service = GlobalDiscoveryRecoveryAdmissionService(
                recovery=recovery, artifact_store=artifact_store,
            )
            control = runtime.control
            if operation == 'prepare':
                assert not arguments
                command = service.new_preparation_command(actor_id=actor)
                status = control.prepare(command)
                return {**status.to_dict(), 'outcome': 'preparation_accepted',
                        'idempotent_replay': status.run_id != command.binding.run_id}
            if operation == 'confirm':
                return service.confirm(actor_id=actor, **arguments)
            if operation == 'start':
                command = service.prepare_durable_start(actor_id=actor, **arguments)
                before = control.status(command.binding.run_id)
                status = control.start(command)
                return {**status.to_dict(), 'outcome': 'accepted',
                        'idempotent_replay': before.phase != 'prepared'}
            if operation == 'status':
                return control.status(**arguments).to_dict()
            if operation in {'cancel', 'resume'}:
                return getattr(control, operation)(
                    requested_at=datetime.now(timezone.utc),
                    requested_by_actor_id=actor, **arguments,
                ).to_dict()
            raise AssertionError(f'Unknown internal test operation: {operation}')

        def serve():
            (root / 'ready').write_text('ready', encoding='ascii')
            while not (root / 'stop').exists():
                for path in sorted(root.glob('*.request.json')):
                    response = path.with_name(path.name.replace('.request.json', '.response.json'))
                    if response.exists():
                        continue
                    try:
                        raw = _read_request_bytes(path)
                        assert len(raw) <= 65536
                        result = execute(json.loads(raw))
                        envelope = {'result': result}
                    except Exception as exc:
                        # Preserve typed refusal facts for assertions. Unexpected
                        # failures carry a traceback only in this test channel.
                        if isinstance(getattr(exc, 'code', None), str):
                            result = {'error': exc.code}
                            for key in ('reason', 'run_id', 'epoch', 'expected_epoch',
                                        'actual_epoch', 'expected_progress_seq',
                                        'actual_progress_seq', 'state', 'phase'):
                                if hasattr(exc, key):
                                    result[key] = getattr(exc, key)
                            envelope = {'result': result}
                        else:
                            envelope = {'unexpected_error': traceback.format_exc()}
                    temp = response.with_suffix('.tmp')
                    temp.write_text(json.dumps(envelope, default=str), encoding='utf-8')
                    temp.replace(response)
                time.sleep(0.01)

        threading.Thread(target=context.run, args=(serve,), daemon=True,
                         name='installed-test-internal-control').start()

    CommunityRecoveryRuntime.start = start
