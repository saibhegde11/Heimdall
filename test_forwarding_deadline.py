import asyncio

import httpx
import pytest

import proxy


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_shared_forwarding_deadline_cancels_a_waiting_attempt(run_proxy, monkeypatch, method):
    monkeypatch.setattr(proxy, "FORWARDING_TIMEOUT", 0.02)
    seen = []
    state = {"cancelled": False}

    async def handler(request):
        seen.append(request.url.host)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def exercise(client):
        response = await asyncio.wait_for(client.request(method, "/"), timeout=2.0)
        assert response.status_code == 504
        assert response.json() == {"detail": "Backend Request Timed Out"}

    run_proxy(handler, exercise)
    assert state["cancelled"] is True
    assert seen == ["backend-a"]



def test_forwarding_deadline_also_covers_a_waiting_retry(run_proxy, monkeypatch):
    monkeypatch.setattr(proxy, "FORWARDING_TIMEOUT", 0.02)
    seen = []
    state = {"retry_cancelled": False}

    async def handler(request):
        seen.append(request.url.host)
        if request.url.host == "backend-a":
            raise httpx.ConnectError("First backend failed", request=request)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["retry_cancelled"] = True
            raise

    async def exercise(client):
        response = await asyncio.wait_for(client.get("/"), timeout=2.0)
        assert response.status_code == 504
        assert response.json() == {"detail": "Backend Request Timed Out"}

    run_proxy(handler, exercise)
    assert seen == ["backend-a", "backend-b"]
    assert state["retry_cancelled"] is True

