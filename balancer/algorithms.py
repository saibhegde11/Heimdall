"""
Heimdall — Load Balancing Algorithms

Implements three scheduling strategies:
  1. Round-Robin: Cycles through backends sequentially.
  2. Weighted Round-Robin: Routes proportionally more traffic to higher-weight backends.
  3. Least Connections: Routes to the backend with the fewest active connections.

All algorithms are async-safe and only route to healthy backends.
"""

import asyncio
from abc import ABC, abstractmethod


class LoadBalancer(ABC):
    """Abstract base class for all load balancing algorithms."""

    @abstractmethod
    async def get_next_backend(self, healthy_backends: list[dict]) -> dict | None:
        """
        Select the next backend to route a request to.

        Args:
            healthy_backends: List of backend dicts that are currently healthy.
                              Each dict has at least {"url": str, "weight": int}.

        Returns:
            The selected backend dict, or None if no backends are available.
        """
        ...

    @abstractmethod
    def record_connection_start(self, backend_url: str) -> None:
        """Called when a request begins being proxied to a backend."""
        ...

    @abstractmethod
    def record_connection_end(self, backend_url: str) -> None:
        """Called when a proxied request completes (success or failure)."""
        ...


class RoundRobin(LoadBalancer):
    """
    Cycles through healthy backends in sequential order.

    Simple and fair when backends have equal capacity.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self):
        self._index = 0
        self._lock = asyncio.Lock()

    async def get_next_backend(self, healthy_backends: list[dict]) -> dict | None:
        if not healthy_backends:
            return None

        async with self._lock:
            # Wrap index if it exceeds the list length (backends may have changed)
            self._index = self._index % len(healthy_backends)
            backend = healthy_backends[self._index]
            self._index = (self._index + 1) % len(healthy_backends)
            return backend

    def record_connection_start(self, backend_url: str) -> None:
        pass  # Not needed for round-robin

    def record_connection_end(self, backend_url: str) -> None:
        pass  # Not needed for round-robin


class WeightedRoundRobin(LoadBalancer):
    """
    Routes more traffic to backends with higher weights.

    Uses a Smooth Weighted Round-Robin (SWRR) algorithm, which avoids
    bursty routing that simple weight expansion causes.

    Example: backends A(w=5), B(w=1), C(w=1) produces the sequence
    A, A, B, A, C, A, A — spreading B and C across the cycle evenly.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        # Tracks the "current weight" for each backend URL in the SWRR algorithm
        self._current_weights: dict[str, int] = {}

    async def get_next_backend(self, healthy_backends: list[dict]) -> dict | None:
        if not healthy_backends:
            return None

        async with self._lock:
            total_weight = sum(b["weight"] for b in healthy_backends)

            # Initialize current weights for any new backends
            for b in healthy_backends:
                if b["url"] not in self._current_weights:
                    self._current_weights[b["url"]] = 0

            # Step 1: Increase each backend's current weight by its effective weight
            for b in healthy_backends:
                self._current_weights[b["url"]] += b["weight"]

            # Step 2: Select the backend with the highest current weight
            best = max(healthy_backends, key=lambda b: self._current_weights[b["url"]])

            # Step 3: Reduce the selected backend's current weight by total weight
            self._current_weights[best["url"]] -= total_weight

            # Clean up weights for backends that are no longer in the healthy list
            active_urls = {b["url"] for b in healthy_backends}
            self._current_weights = {
                url: w for url, w in self._current_weights.items() if url in active_urls
            }

            return best

    def record_connection_start(self, backend_url: str) -> None:
        pass

    def record_connection_end(self, backend_url: str) -> None:
        pass


class LeastConnections(LoadBalancer):
    """
    Routes to the backend with the fewest active (in-flight) connections.

    Ideal when backend processing times vary — slower backends naturally
    accumulate connections and stop receiving new ones.
    """

    def __init__(self):
        self._active_connections: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def get_next_backend(self, healthy_backends: list[dict]) -> dict | None:
        if not healthy_backends:
            return None

        async with self._lock:
            # Initialize counts for any new backends
            for b in healthy_backends:
                if b["url"] not in self._active_connections:
                    self._active_connections[b["url"]] = 0

            # Select the backend with the fewest active connections
            best = min(
                healthy_backends,
                key=lambda b: self._active_connections[b["url"]]
            )
            return best

    def record_connection_start(self, backend_url: str) -> None:
        self._active_connections[backend_url] = (
            self._active_connections.get(backend_url, 0) + 1
        )

    def record_connection_end(self, backend_url: str) -> None:
        self._active_connections[backend_url] = max(
            0, self._active_connections.get(backend_url, 0) - 1
        )


# ── Factory ──────────────────────────────────────────────────

ALGORITHM_MAP = {
    "round-robin": RoundRobin,
    "weighted-round-robin": WeightedRoundRobin,
    "least-connections": LeastConnections,
}


def create_balancer(algorithm_name: str) -> LoadBalancer:
    """
    Factory function to instantiate a load balancer by name.

    Args:
        algorithm_name: One of 'round-robin', 'weighted-round-robin', 'least-connections'.

    Raises:
        ValueError: If the algorithm name is not recognized.
    """
    cls = ALGORITHM_MAP.get(algorithm_name)
    if cls is None:
        supported = ", ".join(ALGORITHM_MAP.keys())
        raise ValueError(
            f"Unknown algorithm '{algorithm_name}'. Supported: {supported}"
        )
    return cls()
