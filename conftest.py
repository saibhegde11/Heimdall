import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.datastructures import State

import balancer
import health_checks
import metrics
import proxy
import rate_limiter


@pytest.fixture(autouse=True)
def reset_selection(monkeypatch):
    monkeypatch.setattr(balancer, "next_index", 0)
    monkeypatch.setattr(balancer, "least_next_index", 0)
    monkeypatch.setattr(balancer, "current_scores", {})
    monkeypatch.setattr(balancer, "in_flight", {})
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", "round_robin")


@pytest.fixture(autouse=True)
def reset_metrics(monkeypatch):
    monkeypatch.setattr(metrics, "totals", {
        "requests_total": 0,
        "backend_attempts_total": 0,
        "retries_total": 0,
        "rate_limited_total": 0,
    })


@pytest.fixture(autouse=True)
def reset_rate_limit(monkeypatch):
    monkeypatch.setattr(rate_limiter, "buckets", {})
    monkeypatch.setattr(rate_limiter, "CAPACITY", 10)
    monkeypatch.setattr(rate_limiter, "REFILL_RATE", 2.0)


@pytest.fixture(autouse=True)
def reset_timeouts(monkeypatch):
    # Keep regression scenarios independent of a developer's selected config.
    # Configuration integration tests start fresh processes with their own files.
    monkeypatch.setattr(proxy, "BACKEND_TIMEOUT", 2.0)
    monkeypatch.setattr(proxy, "HEALTH_CHECK_TIMEOUT", 1.0)
    monkeypatch.setattr(proxy, "HEALTH_CHECK_INTERVAL", 2.0)
    monkeypatch.setattr(proxy, "FORWARDING_TIMEOUT", 3.0)
    monkeypatch.setattr(health_checks, "HEALTH_CHECK_TIMEOUT", 1.0)


@pytest.fixture
def rate_clock(monkeypatch):
    state = {"now": 1000.0}
    # Replace this module's time reference, preserving asyncio's real clock.
    monkeypatch.setattr(
        rate_limiter, "time",
        SimpleNamespace(monotonic=lambda: state["now"]),
    )
    return state


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch):
    async def reject_async_request(self, request):
        raise AssertionError("Tests must use MockTransport or ASGITransport")

    def reject_sync_request(self, request):
        raise AssertionError("Tests must use MockTransport or ASGITransport")

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", reject_async_request
    )
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", reject_sync_request
    )


@pytest.fixture
def run_proxy(monkeypatch):
    """Run real proxy routes with a controlled outbound HTTP transport.

    ASGITransport does not run lifespan. Startup, monitoring and shutdown
    have their own tests in test_proxy_lifespan.py.
    """
    def run(handler, exercise, backends=None, client_address=("127.0.0.1", 123)):
        async def scenario():
            monkeypatch.setattr(proxy.app, "state", State())
            candidates = (
                ["http://backend-a", "http://backend-b"]
                if backends is None else list(backends)
            )
            async with (
                httpx.AsyncClient(
                    transport=httpx.MockTransport(handler),
                    timeout=2.0,
                    trust_env=False,
                ) as outbound,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=proxy.app, client=client_address),
                    base_url="http://heimdall.test",
                    follow_redirects=False,
                    trust_env=False,
                ) as incoming,
            ):
                proxy.app.state.http_client = outbound
                proxy.app.state.healthy_backends = candidates
                return await exercise(incoming)

        return asyncio.run(scenario())

    return run


@pytest.fixture
def real_backend_router():
    """Dispatch outbound requests into the uploaded A and B FastAPI apps."""
    import backend_a
    import backend_b

    apps = {"backend-a": backend_a.app, "backend-b": backend_b.app}

    async def handler(request):
        backend_app = apps[request.url.host]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend_app),
            follow_redirects=False,
            trust_env=False,
        ) as backend_client:
            return await backend_client.send(request, follow_redirects=False)

    return handler
