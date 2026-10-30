import asyncio

import httpx
import pytest

import balancer
import proxy


A = "http://backend-a"
B = "http://backend-b"
ALGORITHMS = ["round_robin", "weighted_round_robin", "least_in_flight"]


def configure_algorithm(monkeypatch, algorithm):
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", algorithm)
    monkeypatch.setattr(balancer, "BACKEND_WEIGHTS", {A: 2, B: 1})


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_success_releases_the_reserved_attempt(run_proxy, monkeypatch, algorithm):
    configure_algorithm(monkeypatch, algorithm)

    def handler(request):
        assert request.url.host == "backend-a"
        assert balancer.in_flight == {A: 1}
        return httpx.Response(200, json={"server": "A"})

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 200
        assert balancer.in_flight == {A: 0}

    run_proxy(handler, exercise)


def test_backend_http_error_releases_the_attempt_without_retry(run_proxy, monkeypatch):
    configure_algorithm(monkeypatch, "least_in_flight")
    seen = []

    def handler(request):
        seen.append(request.url.host)
        assert balancer.in_flight == {A: 1}
        return httpx.Response(503, json={"detail": "Backend overloaded"})

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 503
        assert response.json() == {"detail": "Backend overloaded"}
        assert balancer.in_flight == {A: 0}

    run_proxy(handler, exercise)
    assert seen == ["backend-a"]


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_get_retry_releases_first_backend_before_reserving_second(run_proxy, monkeypatch, algorithm):
    configure_algorithm(monkeypatch, algorithm)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "backend-a":
            assert balancer.in_flight == {A: 1}
            raise httpx.ConnectError("A stopped", request=request)
        assert balancer.in_flight == {A: 0, B: 1}
        return httpx.Response(200, json={"server": "B"})

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 200
        assert response.json() == {"server": "B"}
        assert balancer.in_flight == {A: 0, B: 0}

    run_proxy(handler, exercise)
    assert seen == ["backend-a", "backend-b"]


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_post_failure_releases_its_only_attempt(run_proxy, monkeypatch, algorithm):
    configure_algorithm(monkeypatch, algorithm)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        assert balancer.in_flight == {A: 1}
        raise httpx.ReadTimeout("POST response lost", request=request)

    async def exercise(client):
        response = await client.post("/echo", json={"message": "hello"})
        assert response.status_code == 504
        assert balancer.in_flight == {A: 0}

    run_proxy(handler, exercise)
    assert seen == ["backend-a"]


def test_exhausted_get_releases_both_failed_attempts(run_proxy, monkeypatch):
    configure_algorithm(monkeypatch, "least_in_flight")
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "backend-a":
            assert balancer.in_flight == {A: 1}
            raise httpx.ConnectError("A stopped", request=request)
        assert balancer.in_flight == {A: 0, B: 1}
        raise httpx.ReadTimeout("B timed out", request=request)

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 504
        assert balancer.in_flight == {A: 0, B: 0}

    run_proxy(handler, exercise)
    assert seen == ["backend-a", "backend-b"]


def test_no_healthy_backend_creates_no_reservation(run_proxy, monkeypatch):
    configure_algorithm(monkeypatch, "least_in_flight")

    def handler(request):
        raise AssertionError("No backend request should be sent")

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 503
        assert balancer.in_flight == {}

    run_proxy(handler, exercise, backends=[])


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_forwarding_deadline_releases_a_cancelled_attempt(run_proxy, monkeypatch, algorithm):
    configure_algorithm(monkeypatch, algorithm)
    monkeypatch.setattr(proxy, "FORWARDING_TIMEOUT", 0.02)
    state = {"cancelled": False}

    async def handler(request):
        assert balancer.in_flight == {A: 1}
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def exercise(client):
        response = await asyncio.wait_for(client.get("/"), timeout=2.0)
        assert response.status_code == 504
        assert balancer.in_flight == {A: 0}

    run_proxy(handler, exercise)
    assert state["cancelled"] is True


def test_deadline_during_retry_releases_both_reservations(run_proxy, monkeypatch):
    configure_algorithm(monkeypatch, "least_in_flight")
    monkeypatch.setattr(proxy, "FORWARDING_TIMEOUT", 0.02)
    seen = []

    async def handler(request):
        seen.append(request.url.host)
        if request.url.host == "backend-a":
            raise httpx.ConnectError("A stopped", request=request)
        assert balancer.in_flight == {A: 0, B: 1}
        await asyncio.Event().wait()

    async def exercise(client):
        response = await asyncio.wait_for(client.get("/"), timeout=2.0)
        assert response.status_code == 504
        assert balancer.in_flight == {A: 0, B: 0}

    run_proxy(handler, exercise)
    assert seen == ["backend-a", "backend-b"]


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_forwarding_task_cancellation_releases_without_retry(run_proxy, monkeypatch, algorithm):
    configure_algorithm(monkeypatch, algorithm)
    gates = {}
    seen = []

    async def handler(request):
        seen.append(request.url.host)
        assert balancer.in_flight == {A: 1}
        gates["started"].set()
        await asyncio.Event().wait()

    async def exercise(client):
        gates["started"] = asyncio.Event()
        pending = asyncio.create_task(client.get("/"))
        try:
            await asyncio.wait_for(gates["started"].wait(), timeout=2.0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=2.0)
            assert balancer.in_flight == {A: 0}
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    run_proxy(handler, exercise)
    assert seen == ["backend-a"]


def test_unexpected_forwarding_exception_also_releases_the_attempt(run_proxy, monkeypatch):
    configure_algorithm(monkeypatch, "least_in_flight")

    def handler(request):
        assert balancer.in_flight == {A: 1}
        raise RuntimeError("Unexpected transport failure")

    async def exercise(client):
        with pytest.raises(RuntimeError, match="Unexpected transport failure"):
            await client.get("/")
        assert balancer.in_flight == {A: 0}

    run_proxy(handler, exercise)


def test_least_mode_avoids_a_busy_backend_while_serving_other_requests(run_proxy, monkeypatch):
    configure_algorithm(monkeypatch, "least_in_flight")
    monkeypatch.setattr(balancer, "BACKEND_WEIGHTS", {A: 100, B: 1})
    gates = {}

    async def handler(request):
        if request.url.path == "/slow":
            assert request.url.host == "backend-a"
            assert balancer.in_flight == {A: 1}
            gates["started"].set()
            await gates["release"].wait()
        else:
            assert request.url.host == "backend-b"
            assert balancer.in_flight == {A: 1, B: 1}
        return httpx.Response(200, json={"server": request.url.host})

    async def exercise(client):
        gates["started"] = asyncio.Event()
        gates["release"] = asyncio.Event()
        pending = asyncio.create_task(client.get("/slow"))
        try:
            await asyncio.wait_for(gates["started"].wait(), timeout=2.0)
            for _ in range(4):
                response = await asyncio.wait_for(client.get("/fast"), timeout=2.0)
                assert response.json() == {"server": "backend-b"}
                assert balancer.in_flight == {A: 1, B: 0}
            gates["release"].set()
            response = await asyncio.wait_for(pending, timeout=2.0)
            assert response.status_code == 200
            assert balancer.in_flight == {A: 0, B: 0}
        finally:
            gates["release"].set()
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=2.0)

    run_proxy(handler, exercise)


def test_health_changes_do_not_erase_a_still_active_attempt(run_proxy, monkeypatch):
    configure_algorithm(monkeypatch, "least_in_flight")
    gates = {}

    async def handler(request):
        if request.url.path == "/slow":
            assert request.url.host == "backend-a"
            gates["started"].set()
            await gates["release"].wait()
        else:
            assert request.url.host == "backend-b"
            assert balancer.in_flight == {A: 1, B: 1}
        return httpx.Response(200, json={"server": request.url.host})

    async def exercise(client):
        gates["started"] = asyncio.Event()
        gates["release"] = asyncio.Event()
        pending = asyncio.create_task(client.get("/slow"))
        try:
            await asyncio.wait_for(gates["started"].wait(), timeout=2.0)
            proxy.app.state.healthy_backends = [B]
            response = await asyncio.wait_for(client.get("/fast"), timeout=2.0)
            assert response.status_code == 200
            assert balancer.in_flight == {A: 1, B: 0}
            proxy.app.state.healthy_backends = [A, B]
            recovered = await asyncio.wait_for(client.get("/fast"), timeout=2.0)
            assert recovered.json() == {"server": "backend-b"}
            assert balancer.in_flight == {A: 1, B: 0}
            gates["release"].set()
            await asyncio.wait_for(pending, timeout=2.0)
            assert balancer.in_flight == {A: 0, B: 0}
        finally:
            gates["release"].set()
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=2.0)

    run_proxy(handler, exercise)


@pytest.mark.integration
def test_actual_backend_apps_rotate_when_no_attempt_is_pending(
    run_proxy, real_backend_router, monkeypatch,
):
    configure_algorithm(monkeypatch, "least_in_flight")

    async def exercise(client):
        servers = []
        for _ in range(6):
            response = await client.get("/")
            assert response.status_code == 200
            servers.append(response.json()["server"])
            assert all(count == 0 for count in balancer.in_flight.values())
        assert servers == ["A", "B", "A", "B", "A", "B"]

    run_proxy(real_backend_router, exercise)
