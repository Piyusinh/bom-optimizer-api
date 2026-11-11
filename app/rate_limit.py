"""In-memory rate limiting for the login endpoint.

A production deployment should replace this with a shared store
(e.g. Redis or slowapi with a Redis backend) when running multiple workers.
"""
import time
from collections import defaultdict
from fastapi import HTTPException, Request, status


class LoginRateLimiter:
    _instance = None
    window_seconds = 60
    max_attempts = 5

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._attempts = defaultdict(list)
        return cls._instance

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        attempts = self._attempts[key]
        attempts = [t for t in attempts if now - t < self.window_seconds]
        self._attempts[key] = attempts
        return len(attempts) < self.max_attempts

    def record(self, key: str) -> None:
        self._attempts[key].append(time.time())


_limiter = LoginRateLimiter()


async def login_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    if not _limiter.is_allowed(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please wait a minute.",
        )
    _limiter.record(key)
