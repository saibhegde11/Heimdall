import asyncio
import inspect
from types import SimpleNamespace

import httpx
import pytest
from starlette.datastructures import State

import proxy


BACKENDS = ["http://backend-a", "http://backend-b"]


def run_scenario(scenario):
    async def bounded():
        return await asyncio.wait_for(scenario(), timeout=5.0)
    return asyncio.run(bounded())


class ScanClock:
    """Advance monitor iterations explicitly instead of waiting real seconds."""
    def __init__(self):
        self.waiting = asyncio.Queue()
        self.permits = asyncio.Queue()

    async def sleep(self, delay):
        self.waiting.put_nowait(delay)
        await self.permits.get()

    async def wait_until_sleeping(self):
        delay = await asyncio.wait_for(self.waiting.get(), timeout=2.0)
        assert delay == proxy.HEALTH_CHECK_INTERVAL

    async def tick(self):
        self.permits.put_nowait(None)
        await self.wait_until_sleeping()


def install_scan_clock(monkeypatch, clock):
    # Replace proxy's reference, not asyncio.sleep used by the test runner.
    monkeypatch.setattr(proxy, "asyncio", SimpleNamespace(
        sleep=clock.sleep,
        create_task=asyncio.create_task,
        CancelledError=asyncio.CancelledError,
        timeout=asyncio.timeout,
    ))


def install_mock_clients(monkeypatch, handler):
    clients = []
    requests = []

    def factory(**kwargs):
        assert kwargs.get("trust_env") is False
        role = "forward" if not clients else "health"

        async def dispatch(request):
            requests.append((role, request.url.host, request.url.path))
            result = handler(request)
            if inspect.isawaitable(result):
                result = await result
            return result

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch), **kwargs,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(proxy, "httpx", SimpleNamespace(
        AsyncClient=factory,
        TimeoutException=httpx.TimeoutException,
        RequestError=httpx.RequestError,
    ))
    monkeypatch.setattr(proxy, "BACKENDS", BACKENDS.copy())
    monkeypatch.setattr(proxy.app, "state", State())
    return clients, requests


def test_upstream_clients_ignore_ambient_forward_proxy_settings(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "socks5://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:9")
    monkeypatch.setattr(proxy.app, "state", State())
    health_clients = []

    async def scan_without_network(health_client, backends):
        health_clients.append(health_client)
        assert health_client.trust_env is False
        return []

    monkeypatch.setattr(proxy, "get_healthy_backends", scan_without_network)

    async def scenario():
        async with proxy.app.router.lifespan_context(proxy.app):
            forwarding = proxy.app.state.http_client
            assert forwarding.trust_env is False
        assert forwarding.is_closed
        assert all(client.is_closed for client in health_clients)

    run_scenario(scenario)


def track_monitor(monkeypatch):
    tasks = []
    actual_monitor = proxy.monitor_backends

    async def tracked(app, health_client):
        tasks.append(asyncio.current_task())
        return await actual_monitor(app, health_client)

    monkeypatch.setattr(proxy, "monitor_backends", tracked)
    return tasks


def test_real_lifespan_routes_around_failures_and_detects_recovery(monkeypatch):
    state = {"backend-a": True, "backend-b": True}

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200 if state[request.url.host] else 503)
        return httpx.Response(200, json={"server": request.url.host})

    clients, requests = install_mock_clients(monkeypatch, handler)
    tasks = track_monitor(monkeypatch)

    async def scenario():
        clock = ScanClock()
        install_scan_clock(monkeypatch, clock)
        async with proxy.app.router.lifespan_context(proxy.app):
            await clock.wait_until_sleeping()
            assert proxy.app.state.healthy_backends == BACKENDS
            assert len(clients) == 2
            assert clients[0] is not clients[1]
            assert proxy.app.state.http_client is clients[0]
            assert clients[0].timeout.read == 2.0
            assert clients[1].timeout.read == 1.0
            assert not any(client.is_closed for client in clients)

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=proxy.app),
                base_url="http://heimdall.test", trust_env=False,
            ) as incoming:
                responses = [await incoming.get("/") for _ in range(2)]
                assert [response.json()["server"] for response in responses] == [
                    "backend-a", "backend-b",
                ]
                original_snapshot = proxy.app.state.healthy_backends
                state["backend-b"] = False
                await clock.tick()
                assert proxy.app.state.healthy_backends == [BACKENDS[0]]
                assert original_snapshot == BACKENDS
                only_a = await incoming.get("/")
                assert only_a.json() == {"server": "backend-a"}

                state["backend-a"] = False
                await clock.tick()
                assert proxy.app.state.healthy_backends == []
                unavailable = await incoming.get("/")
                assert unavailable.status_code == 503

                state["backend-b"] = True
                await clock.tick()
                assert proxy.app.state.healthy_backends == [BACKENDS[1]]
                recovered_b = await incoming.get("/")
                assert recovered_b.json() == {"server": "backend-b"}

                state["backend-a"] = True
                await clock.tick()
                assert proxy.app.state.healthy_backends == BACKENDS
                recovered = [await incoming.get("/") for _ in range(2)]
                assert {response.json()["server"] for response in recovered} == {
                    "backend-a", "backend-b",
                }
                assert proxy.app.state.http_client is clients[0]

        assert len(tasks) == 1
        assert tasks[0].cancelled()
        assert all(client.is_closed for client in clients)

    run_scenario(scenario)
    assert all(role == "health" for role, _, path in requests if path == "/health")
    assert all(role == "forward" for role, _, path in requests if path == "/")


def test_startup_with_all_backends_down_can_later_recover(monkeypatch):
    state = {"up": False}

    def handler(request):
        if not state["up"]:
            raise httpx.ConnectError("Offline", request=request)
        return httpx.Response(200, json={"server": request.url.host})

    clients, _ = install_mock_clients(monkeypatch, handler)

    async def scenario():
        clock = ScanClock()
        install_scan_clock(monkeypatch, clock)
        async with proxy.app.router.lifespan_context(proxy.app):
            await clock.wait_until_sleeping()
            assert proxy.app.state.healthy_backends == []
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=proxy.app),
                base_url="http://heimdall.test", trust_env=False,
            ) as incoming:
                unavailable = await incoming.get("/")
                assert unavailable.status_code == 503
                state["up"] = True
                await clock.tick()
                assert proxy.app.state.healthy_backends == BACKENDS
                recovered = await incoming.get("/")
                assert recovered.status_code == 200
        assert all(client.is_closed for client in clients)

    run_scenario(scenario)


def test_failure_after_a_successful_probe_fails_over_to_the_other_backend(monkeypatch):
    state = {"crashed": False}

    def handler(request):
        if state["crashed"] and request.url.host == "backend-a":
            raise httpx.ConnectError("Crashed after health check", request=request)
        return httpx.Response(200, json={"server": request.url.host})

    install_mock_clients(monkeypatch, handler)

    async def scenario():
        clock = ScanClock()
        install_scan_clock(monkeypatch, clock)
        async with proxy.app.router.lifespan_context(proxy.app):
            await clock.wait_until_sleeping()
            state["crashed"] = True
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=proxy.app),
                base_url="http://heimdall.test", trust_env=False,
            ) as incoming:
                recovered = await incoming.get("/")
                assert recovered.status_code == 200
                assert recovered.json() == {"server": "backend-b"}
                await clock.tick()
                assert proxy.app.state.healthy_backends == [BACKENDS[1]]
                for _ in range(2):
                    response = await incoming.get("/")
                    assert response.status_code == 200
                    assert response.json() == {"server": "backend-b"}

    run_scenario(scenario)


def test_shutdown_cleans_up_when_the_lifespan_body_raises(monkeypatch):
    clients, _ = install_mock_clients(
        monkeypatch, lambda request: httpx.Response(200),
    )
    tasks = track_monitor(monkeypatch)

    async def scenario():
        clock = ScanClock()
        install_scan_clock(monkeypatch, clock)
        with pytest.raises(RuntimeError, match="Controlled body failure"):
            async with proxy.app.router.lifespan_context(proxy.app):
                await clock.wait_until_sleeping()
                raise RuntimeError("Controlled body failure")
        assert len(tasks) == 1
        assert tasks[0].cancelled()
        assert all(client.is_closed for client in clients)

    run_scenario(scenario)


def test_startup_failure_closes_both_clients_without_starting_the_monitor(monkeypatch):
    def handler(request):
        raise RuntimeError("Unexpected startup failure")

    clients, _ = install_mock_clients(monkeypatch, handler)
    tasks = track_monitor(monkeypatch)

    async def scenario():
        with pytest.raises(RuntimeError, match="Unexpected startup failure"):
            async with proxy.app.router.lifespan_context(proxy.app):
                pytest.fail("Startup was expected to fail before yielding")
        assert len(clients) == 2
        assert all(client.is_closed for client in clients)
        assert tasks == []

    run_scenario(scenario)


def test_shutdown_can_cancel_a_monitor_waiting_on_a_health_response(monkeypatch):
    state = {"block": False}
    gates = {}

    async def handler(request):
        if state["block"]:
            gates["started"].set()
            await gates["release"].wait()
        return httpx.Response(200)

    clients, _ = install_mock_clients(monkeypatch, handler)
    tasks = track_monitor(monkeypatch)

    async def scenario():
        clock = ScanClock()
        gates["started"] = asyncio.Event()
        gates["release"] = asyncio.Event()
        install_scan_clock(monkeypatch, clock)
        try:
            async with proxy.app.router.lifespan_context(proxy.app):
                await clock.wait_until_sleeping()
                state["block"] = True
                clock.permits.put_nowait(None)
                await asyncio.wait_for(gates["started"].wait(), timeout=2.0)
        finally:
            gates["release"].set()
        assert len(tasks) == 1
        assert tasks[0].cancelled()
        assert all(client.is_closed for client in clients)

    run_scenario(scenario)
