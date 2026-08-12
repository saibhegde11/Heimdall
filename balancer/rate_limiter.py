"""
Heimdall — Per-IP Sliding Window Rate Limiter

Limits the number of requests a single client IP can make within a
configurable time window. Returns 429 Too Many Requests with a
Retry-After header when the limit is exceeded.

Uses an in-memory sliding window log approach: each IP's request
timestamps are stored in a list, and expired entries are pruned
on each check.
"""

import time
import asyncio
import logging
from collections import defaultdict

logger = logging.getLogger("heimdall.rate_limiter")


class RateLimiter:
    """
    Sliding-window rate limiter keyed by client IP address.

    Attributes:
        max_requests:   Maximum number of requests allowed per window.
        window_seconds: Duration of the sliding window in seconds.
    """

    def __init__(self, max_requests: int = 50, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds

        # { client_ip: [timestamp, timestamp, ...] }
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

        # Periodic cleanup to prevent memory leaks from stale IPs
        self._cleanup_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the background cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """Stop the background cleanup task."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    async def is_allowed(self, client_ip: str) -> tuple[bool, dict]:
        """
        Check if a request from the given IP is allowed.

        Returns:
            A tuple of (allowed: bool, info: dict).
            If not allowed, info contains 'retry_after' (seconds until the
            oldest request in the window expires).
        """
        now = time.time()
        window_start = now - self.window_seconds

        async with self._lock:
            # Prune expired timestamps for this IP
            timestamps = self._requests[client_ip]
            self._requests[client_ip] = [
                ts for ts in timestamps if ts > window_start
            ]
            timestamps = self._requests[client_ip]

            if len(timestamps) >= self.max_requests:
                # Calculate when the oldest request in the window will expire
                retry_after = round(timestamps[0] - window_start, 1)
                retry_after = max(retry_after, 1.0)
                logger.warning(
                    "Rate limit exceeded for IP %s (%d/%d in %ds window)",
                    client_ip, len(timestamps), self.max_requests, self.window_seconds
                )
                return False, {"retry_after": retry_after}

            # Record this request
            timestamps.append(now)
            return True, {
                "remaining": self.max_requests - len(timestamps),
                "reset": round(timestamps[0] - window_start, 1) if timestamps else self.window_seconds,
            }

    async def _cleanup_loop(self) -> None:
        """Periodically remove entries for IPs with no recent requests."""
        while True:
            await asyncio.sleep(self.window_seconds * 2)
            now = time.time()
            window_start = now - self.window_seconds

            async with self._lock:
                stale_ips = [
                    ip for ip, timestamps in self._requests.items()
                    if not timestamps or timestamps[-1] <= window_start
                ]
                for ip in stale_ips:
                    del self._requests[ip]

                if stale_ips:
                    logger.debug("Cleaned up %d stale IP entries", len(stale_ips))
