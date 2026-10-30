import asyncio

import httpx
import pytest

from health_checks import get_healthy_backends, is_backend_healthy


def run_probe(handler, follow_redirects=False):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=follow_redirects,
            trust_env=False,
        ) as client:
            return await is_backend_healthy(client, "http://backend-a")
    return asyncio.run(scenario())


def test_healthy_response_returns_true():
    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == "http://backend-a/health"
        return httpx.Response(200)
    assert run_probe(handler) is True


@pytest.mark.parametrize("status", [204, 301, 302, 401, 404, 429, 500, 503])
def test_unhealthy_response_returns_false(status):
    assert run_probe(lambda request: httpx.Response(status)) is False


def test_connection_failure_returns_false():
    def handler(request):
        raise httpx.ConnectError("Backend unavailable", request=request)
    assert run_probe(handler) is False


def test_timeout_returns_false():
    def handler(request):
        raise httpx.ReadTimeout("Backend timed out", request=request)
    assert run_probe(handler) is False


def test_health_redirect_is_not_followed_even_if_the_client_normally_follows():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(302, headers={"location": "/login"})
        return httpx.Response(200)

    assert run_probe(handler, follow_redirects=True) is False
    assert seen == ["/health"]


def test_health_probe_sets_the_one_second_timeout():
    def handler(request):
        assert request.extensions["timeout"] == {
            "connect": 1.0, "read": 1.0, "write": 1.0, "pool": 1.0,
        }
        return httpx.Response(200)
    assert run_probe(handler) is True


def test_health_probe_does_not_swallow_cancellation():
    async def handler(request):
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        run_probe(handler)


def test_health_scan_filters_failures_and_preserves_configured_order():
    backends = [f"http://backend-{letter}" for letter in "abcde"]
    original = backends.copy()
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "backend-b":
            return httpx.Response(500)
        if request.url.host == "backend-c":
            raise httpx.ConnectError("Offline", request=request)
        if request.url.host == "backend-d":
            raise httpx.ReadTimeout("Slow", request=request)
        return httpx.Response(200)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False,
        ) as client:
            return await get_healthy_backends(client, backends)

    assert asyncio.run(scenario()) == [backends[0], backends[4]]
    assert seen == [f"{backend}/health" for backend in backends]
    assert backends == original


def test_health_scan_returns_empty_when_all_backends_are_unhealthy():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503)),
            trust_env=False,
        ) as client:
            return await get_healthy_backends(
                client, ["http://backend-a", "http://backend-b"],
            )
    assert asyncio.run(scenario()) == []


def test_health_scan_with_no_backends_makes_no_requests():
    def handler(request):
        pytest.fail("An empty backend list must not make HTTP requests")

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False,
        ) as client:
            return await get_healthy_backends(client, [])
    assert asyncio.run(scenario()) == []


def test_a_later_scan_can_detect_recovery():
    state = {"b_is_up": False}
    backends = ["http://backend-a", "http://backend-b"]

    def handler(request):
        if request.url.host == "backend-b" and not state["b_is_up"]:
            raise httpx.ConnectError("Offline", request=request)
        return httpx.Response(200)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False,
        ) as client:
            assert await get_healthy_backends(client, backends) == [backends[0]]
            state["b_is_up"] = True
            assert await get_healthy_backends(client, backends) == backends
    asyncio.run(scenario())
