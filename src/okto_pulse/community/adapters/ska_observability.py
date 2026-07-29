"""Community timing wrapper for bounded SK-A relational projections."""

from __future__ import annotations

from functools import wraps
from time import perf_counter
from typing import Any

from okto_pulse.core.services.ska_observability import (
    observe_ska_projection_queries,
)


def observed_ska_projection(
    *,
    surface: str,
    subject_type: str,
    query_count: int,
):
    """Measure one fixed-query adapter method without adding dynamic labels."""

    def decorate(method):
        @wraps(method)
        async def observed(*args: Any, **kwargs: Any):
            started = perf_counter()
            try:
                result = await method(*args, **kwargs)
            except Exception:
                observe_ska_projection_queries(
                    surface=surface,
                    subject_type=subject_type,
                    query_count=query_count,
                    duration_ms=(perf_counter() - started) * 1000,
                    payload_bytes=0,
                    outcome="error",
                )
                raise
            observe_ska_projection_queries(
                surface=surface,
                subject_type=subject_type,
                query_count=query_count,
                duration_ms=(perf_counter() - started) * 1000,
                payload_bytes=0,
            )
            return result

        return observed

    return decorate


__all__ = ["observed_ska_projection"]
