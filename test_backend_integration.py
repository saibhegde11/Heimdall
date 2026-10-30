import asyncio

import httpx
import pytest

from health_checks import get_healthy_backends

pytestmark = pytest.mark.integration


def test_both_real_backend_health_routes_pass(real_backend_router):
    backends = ["http://backend-a", "http://backend-b"]

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(real_backend_router), trust_env=False,
        ) as client:
            assert await get_healthy_backends(client, backends) == backends

    asyncio.run(scenario())


def test_real_root_and_item_routes_alternate_through_proxy(run_proxy, real_backend_router):
    async def exercise(client):
        for path in ["/", "/items/42"]:
            results = [await client.get(path) for _ in range(2)]
            assert [response.status_code for response in results] == [200, 200]
            assert [response.json()["server"] for response in results] == ["A", "B"]
            if path == "/items/42":
                assert [response.json()["item_id"] for response in results] == [42, 42]

    run_proxy(real_backend_router, exercise)


def test_real_search_route_receives_repeated_query_parameters(run_proxy, real_backend_router):
    async def exercise(client):
        for server in ["A", "B"]:
            response = await client.get("/search?q=python&tag=backend&tag=fastapi")
            assert response.status_code == 200
            assert response.json() == {
                "server": server,
                "query": [["q", "python"], ["tag", "backend"], ["tag", "fastapi"]],
            }

    run_proxy(real_backend_router, exercise)


def test_real_echo_route_receives_post_body_and_returns_201(run_proxy, real_backend_router):
    async def exercise(client):
        for server in ["A", "B"]:
            response = await client.post(
                "/echo?source=practice", json={"message": "hello", "count": 2},
            )
            assert response.status_code == 201
            assert response.json() == {
                "server": server,
                "method": "POST",
                "content_type": "application/json",
                "query": [["source", "practice"]],
                "received": {"message": "hello", "count": 2},
            }

    run_proxy(real_backend_router, exercise)


def test_real_backend_redirect_can_be_followed_by_the_client(run_proxy, real_backend_router):
    async def exercise(client):
        response = await client.get("/go", follow_redirects=True)
        assert [item.status_code for item in response.history] == [302]
        assert response.history[0].headers["location"] == "/items/42"
        assert response.status_code == 200
        assert response.json() == {"server": "B", "item_id": 42}

    run_proxy(real_backend_router, exercise)


def test_real_backend_404_and_validation_422_are_preserved(run_proxy, real_backend_router):
    async def exercise(client):
        missing = await client.get("/missing")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Not Found"}
        invalid = await client.get("/items/not-an-integer")
        assert invalid.status_code == 422
        assert isinstance(invalid.json()["detail"], list)

    run_proxy(real_backend_router, exercise)
