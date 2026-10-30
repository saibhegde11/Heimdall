import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

import metrics
import proxy
import rate_limiter


def events(caplog):
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == proxy.logger.name
    ]


def test_backend_response_log_has_identity_status_attempt_and_duration(run_proxy, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=proxy.logger.name)
    readings = iter([100.0, 100.125])
    monkeypatch.setattr(proxy, "time", SimpleNamespace(perf_counter=lambda: next(readings)))
    monkeypatch.setattr(proxy, "uuid", SimpleNamespace(
        uuid4=lambda: SimpleNamespace(hex="a" * 32),
    ))

    async def exercise(client):
        assert (await client.get("/example")).status_code == 200

    run_proxy(lambda request: httpx.Response(200), exercise)
    assert events(caplog) == [{
        "event": "backend_response",
        "request_id": "a" * 32,
        "method": "GET",
        "backend": "http://backend-a",
        "attempt": 1,
        "status_code": 200,
        "duration_ms": 125.0,
    }]


def test_failed_attempt_and_retry_share_identity_and_have_distinct_results(run_proxy, caplog):
    caplog.set_level(logging.INFO, logger=proxy.logger.name)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if len(seen) == 1:
            raise httpx.ReadTimeout("Response missing", request=request)
        return httpx.Response(200)

    async def exercise(client):
        assert (await client.get("/")).status_code == 200

    run_proxy(handler, exercise)
    logged = events(caplog)
    assert [event["event"] for event in logged] == ["backend_request_failed", "backend_response"]
    assert logged[0]["request_id"] == logged[1]["request_id"]
    assert logged[0]["attempt"] == 1
    assert logged[1]["attempt"] == 2
    assert logged[0]["backend"] != logged[1]["backend"]
    assert logged[0]["error_type"] == "ReadTimeout"
    assert "status_code" not in logged[0]
    assert logged[1]["status_code"] == 200
    own_records = [record for record in caplog.records if record.name == proxy.logger.name]
    assert [record.levelno for record in own_records] == [logging.WARNING, logging.INFO]
    assert all(event["duration_ms"] >= 0 for event in logged)


def test_backend_http_error_is_logged_as_a_response_without_retry(run_proxy, caplog):
    caplog.set_level(logging.INFO, logger=proxy.logger.name)

    async def exercise(client):
        assert (await client.get("/")).status_code == 503

    run_proxy(lambda request: httpx.Response(503), exercise)
    logged = events(caplog)
    assert len(logged) == 1
    assert logged[0]["event"] == "backend_response"
    assert logged[0]["status_code"] == 503


def test_own_events_omit_query_body_headers_and_exception_message(run_proxy, caplog):
    caplog.set_level(logging.INFO, logger=proxy.logger.name)
    secrets = ["query-secret-123", "body-secret-456", "header-secret-789", "error-secret-000"]

    def handler(request):
        raise httpx.ConnectError(secrets[3], request=request)

    async def exercise(client):
        response = await client.post(
            "/echo?token=" + secrets[0],
            content=secrets[1],
            headers={"authorization": "Bearer " + secrets[2]},
        )
        assert response.status_code == 502

    run_proxy(handler, exercise)
    own_messages = "\n".join(
        record.getMessage() for record in caplog.records if record.name == proxy.logger.name
    )
    assert own_messages
    assert all(secret not in own_messages for secret in secrets)
    assert [event["event"] for event in events(caplog)] == [
        "backend_request_failed", "forwarding_failed",
    ]


def test_deadline_logs_cancelled_attempt_then_forwarding_timeout(run_proxy, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=proxy.logger.name)
    monkeypatch.setattr(proxy, "FORWARDING_TIMEOUT", 0.02)

    async def handler(request):
        await asyncio.Event().wait()

    async def exercise(client):
        assert (await asyncio.wait_for(client.get("/"), timeout=2.0)).status_code == 504

    run_proxy(handler, exercise)
    logged = events(caplog)
    assert [event["event"] for event in logged] == [
        "backend_attempt_cancelled", "forwarding_deadline_exceeded",
    ]
    assert logged[0]["request_id"] == logged[1]["request_id"]
    assert logged[1]["status_code"] == 504
    assert logged[1]["timeout_seconds"] == 0.02


def test_caller_cancellation_is_not_logged_as_a_deadline_or_retry(run_proxy, caplog):
    caplog.set_level(logging.INFO, logger=proxy.logger.name)
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
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    run_proxy(handler, exercise)
    assert [event["event"] for event in events(caplog)] == ["backend_attempt_cancelled"]


def test_no_healthy_backend_has_a_proxy_unavailable_event(run_proxy, caplog):
    caplog.set_level(logging.INFO, logger=proxy.logger.name)

    def handler(request):
        raise AssertionError("No backend should be contacted")

    async def exercise(client):
        assert (await client.get("/")).status_code == 503

    run_proxy(handler, exercise, backends=[])
    logged = events(caplog)
    assert len(logged) == 1
    assert logged[0]["event"] == "forwarding_unavailable"
    assert logged[0]["status_code"] == 503
    assert "backend" not in logged[0]


def test_rate_limited_event_has_its_own_request_identity(run_proxy, monkeypatch, rate_clock, caplog):
    caplog.set_level(logging.INFO, logger=proxy.logger.name)
    monkeypatch.setattr(rate_limiter, "CAPACITY", 1)
    identifiers = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(proxy, "uuid", SimpleNamespace(
        uuid4=lambda: SimpleNamespace(hex=next(identifiers)),
    ))

    async def exercise(client):
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/")).status_code == 429

    run_proxy(lambda request: httpx.Response(200), exercise)
    logged = events(caplog)
    assert [event["event"] for event in logged] == ["backend_response", "rate_limited"]
    assert logged[0]["request_id"] == "a" * 32
    assert logged[1]["request_id"] == "b" * 32
    assert logged[1]["retry_after_seconds"] == 1
    assert logged[1]["status_code"] == 429


def test_unexpected_exception_is_logged_and_still_propagates(run_proxy, caplog):
    caplog.set_level(logging.INFO, logger=proxy.logger.name)

    def handler(request):
        raise RuntimeError("unexpected-private-message")

    async def exercise(client):
        with pytest.raises(RuntimeError, match="unexpected-private-message"):
            await client.get("/")

    run_proxy(handler, exercise)
    logged = events(caplog)
    assert len(logged) == 1
    assert logged[0]["event"] == "backend_unexpected_error"
    assert logged[0]["error_type"] == "RuntimeError"
    assert "unexpected-private-message" not in str(logged)
    own_records = [record for record in caplog.records if record.name == proxy.logger.name]
    assert own_records[0].levelno == logging.ERROR


def test_health_snapshot_change_is_logged_once_and_does_not_increment_request_counters(
    run_proxy, monkeypatch, caplog,
):
    caplog.set_level(logging.INFO, logger=proxy.logger.name)
    monkeypatch.setattr(proxy, "BACKENDS", ["http://backend-a", "http://backend-b"])
    state = {"sleeps": 0}

    async def controlled_sleep(delay):
        state["sleeps"] += 1
        if state["sleeps"] == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(proxy, "asyncio", SimpleNamespace(
        sleep=controlled_sleep,
        create_task=asyncio.create_task,
        CancelledError=asyncio.CancelledError,
        timeout=asyncio.timeout,
    ))

    def handler(request):
        return httpx.Response(200 if request.url.host == "backend-b" else 503)

    async def exercise(client):
        with pytest.raises(asyncio.CancelledError):
            await proxy.monitor_backends(proxy.app, proxy.app.state.http_client)
        assert proxy.app.state.healthy_backends == ["http://backend-b"]
        assert all(value == 0 for value in metrics.get_totals().values())

    run_proxy(handler, exercise)
    assert events(caplog) == [{
        "event": "health_snapshot_changed",
        "healthy_backends": ["http://backend-b"],
    }]


def test_suppressing_info_logs_preserves_forwarding_and_counters(run_proxy, caplog):
    caplog.set_level(logging.ERROR, logger=proxy.logger.name)

    async def exercise(client):
        assert (await client.get("/")).status_code == 200
        assert metrics.get_totals()["requests_total"] == 1
        assert metrics.get_totals()["backend_attempts_total"] == 1

    run_proxy(lambda request: httpx.Response(200), exercise)
    assert events(caplog) == []
