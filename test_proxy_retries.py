import asyncio
from contextlib import suppress

import httpx
import pytest

import proxy


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError],
    ids=["connect-error", "read-timeout", "read-error"],
)
def test_get_failure_tries_a_different_backend_and_preserves_the_request(run_proxy, error_type):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        assert request.method == "GET"
        assert request.url.path == "/search"
        assert request.url.params.multi_items() == [("tag", "backend"), ("tag", "fastapi")]
        if request.url.host == "backend-a":
            raise error_type("First attempt failed", request=request)
        return httpx.Response(200, json={"server": "B"})

    async def exercise(client):
        response = await client.get("/search?tag=backend&tag=fastapi")
        assert response.status_code == 200
        assert response.json() == {"server": "B"}

    run_proxy(handler, exercise)
    assert seen == ["backend-a", "backend-b"]


def test_get_makes_at_most_two_attempts_even_with_three_eligible_backends(run_proxy):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        raise httpx.ConnectError("Unavailable", request=request)

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 502

    run_proxy(handler, exercise, backends=[
        "http://backend-a", "http://backend-b", "http://backend-c",
    ])
    assert len(seen) == 2
    assert len(set(seen)) == 2


@pytest.mark.parametrize(
    "error_type, status", [(httpx.ConnectError, 502), (httpx.ReadTimeout, 504)],
    ids=["connection", "timeout"],
)
def test_a_single_failed_candidate_is_not_tried_twice(run_proxy, error_type, status):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        raise error_type("Only candidate failed", request=request)

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == status

    run_proxy(handler, exercise, backends=["http://backend-a"])
    assert seen == ["backend-a"]


def test_successful_first_attempt_does_not_contact_another_backend(run_proxy):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, json={"server": "A"})

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 200
        assert response.json() == {"server": "A"}

    run_proxy(handler, exercise)
    assert seen == ["backend-a"]


@pytest.mark.parametrize("status", [404, 500, 503, 504])
def test_backend_error_responses_are_relayed_without_retry(run_proxy, status):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(status, json={"from": "backend", "status": status})

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == status
        assert response.json() == {"from": "backend", "status": status}

    run_proxy(handler, exercise)
    assert seen == ["backend-a"]


def test_post_connection_failure_is_not_retried(run_proxy):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        raise httpx.ConnectError("Connection failed", request=request)

    async def exercise(client):
        response = await client.post("/echo", json={"count": 2})
        assert response.status_code == 502

    run_proxy(handler, exercise)
    assert seen == ["backend-a"]


@pytest.mark.parametrize(
    "first_error, last_error, status, detail",
    [
        (httpx.ReadTimeout, httpx.ConnectError, 502, "Backend Request Failed"),
        (httpx.ConnectError, httpx.ReadTimeout, 504, "Backend Request Timed Out"),
    ],
    ids=["last-connection-error", "last-timeout"],
)
def test_exhausted_retries_report_the_last_failure(
    run_proxy, first_error, last_error, status, detail,
):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        error_type = first_error if len(seen) == 1 else last_error
        raise error_type("Attempt failed", request=request)

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == status
        assert response.json() == {"detail": detail}

    run_proxy(handler, exercise)
    assert seen == ["backend-a", "backend-b"]


def test_no_new_candidate_after_a_failure_keeps_the_failure_status(run_proxy):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        proxy.app.state.healthy_backends = []
        raise httpx.ConnectError("Failed while health information changed", request=request)

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 502
        assert response.json() == {"detail": "Backend Request Failed"}

    run_proxy(handler, exercise)
    assert seen == ["backend-a"]


def test_retry_can_use_a_backend_that_has_just_recovered(run_proxy):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "backend-a":
            proxy.app.state.healthy_backends = ["http://backend-a", "http://backend-b"]
            raise httpx.ConnectError("First backend failed", request=request)
        return httpx.Response(200, json={"server": "B"})

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 200
        assert response.json() == {"server": "B"}

    run_proxy(handler, exercise, backends=["http://backend-a"])
    assert seen == ["backend-a", "backend-b"]


def test_duplicate_candidate_urls_are_not_retried(run_proxy):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        raise httpx.ConnectError("Unavailable", request=request)

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 502

    run_proxy(handler, exercise, backends=["http://backend-a", "http://backend-a"])
    assert seen == ["backend-a"]




def test_caller_cancellation_is_not_converted_into_a_retry(run_proxy):
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
        finally:
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending

    run_proxy(handler, exercise)
    assert seen == ["backend-a"]


