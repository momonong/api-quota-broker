"""Conservative interpretation of provider Retry-After metadata."""

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def parse_retry_after(value: str | None, now: datetime) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdecimal():
        if len(value) > 10:
            return None
        return min(int(value), 86_400)
    try:
        instant = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if instant.tzinfo is None:
        return None
    seconds = math.ceil((instant.astimezone(UTC) - now.astimezone(UTC)).total_seconds())
    return min(max(seconds, 0), 86_400)
