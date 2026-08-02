"""UTC timestamp helpers shared across persistence and runtime code."""

from datetime import datetime, timedelta, timezone


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def utc_days_ago(days: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=max(days, 0))
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
