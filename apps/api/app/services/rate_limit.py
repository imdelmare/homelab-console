"""In-memory sliding-window rate limiting.

Single-process by design: the app runs as one uvicorn process. If the
deployment ever scales beyond one process this must move to a shared store.
"""

import time
from collections import defaultdict, deque
from threading import Lock


class RateLimiter:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, scope: str, key: str, max_events: int, window_seconds: float) -> bool:
        now = time.monotonic()
        bucket_key = (scope, key)
        with self._lock:
            bucket = self._events[bucket_key]
            while bucket and now - bucket[0] > window_seconds:
                bucket.popleft()
            if len(bucket) >= max_events:
                return False
            bucket.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


limiter = RateLimiter()

# scope -> (max_events, window_seconds)
LIMITS = {
    "login.attempt": (5, 60.0),
    "login.challenge": (3, 60.0),
    "login.otp": (5, 60.0),
    "login.recovery": (3, 300.0),
    "telegram.callback": (10, 60.0),
    "mcp.pairing.start": (3, 300.0),
    "login.failure": (10, 600.0),
}


def check(scope: str, key: str) -> bool:
    max_events, window = LIMITS[scope]
    return limiter.allow(scope, key, max_events, window)
