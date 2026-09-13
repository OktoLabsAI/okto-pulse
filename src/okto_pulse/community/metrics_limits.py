"""Shared Community CLI/REST telemetry observation bounds."""

DEFAULT_WINDOW_DAYS = 30
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 400


def validate_window_days(value: int) -> int:
    if type(value) is not int or not MIN_WINDOW_DAYS <= value <= MAX_WINDOW_DAYS:
        raise ValueError(
            f"window-days must be an integer between {MIN_WINDOW_DAYS} and {MAX_WINDOW_DAYS}"
        )
    return value
