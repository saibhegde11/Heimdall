import asyncio

import httpx
import pytest
from fastapi import HTTPException, Request

import balancer
import health_checks
import proxy
import rate_limiter as limiter


ALGORITHMS = ["round_robin", "weighted_round_robin", "least_in_flight"]
CLIENT_IP = "127.0.0.1"
A = "http://backend-a"
B = "http://backend-b"


@pytest.fixture
def one_token(monkeypatch, rate_clock):
    monkeypatch.setattr(limiter, "CAPACITY", 1)
    return rate_clock


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_rejection_does_not_contact_or_reserve_a_backend(run_proxy, monkeypatch, one_token, algorithm):
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", algorithm)
    monkeypatch.setattr(balancer, "BACKEND_WEIGHTS", {A: 2, B: 1})
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, json={"server": request.url.host})

    async def exercise(client):
        assert (await client.get("/")).status_code == 200
        before = (
            balancer.next_index, balancer.least_next_index,
            balancer.current_scores.copy(), balancer.in_flight.copy(),
        )
        for _ in range(2):
            rejected = await client.get("/")
            assert rejected.status_code == 429
            assert rejected.json() == {"detail": "Too Many Requests"}
            assert rejected.headers["retry-after"] == "1"
            assert rejected.headers["cache-control"] == "no-store"
        assert before == (
            balancer.next_index, balancer.least_next_index,
            balancer.current_scores, balancer.in_flight,
        )

    run_proxy(handler, exercise)
    assert len(seen) == 1


@pytest.mark.parametrize("first_method", ["GET", "POST"])
def test_methods_and_paths_share_the_same_client_bucket(run_proxy, one_token, first_method):
    def handler(request):
        return httpx.Response(200)

    async def exercise(client):
        assert (await client.request(first_method, "/first?q=1")).status_code == 200
        other_method = "POST" if first_method == "GET" else "GET"
        assert (await client.request(other_method, "/different?q=2")).status_code == 429

    run_proxy(handler, exercise)


@pytest.mark.parametrize("refill_rate, expected_header", [(2.0, "1"), (0.4, "3"), (0.25, "4")])
def test_retry_after_is_a_whole_number_rounded_up(run_proxy, monkeypatch, one_token, refill_rate, expected_header):
    monkeypatch.setattr(limiter, "REFILL_RATE", refill_rate)

    def handler(request):
        return httpx.Response(200)

    async def exercise(client):
        assert (await client.get("/")).status_code == 200
        rejected = await client.get("/")
        assert rejected.status_code == 429
        assert rejected.headers["retry-after"] == expected_header

    run_proxy(handler, exercise)


def test_request_is_accepted_after_enough_refill_time(run_proxy, one_token):
    def handler(request):
        return httpx.Response(200)

    async def exercise(client):
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/")).status_code == 429
        one_token["now"] = 1000.2
        assert (await client.get("/")).status_code == 429
        assert limiter.buckets[CLIENT_IP]["tokens"] == pytest.approx(0.4)
        one_token["now"] = 1000.5
        assert (await client.get("/")).status_code == 200
        assert limiter.buckets[CLIENT_IP]["tokens"] == 0.0

    run_proxy(handler, exercise)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_get_retry_uses_one_client_token_for_two_backend_attempts(
    run_proxy, monkeypatch, one_token, algorithm,
):
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", algorithm)
    monkeypatch.setattr(balancer, "BACKEND_WEIGHTS", {A: 2, B: 1})
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "backend-a":
            raise httpx.ConnectError("A stopped", request=request)
        return httpx.Response(200, json={"server": "B"})

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 200
        assert response.json() == {"server": "B"}
        assert limiter.buckets[CLIENT_IP]["tokens"] == 0.0
        assert balancer.in_flight == {A: 0, B: 0}
        assert (await client.get("/")).status_code == 429

    run_proxy(handler, exercise)
    assert seen == ["backend-a", "backend-b"]


def test_backend_http_error_does_not_refund_the_client_token(run_proxy, one_token):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(503, json={"detail": "Backend overloaded"})

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 503
        assert response.json() == {"detail": "Backend overloaded"}
        assert limiter.buckets[CLIENT_IP]["tokens"] == 0.0
        assert (await client.get("/")).status_code == 429

    run_proxy(handler, exercise)
    assert seen == ["backend-a"]


def test_no_healthy_backend_still_spends_the_admitted_request_token(run_proxy, one_token):
    def handler(request):
        raise AssertionError("No backend should be contacted")

    async def exercise(client):
        assert (await client.get("/")).status_code == 503
        assert (await client.get("/")).status_code == 429
        assert balancer.in_flight == {}
        assert limiter.buckets[CLIENT_IP]["tokens"] == 0.0

    run_proxy(handler, exercise, backends=[])


def test_rejection_does_not_release_another_requests_pending_reservation(run_proxy, one_token):
    gates = {}

    async def handler(request):
        gates["started"].set()
        await gates["release"].wait()
        return httpx.Response(200)

    async def exercise(client):
        gates["started"] = asyncio.Event()
        gates["release"] = asyncio.Event()
        pending = asyncio.create_task(client.get("/slow"))
        try:
            await asyncio.wait_for(gates["started"].wait(), timeout=2.0)
            assert balancer.in_flight == {A: 1}
            assert (await client.get("/fast")).status_code == 429
            assert balancer.in_flight == {A: 1}
            gates["release"].set()
            assert (await asyncio.wait_for(pending, timeout=2.0)).status_code == 200
            assert balancer.in_flight == {A: 0}
            assert (await client.get("/fast")).status_code == 429
        finally:
            gates["release"].set()
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=2.0)

    run_proxy(handler, exercise)


def test_cancelled_forwarding_does_not_refund_the_admission_token(run_proxy, one_token):
    gates = {}
    seen = []

    async def handler(request):
        seen.append(request.url.host)
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
            assert limiter.buckets[CLIENT_IP]["tokens"] == 0.0
            assert (await client.get("/")).status_code == 429
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    run_proxy(handler, exercise)
    assert seen == ["backend-a"]


def test_different_ips_have_separate_buckets_and_changing_ports_does_not_reset_one(run_proxy, one_token):
    def handler(request):
        return httpx.Response(200)

    async def exercise(client):
        assert (await client.get("/")).status_code == 200
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=proxy.app, client=("192.0.2.1", 54321)),
                base_url="http://heimdall.test", trust_env=False,
            ) as same_ip_new_port,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=proxy.app, client=("198.51.100.1", 12345)),
                base_url="http://heimdall.test", trust_env=False,
            ) as other_ip,
        ):
            assert (await same_ip_new_port.get("/")).status_code == 429
            assert (await other_ip.get("/")).status_code == 200
        assert set(limiter.buckets) == {"192.0.2.1", "198.51.100.1"}

    run_proxy(handler, exercise, client_address=("192.0.2.1", 12345))


def test_application_does_not_take_its_client_key_from_forwarded_headers(run_proxy, one_token):
    def handler(request):
        return httpx.Response(200)

    async def exercise(client):
        assert (await client.get("/", headers={"X-Forwarded-For": "192.0.2.1"})).status_code == 200
        rejected = await client.get("/", headers={"X-Forwarded-For": "198.51.100.1"})
        assert rejected.status_code == 429
        assert set(limiter.buckets) == {CLIENT_IP}

    run_proxy(handler, exercise)


def test_absent_asgi_client_uses_a_shared_unknown_bucket(run_proxy, one_token):
    def handler(request):
        return httpx.Response(200)

    async def exercise(client):
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/")).status_code == 429
        assert set(limiter.buckets) == {"unknown"}

    run_proxy(handler, exercise, client_address=None)


def test_rejected_request_body_is_not_read(one_token):
    assert limiter.allow_request("192.0.2.1") == (True, 0.0)

    async def reject_receive():
        raise AssertionError("Rejected request body must not be read")

    async def scenario():
        request = Request({
            "type": "http", "asgi": {"version": "3.0"},
            "method": "POST", "path": "/echo", "raw_path": b"/echo",
            "query_string": b"", "headers": [], "scheme": "http",
            "server": ("heimdall.test", 80), "client": ("192.0.2.1", 12345),
        }, receive=reject_receive)
        with pytest.raises(HTTPException) as raised:
            await proxy.fetch_from_backend("/echo", request)
        assert raised.value.status_code == 429
        assert balancer.in_flight == {}

    asyncio.run(scenario())


def test_outbound_health_probes_do_not_spend_client_tokens(rate_clock):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            trust_env=False,
        ) as health_client:
            assert await health_checks.is_backend_healthy(health_client, A) is True
        assert limiter.buckets == {}

    asyncio.run(scenario())


@pytest.mark.integration
def test_real_backend_app_is_called_only_for_admitted_requests(
    run_proxy, real_backend_router, one_token,
):
    async def exercise(client):
        accepted = await client.get("/items/42")
        assert accepted.status_code == 200
        assert accepted.json() == {"server": "A", "item_id": 42}
        assert (await client.get("/items/42")).status_code == 429
        one_token["now"] = 1000.5
        recovered = await client.get("/items/42")
        assert recovered.status_code == 200
        assert recovered.json() == {"server": "B", "item_id": 42}

    run_proxy(real_backend_router, exercise)
