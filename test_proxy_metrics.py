import asyncio

import httpx
import pytest

import balancer
import health_checks
import metrics
import proxy
import rate_limiter


A = "http://backend-a"
B = "http://backend-b"
ALGORITHMS = ["round_robin", "weighted_round_robin", "least_in_flight"]


@pytest.fixture(autouse=True)
def configure_backend_metadata(monkeypatch):
    monkeypatch.setattr(proxy, "BACKENDS", [A, B])
    monkeypatch.setattr(balancer, "BACKEND_WEIGHTS", {A: 2, B: 1})


def expected_totals(requests=0, attempts=0, retries=0, rejected=0):
    return {
        "requests_total": requests,
        "backend_attempts_total": attempts,
        "retries_total": retries,
        "rate_limited_total": rejected,
    }


@pytest.mark.parametrize("healthy", [[A, B], []], ids=["healthy", "all-down"])
def test_stats_is_local_and_does_not_change_counters_or_tokens(run_proxy, healthy):
    def handler(request):
        raise AssertionError("Reading proxy stats must not contact a backend")

    async def exercise(client):
        for _ in range(3):
            response = await client.get("/stats")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == {
                **expected_totals(),
                "balancing_algorithm": "round_robin",
                "configured_backends": [A, B],
                "healthy_backends": healthy,
                "in_flight": {A: 0, B: 0},
            }
        assert metrics.get_totals() == expected_totals()
        assert rate_limiter.buckets == {}

    run_proxy(handler, exercise, backends=healthy)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_success_counts_one_request_and_one_attempt(run_proxy, method):
    async def exercise(client):
        assert (await client.request(method, "/example")).status_code == 200
        assert metrics.get_totals() == expected_totals(requests=1, attempts=1)

    run_proxy(lambda request: httpx.Response(200), exercise)


@pytest.mark.parametrize("status_code", [404, 503])
def test_backend_http_error_counts_an_attempt_without_retry(run_proxy, status_code):
    async def exercise(client):
        assert (await client.get("/example")).status_code == status_code
        assert metrics.get_totals() == expected_totals(requests=1, attempts=1)

    run_proxy(lambda request: httpx.Response(status_code), exercise)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_successful_get_retry_counts_one_request_two_attempts_and_one_retry(run_proxy, monkeypatch, algorithm):
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", algorithm)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if len(seen) == 1:
            raise httpx.ConnectError("First backend failed", request=request)
        return httpx.Response(200)

    async def exercise(client):
        assert (await client.get("/")).status_code == 200
        assert metrics.get_totals() == expected_totals(requests=1, attempts=2, retries=1)

    run_proxy(handler, exercise)
    assert len(seen) == 2
    assert len(set(seen)) == 2


def test_exhausted_get_keeps_both_failed_attempts_in_totals(run_proxy):
    def handler(request):
        raise httpx.ReadTimeout("Backend timed out", request=request)

    async def exercise(client):
        assert (await client.get("/")).status_code == 504
        assert metrics.get_totals() == expected_totals(requests=1, attempts=2, retries=1)

    run_proxy(handler, exercise)


def test_missing_alternative_backend_does_not_count_a_retry(run_proxy):
    def handler(request):
        raise httpx.ConnectError("Only backend failed", request=request)

    async def exercise(client):
        assert (await client.get("/")).status_code == 502
        assert metrics.get_totals() == expected_totals(requests=1, attempts=1)

    run_proxy(handler, exercise, backends=[A])


def test_failed_post_still_has_only_one_attempt(run_proxy):
    def handler(request):
        raise httpx.ConnectError("POST connection failed", request=request)

    async def exercise(client):
        assert (await client.post("/echo")).status_code == 502
        assert metrics.get_totals() == expected_totals(requests=1, attempts=1)

    run_proxy(handler, exercise)


def test_no_healthy_backend_counts_the_request_without_an_attempt(run_proxy):
    def handler(request):
        raise AssertionError("No backend should be contacted")

    async def exercise(client):
        assert (await client.get("/")).status_code == 503
        assert metrics.get_totals() == expected_totals(requests=1)

    run_proxy(handler, exercise, backends=[])


def test_rate_limited_request_is_counted_and_stats_remain_available(run_proxy, monkeypatch, rate_clock):
    monkeypatch.setattr(rate_limiter, "CAPACITY", 1)

    async def exercise(client):
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/")).status_code == 429
        expected = expected_totals(requests=2, attempts=1, rejected=1)
        for _ in range(3):
            response = await client.get("/stats")
            assert response.status_code == 200
            assert {key: response.json()[key] for key in expected} == expected
        assert metrics.get_totals() == expected
        assert rate_limiter.buckets["127.0.0.1"]["tokens"] == 0.0

    run_proxy(lambda request: httpx.Response(200), exercise)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_lesson_scenario_has_six_requests_five_attempts_one_retry_two_rejections(
    run_proxy, monkeypatch, rate_clock, algorithm,
):
    monkeypatch.setattr(rate_limiter, "CAPACITY", 4)
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", algorithm)
    state = {"retry_attempts": 0}

    def handler(request):
        if request.url.path == "/retry":
            state["retry_attempts"] += 1
            if state["retry_attempts"] == 1:
                raise httpx.ConnectError("First attempt failed", request=request)
        return httpx.Response(200)

    async def exercise(client):
        for index in range(3):
            assert (await client.get(f"/first-{index}")).status_code == 200
        assert (await client.get("/retry")).status_code == 200
        assert (await client.get("/limited")).status_code == 429
        assert (await client.get("/limited")).status_code == 429
        expected = expected_totals(requests=6, attempts=5, retries=1, rejected=2)
        assert metrics.get_totals() == expected
        snapshot = (await client.get("/stats")).json()
        assert {key: snapshot[key] for key in expected} == expected
        assert snapshot["balancing_algorithm"] == algorithm
        assert snapshot["in_flight"] == {A: 0, B: 0}

    run_proxy(handler, exercise)


@pytest.mark.parametrize("fail_first", [False, True], ids=["first-attempt", "retry"])
def test_shared_deadline_keeps_only_attempts_that_actually_started(run_proxy, monkeypatch, fail_first):
    monkeypatch.setattr(proxy, "FORWARDING_TIMEOUT", 0.02)
    seen = []

    async def handler(request):
        seen.append(request.url.host)
        if fail_first and len(seen) == 1:
            raise httpx.ConnectError("First backend failed", request=request)
        await asyncio.Event().wait()

    async def exercise(client):
        assert (await asyncio.wait_for(client.get("/"), timeout=2.0)).status_code == 504
        assert metrics.get_totals() == expected_totals(
            requests=1, attempts=2 if fail_first else 1, retries=1 if fail_first else 0,
        )
        assert all(count == 0 for count in balancer.get_in_flight().values())

    run_proxy(handler, exercise)


def test_caller_cancellation_counts_the_started_attempt_without_retry(run_proxy):
    gates = {}

    async def handler(request):
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
            assert metrics.get_totals() == expected_totals(requests=1, attempts=1)
            assert balancer.get_in_flight() == {A: 0}
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    run_proxy(handler, exercise)


def test_unexpected_exception_preserves_accounting_and_cleanup(run_proxy):
    def handler(request):
        raise RuntimeError("Unexpected forwarding failure")

    async def exercise(client):
        with pytest.raises(RuntimeError, match="Unexpected forwarding failure"):
            await client.get("/")
        assert metrics.get_totals() == expected_totals(requests=1, attempts=1)
        assert balancer.get_in_flight() == {A: 0}

    run_proxy(handler, exercise)


def test_stats_show_pending_work_even_if_backend_leaves_the_healthy_snapshot(run_proxy):
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
            proxy.app.state.healthy_backends = [B]
            snapshot = (await client.get("/stats")).json()
            assert snapshot["requests_total"] == 1
            assert snapshot["backend_attempts_total"] == 1
            assert snapshot["configured_backends"] == [A, B]
            assert snapshot["healthy_backends"] == [B]
            assert snapshot["in_flight"] == {A: 1, B: 0}
            gates["release"].set()
            assert (await asyncio.wait_for(pending, timeout=2.0)).status_code == 200
            assert (await client.get("/stats")).json()["in_flight"] == {A: 0, B: 0}
            assert metrics.get_totals() == expected_totals(requests=1, attempts=1)
        finally:
            gates["release"].set()
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), timeout=2.0)

    run_proxy(handler, exercise)


def test_outbound_health_scans_do_not_count_as_incoming_requests():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            trust_env=False,
        ) as client:
            assert await health_checks.get_healthy_backends(client, [A, B]) == [A, B]
        assert metrics.get_totals() == expected_totals()

    asyncio.run(scenario())


def test_unsupported_method_does_not_enter_the_forwarding_counters(run_proxy):
    def handler(request):
        raise AssertionError("Unsupported method must not be forwarded")

    async def exercise(client):
        assert (await client.put("/example")).status_code == 405
        assert metrics.get_totals() == expected_totals()

    run_proxy(handler, exercise)


def test_only_get_stats_is_local_while_post_stats_is_forwarded(run_proxy):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/stats"
        return httpx.Response(201)

    async def exercise(client):
        assert (await client.post("/stats")).status_code == 201
        assert metrics.get_totals() == expected_totals(requests=1, attempts=1)

    run_proxy(handler, exercise)


def test_client_following_redirect_creates_a_second_incoming_request(run_proxy):
    def handler(request):
        if request.url.path == "/go":
            return httpx.Response(302, headers={"location": "/items/42"})
        return httpx.Response(200)

    async def exercise(client):
        assert (await client.get("/go", follow_redirects=True)).status_code == 200
        assert metrics.get_totals() == expected_totals(requests=2, attempts=2)

    run_proxy(handler, exercise)
