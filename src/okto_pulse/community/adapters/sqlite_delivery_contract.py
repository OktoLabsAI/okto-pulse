"""SQLite projection of the public, exact delivery-attempt contract."""

from __future__ import annotations

import json
from types import SimpleNamespace

from okto_pulse.core.ports.delivery_ledger import (
    DeliveryAttemptContractError,
    parse_delivery_attempt_event,
)


def current_delivery_identity(event_id, board_id, session_id, event_type, payload):
    """Return an identity only for a complete current envelope; never infer one."""
    try:
        envelope = parse_delivery_attempt_event(SimpleNamespace(
            event_id=event_id, board_id=board_id, session_id=session_id,
            event_type=event_type, payload=json.loads(payload),
        ))
    except (DeliveryAttemptContractError, TypeError, ValueError):
        return None
    return envelope.delivery_key if envelope is not None else None


def install_delivery_contract_function(connection) -> None:
    connection.create_function(
        "pulse_current_delivery_identity", 5, current_delivery_identity,
        deterministic=True,
    )
