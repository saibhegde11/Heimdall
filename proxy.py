"""
Heimdall — Async HTTP Load Balancer & Reverse Proxy

A transparent reverse proxy that distributes incoming HTTP traffic across
a pool of backend servers. Supports multiple load balancing algorithms,
active health checks, per-IP rate limiting, and request retries.

Usage:
    uvicorn proxy:app --port 8000
"""

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from balancer.algorithms import create_balancer, LoadBalancer
from balancer.health import HealthChecker
from balancer.rate_limiter import RateLimiter

# ── Logging Setup ────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-22s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("heimdall.proxy")

# ── Configuration ────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    """Load and return the YAML configuration file."""
    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)
    logger.info("Configuration loaded from %s", CONFIG_PATH)
    return config


# ── Globals (initialized during lifespan) ────────────────────

config: dict = {}
balancer: LoadBalancer | None = None
health_checker: HealthChecker | None = None
rate_limiter: RateLimiter | None = None
http_client: httpx.AsyncClient | None = None

# Per-backend stats tracking
stats: dict[str, dict] = {}

# Headers that should NOT be forwarded between client ↔ proxy ↔ backend
# (hop-by-hop headers per HTTP/1.1 spec, RFC 2616 §13.5.1)
HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host",  # We also strip Host since the backend has a different host
})

# Maximum number of backends to retry on failure
MAX_RETRIES = 3


# ── Application Lifespan ─────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic for the FastAPI application."""
    global config, balancer, health_checker, rate_limiter, http_client, stats

    # ─ Startup ─
    config = load_config()

    # Initialize backends
    backends = config.get("backends", [])
    for b in backends:
        b.setdefault("weight", 1)
        stats[b["url"]] = {
            "requests": 0,
            "failures": 0,
            "total_latency_ms": 0.0,
        }

    # Initialize load balancer
    algorithm_name = config.get("algorithm", "round-robin")
    balancer = create_balancer(algorithm_name)
    logger.info(
        "Load balancer initialized: algorithm=%s, backends=%d",
        algorithm_name, len(backends),
    )

    # Initialize health checker
    hc_config = config.get("health_check", {})
    health_checker = HealthChecker(
        backends=backends,
        interval=hc_config.get("interval_seconds", 10),
        timeout=hc_config.get("timeout_seconds", 3),
        unhealthy_threshold=hc_config.get("unhealthy_threshold", 3),
        endpoint=hc_config.get("endpoint", "/health"),
    )
    if hc_config.get("enabled", True):
        health_checker.start()

    # Initialize rate limiter
    rl_config = config.get("rate_limit", {})
    rate_limiter = RateLimiter(
        max_requests=rl_config.get("max_requests", 50),
        window_seconds=rl_config.get("window_seconds", 60),
    )
    if rl_config.get("enabled", True):
        rate_limiter.start()

    # Initialize shared HTTP client (connection pooling)
    http_client = httpx.AsyncClient(timeout=30.0)

    logger.info("🛡️  Heimdall is ready. Guarding the Bifröst.")

    yield  # ← App is running

    # ─ Shutdown ─
    if health_checker:
        await health_checker.stop()
    if rate_limiter:
        await rate_limiter.stop()
    if http_client:
        await http_client.aclose()

    logger.info("🛡️  Heimdall has shut down.")


# ── FastAPI App ──────────────────────────────────────────────

app = FastAPI(
    title="Heimdall Load Balancer",
    description="Async HTTP reverse proxy and load balancer",
    lifespan=lifespan,
)


# ── Admin Endpoints ──────────────────────────────────────────
# Prefixed with /_heimdall/ to avoid collisions with proxied routes.

@app.get("/_heimdall/health")
async def admin_health():
    """Show the health status of all registered backends."""
    return {
        "algorithm": config.get("algorithm", "round-robin"),
        "backends": health_checker.get_all_statuses() if health_checker else [],
    }


@app.get("/_heimdall/stats")
async def admin_stats():
    """Show per-backend request statistics."""
    enriched_stats = []
    for url, s in stats.items():
        avg_latency = (
            round(s["total_latency_ms"] / s["requests"], 2)
            if s["requests"] > 0
            else 0.0
        )
        enriched_stats.append({
            "url": url,
            "total_requests": s["requests"],
            "total_failures": s["failures"],
            "avg_latency_ms": avg_latency,
        })
    return {"stats": enriched_stats}


# ── Catch-All Proxy Route ───────────────────────────────────

@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_request(request: Request, path: str):
    """
    Transparent reverse proxy handler.

    Forwards any incoming request to a healthy backend, preserving:
      - HTTP method
      - URL path and query string
      - Request headers (minus hop-by-hop headers)
      - Request body

    Returns the backend's response (status, headers, body) to the client.
    """
    # ── Rate Limiting ──
    rl_config = config.get("rate_limit", {})
    if rl_config.get("enabled", True) and rate_limiter:
        client_ip = request.client.host if request.client else "unknown"
        allowed, info = await rate_limiter.is_allowed(client_ip)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too Many Requests",
                    "retry_after": info["retry_after"],
                },
                headers={"Retry-After": str(info["retry_after"])},
            )

    # ── Select Backend with Retry ──
    healthy = health_checker.get_healthy_backends() if health_checker else config.get("backends", [])

    if not healthy:
        logger.error("No healthy backends available!")
        return JSONResponse(
            status_code=503,
            content={"error": "Service Unavailable", "detail": "No healthy backends"},
        )

    # Build the outgoing request components
    body = await request.body()
    headers = _filter_headers(request.headers)
    query_string = str(request.url.query) if request.url.query else ""

    # Try up to MAX_RETRIES different backends
    tried_urls: set[str] = set()
    last_error: str = ""

    for attempt in range(min(MAX_RETRIES, len(healthy))):
        # Get next backend from the algorithm
        available = [b for b in healthy if b["url"] not in tried_urls]
        if not available:
            break

        backend = await balancer.get_next_backend(available)
        if backend is None:
            break

        backend_url = backend["url"]
        tried_urls.add(backend_url)

        # Build target URL
        target = f"{backend_url}/{path}"
        if query_string:
            target = f"{target}?{query_string}"

        # Track connection for least-connections
        balancer.record_connection_start(backend_url)
        start_time = time.time()

        try:
            response = await http_client.request(
                method=request.method,
                url=target,
                headers=headers,
                content=body,
            )

            # Record stats
            latency_ms = (time.time() - start_time) * 1000
            stats[backend_url]["requests"] += 1
            stats[backend_url]["total_latency_ms"] += latency_ms
            balancer.record_connection_end(backend_url)

            logger.info(
                "%s %s → %s [%d] %.0fms",
                request.method, f"/{path}", backend_url,
                response.status_code, latency_ms,
            )

            # Build and return the proxied response
            response_headers = _filter_headers(response.headers)
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=dict(response_headers),
            )

        except httpx.RequestError as exc:
            balancer.record_connection_end(backend_url)
            stats[backend_url]["failures"] += 1
            last_error = str(exc)
            logger.warning(
                "%s %s → %s FAILED (attempt %d/%d): %s",
                request.method, f"/{path}", backend_url,
                attempt + 1, MAX_RETRIES, last_error,
            )
            continue

    # All retries exhausted
    logger.error("All backend attempts failed for %s /%s", request.method, path)
    return JSONResponse(
        status_code=502,
        content={
            "error": "Bad Gateway",
            "detail": f"All backends failed. Last error: {last_error}",
        },
    )


def _filter_headers(headers) -> dict[str, str]:
    """
    Filter out hop-by-hop headers that should not be forwarded
    between proxy and backend/client.
    """
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }