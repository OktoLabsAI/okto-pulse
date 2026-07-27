"""Compatibility exports for the KG live-event coordination service.

Concrete event-store reads live in edition adapters.  New Core code imports
from :mod:`okto_pulse.core.application.kg_events_hub` directly.
"""

from okto_pulse.core.application.kg_events_hub import (
    OUTBOX_BATCH_LIMIT,
    POLL_INTERVAL_SECONDS,
    SUBSCRIBER_QUEUE_MAXSIZE,
    BoardStream,
    KgEventsHub,
    KgEventsSubscription,
    configure_kg_events_hub,
    format_outbox_row_sse,
    format_progress_sse,
    format_sse,
    get_kg_events_hub,
    shutdown_kg_events_hub,
)

_BoardStream = BoardStream

__all__ = [
    "OUTBOX_BATCH_LIMIT",
    "POLL_INTERVAL_SECONDS",
    "SUBSCRIBER_QUEUE_MAXSIZE",
    "KgEventsHub",
    "KgEventsSubscription",
    "_BoardStream",
    "configure_kg_events_hub",
    "format_outbox_row_sse",
    "format_progress_sse",
    "format_sse",
    "get_kg_events_hub",
    "shutdown_kg_events_hub",
]
