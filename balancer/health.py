"""
Heimdall — Active Health Checker

Runs as a background asyncio task that periodically pings each backend's
health endpoint. Backends that fail consecutively beyond a threshold are
marked unhealthy and excluded from the routing pool. They are automatically
restored once they start responding again.
"""

import asyncio
import logging
import time

import httpx

logger = logging.getLogger("heimdall.health")


class HealthChecker:
    """
    Monitors backend health via periodic HTTP pings.

    Attributes:
        backends:            List of backend config dicts (each has "url", "weight").
        interval:            Seconds between health check rounds.
        timeout:             Seconds to wait for a health check response.
        unhealthy_threshold: Consecutive failures before marking a backend unhealthy.
        endpoint:            The URL path to ping on each backend (e.g. "/health").
    """

    def __init__(
        self,
        backends: list[dict],
        interval: int = 10,
        timeout: int = 3,
        unhealthy_threshold: int = 3,
        endpoint: str = "/health",
    ):
        self.backends = backends
        self.interval = interval
        self.timeout = timeout
        self.unhealthy_threshold = unhealthy_threshold
        self.endpoint = endpoint

        # Internal state: keyed by backend URL
        self._healthy: dict[str, bool] = {b["url"]: True for b in backends}
        self._failure_counts: dict[str, int] = {b["url"]: 0 for b in backends}
        self._last_check_time: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    # ── Public API ────────────────────────────────────────

    def get_healthy_backends(self) -> list[dict]:
        """Return the list of backends currently marked as healthy."""
        return [b for b in self.backends if self._healthy.get(b["url"], False)]

    def get_all_statuses(self) -> list[dict]:
        """Return health status details for every backend (for admin endpoints)."""
        statuses = []
        for b in self.backends:
            url = b["url"]
            statuses.append({
                "url": url,
                "weight": b.get("weight", 1),
                "healthy": self._healthy.get(url, False),
                "consecutive_failures": self._failure_counts.get(url, 0),
                "last_checked": self._last_check_time.get(url),
            })
        return statuses

    def start(self) -> None:
        """Launch the background health check loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())
            logger.info("Health checker started (interval=%ds)", self.interval)

    async def stop(self) -> None:
        """Cancel the background health check loop."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("Health checker stopped")

    # ── Internal ──────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Main loop: check all backends, sleep, repeat."""
        while True:
            await self._check_all()
            await asyncio.sleep(self.interval)

    async def _check_all(self) -> None:
        """Ping all backends concurrently."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            tasks = [self._check_one(client, b) for b in self.backends]
            await asyncio.gather(*tasks)

    async def _check_one(self, client: httpx.AsyncClient, backend: dict) -> None:
        """Ping a single backend and update its health state."""
        url = backend["url"]
        check_url = f"{url}{self.endpoint}"
        self._last_check_time[url] = time.time()

        try:
            response = await client.get(check_url)
            if response.status_code == 200:
                self._on_success(url)
            else:
                self._on_failure(url, reason=f"status {response.status_code}")
        except httpx.RequestError as exc:
            self._on_failure(url, reason=str(exc))

    def _on_success(self, url: str) -> None:
        """Handle a successful health check."""
        was_unhealthy = not self._healthy[url]
        self._failure_counts[url] = 0
        self._healthy[url] = True

        if was_unhealthy:
            logger.info("✅ Backend RECOVERED: %s", url)

    def _on_failure(self, url: str, reason: str) -> None:
        """Handle a failed health check."""
        self._failure_counts[url] += 1
        count = self._failure_counts[url]

        if count >= self.unhealthy_threshold and self._healthy[url]:
            self._healthy[url] = False
            logger.warning(
                "❌ Backend UNHEALTHY: %s (reason: %s, failures: %d)",
                url, reason, count
            )
        elif self._healthy[url]:
            logger.debug(
                "⚠️  Backend check failed: %s (reason: %s, failures: %d/%d)",
                url, reason, count, self.unhealthy_threshold
            )
