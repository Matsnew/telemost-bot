from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))


def now_msk() -> datetime:
    return datetime.now(MSK)


def fmt_msk(dt: datetime, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Format a datetime (naive UTC or aware) in Moscow time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime(fmt)
