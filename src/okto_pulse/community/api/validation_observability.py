"""Cardinality-safe timing for external validation cognition writes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from okto_pulse.core.services.ska_observability import (
    observe_validation_edition_conflict,
    observe_validation_external_cognition_uow,
)


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or str(exc)).strip().lower()


def _is_conflict(exc: BaseException) -> bool:
    category = getattr(exc, "category", None)
    category_value = str(getattr(category, "value", category) or "").lower()
    code = _error_code(exc)
    return category_value == "conflict" or "conflict" in code


def _is_edition_fence_conflict(exc: BaseException) -> bool:
    code = _error_code(exc)
    return any(
        marker in code
        for marker in (
            "edition",
            "submission_fence",
            "subject_version_conflict",
            "spec_version_conflict",
            "head_revision_conflict",
        )
    )


@contextmanager
def observe_external_validation_write(
    *,
    assessment_kind: str,
    subject_type: str,
) -> Iterator[None]:
    """Measure one external-cognition UoW without retaining identifiers."""

    started = perf_counter()
    outcome = "error"
    try:
        yield
    except BaseException as exc:
        if _is_conflict(exc):
            outcome = "conflict"
        if _is_edition_fence_conflict(exc):
            observe_validation_edition_conflict(
                operation=assessment_kind,
                subject_type=subject_type,
            )
        raise
    else:
        outcome = "success"
    finally:
        observe_validation_external_cognition_uow(
            assessment_kind=assessment_kind,
            subject_type=subject_type,
            outcome=outcome,
            duration_seconds=max(0.0, perf_counter() - started),
        )


__all__ = ["observe_external_validation_write"]
