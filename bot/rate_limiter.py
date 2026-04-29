import time
from collections import defaultdict
from config import config

# { user_id: [timestamp, ...] }
_ask_history: dict[int, list[float]] = defaultdict(list)


def check_ask_rate_limit(user_id: int) -> bool:
    """Returns True if the request is allowed, False if rate-limited."""
    now = time.monotonic()
    window_start = now - 60.0

    history = _ask_history[user_id]
    # Drop timestamps outside the 1-minute window
    _ask_history[user_id] = [t for t in history if t >= window_start]

    if len(_ask_history[user_id]) >= config.ASK_RATE_LIMIT:
        return False

    _ask_history[user_id].append(now)
    return True
