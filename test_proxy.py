import asyncio
import json

import httpx
import pytest


def test_requests_alternate_without_running_health_checks_per_request(run_proxy):
    seen = []

    def handler(request):
        seen.append((request.url.host, request.url.path))
        return httpx.Response(200, json={"server": request.url.host})

    async def exercise(client):
        servers = []
        for _ in range(6):
            response = await client.get("/")
            assert response.status_code == 200
            servers.append(response.json()["server"])
        assert servers == ["backend-a", "backend-b"] * 3

    run_proxy(handler, exercise)
    assert seen == [(name, "/") for name in ["backend-a", "backend-b"] * 3]


def test_only_the_healthy_backend_receives_requests(run_proxy):
    def handler(request):
        assert request.url.host == "backend-b"
        return httpx.Response(200, json={"server": "B"})

    async def exercise(client):
        for _ in range(3):
            response = await client.get("/")
            assert response.status_code == 200
            assert response.json() == {"server": "B"}

    run_proxy(handler, exercise, backends=["http://backend-b"])


def test_no_healthy_backends_returns_503_without_contacting_a_backend(run_proxy):
    def handler(request):
        pytest.fail("No backend should be contacted when the candidate list is empty")

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 503
        assert response.json() == {"detail": "No healthy backends available"}

    run_proxy(handler, exercise, backends=[])


@pytest.mark.parametrize("path", ["/", "/items/42", "/nested/path", "/nested/path/"])
def test_get_preserves_the_path(run_proxy, path):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == path
        return httpx.Response(200, content=b"ok")

    async def exercise(client):
        response = await client.get(path)
        assert response.status_code == 200
        assert response.content == b"ok"

    run_proxy(handler, exercise)


def test_query_parameters_keep_duplicates_blank_values_and_encoded_values(run_proxy):
    expected = [
        ("q", "python"), ("page", "2"), ("tag", "backend"),
        ("tag", "fastapi"), ("empty", ""), ("label", "a&b c"),
    ]

    def handler(request):
        assert request.url.params.multi_items() == expected
        return httpx.Response(200, json={"ok": True})

    async def exercise(client):
        response = await client.get(
            "/search?q=python&page=2&tag=backend&tag=fastapi&empty=&label=a%26b%20c"
        )
        assert response.status_code == 200

    run_proxy(handler, exercise)


def test_post_preserves_json_bytes_method_content_type_and_query(run_proxy):
    body = b'{"message": "hello", "count": 2}'

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/echo"
        assert request.url.params.multi_items() == [("source", "practice")]
        assert request.headers["content-type"] == "application/json"
        assert request.content == body
        return httpx.Response(201, json={"received": json.loads(request.content)})

    async def exercise(client):
        response = await client.post(
            "/echo?source=practice", content=body,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 201
        assert response.json() == {"received": {"message": "hello", "count": 2}}

    run_proxy(handler, exercise)


@pytest.mark.parametrize(
    "content_type, body",
    [
        ("text/plain; charset=utf-8", "hello, नमस्ते".encode("utf-8")),
        ("application/octet-stream", bytes([0, 255, 128, 10, 65])),
        ("application/x-www-form-urlencoded", b"tag=backend&tag=fastapi&empty="),
    ],
    ids=["text", "binary", "form"],
)
def test_post_forwards_non_json_bodies_without_parsing_them(run_proxy, content_type, body):
    def handler(request):
        assert request.method == "POST"
        assert request.headers["content-type"] == content_type
        assert request.content == body
        return httpx.Response(201, content=b"accepted")

    async def exercise(client):
        response = await client.post(
            "/upload", content=body, headers={"content-type": content_type},
        )
        assert response.status_code == 201
        assert response.content == b"accepted"

    run_proxy(handler, exercise)


def test_a_missing_request_content_type_is_not_invented(run_proxy):
    def handler(request):
        assert "content-type" not in request.headers
        assert request.content == b"untyped bytes"
        return httpx.Response(200)

    async def exercise(client):
        response = await client.post("/raw", content=b"untyped bytes")
        assert response.status_code == 200

    run_proxy(handler, exercise)


@pytest.mark.parametrize("status", [201, 404, 500, 503, 504])
def test_backend_status_body_and_content_type_are_preserved(run_proxy, status):
    body = json.dumps({"from": "backend", "status": status}).encode()

    def handler(request):
        return httpx.Response(
            status, content=body, headers={"content-type": "application/json"},
        )

    async def exercise(client):
        response = await client.get("/backend-response")
        assert response.status_code == status
        assert response.content == body
        assert response.headers["content-type"] == "application/json"

    run_proxy(handler, exercise)


def test_binary_responses_are_forwarded_without_json_conversion(run_proxy):
    body = bytes([0, 128, 255, 10, 65])

    def handler(request):
        return httpx.Response(
            200, content=body, headers={"content-type": "application/octet-stream"},
        )

    async def exercise(client):
        response = await client.get("/download")
        assert response.status_code == 200
        assert response.content == body
        assert response.headers["content-type"] == "application/octet-stream"
        assert int(response.headers["content-length"]) == len(body)

    run_proxy(handler, exercise)


def test_an_empty_204_response_is_preserved(run_proxy):
    async def exercise(client):
        response = await client.get("/empty")
        assert response.status_code == 204
        assert response.content == b""

    run_proxy(lambda request: httpx.Response(204), exercise)


def test_redirect_status_and_location_are_returned_without_proxy_following(run_proxy):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(302, headers={"location": "/items/42"})

    async def exercise(client):
        response = await client.get("/go")
        assert response.status_code == 302
        assert response.headers["location"] == "/items/42"
        assert response.content == b""

    run_proxy(handler, exercise)
    assert seen == ["/go"]


def test_client_following_a_relative_redirect_makes_a_new_balanced_request(run_proxy):
    seen = []

    def handler(request):
        seen.append((request.url.host, request.url.path))
        if request.url.path == "/go":
            return httpx.Response(302, headers={"location": "/items/42"})
        return httpx.Response(200, json={"server": request.url.host, "item_id": 42})

    async def exercise(client):
        response = await client.get("/go", follow_redirects=True)
        assert response.status_code == 200
        assert len(response.history) == 1
        assert response.json() == {"server": "backend-b", "item_id": 42}

    run_proxy(handler, exercise)
    assert seen == [("backend-a", "/go"), ("backend-b", "/items/42")]


@pytest.mark.parametrize(
    "error_type",
    [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout],
    ids=["connect", "read", "write", "pool"],
)
def test_timeout_failures_return_controlled_504(run_proxy, error_type):
    def handler(request):
        raise error_type("Simulated timeout", request=request)

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 504
        assert response.json() == {"detail": "Backend Request Timed Out"}

    run_proxy(handler, exercise)


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError],
    ids=["connection", "read", "protocol"],
)
def test_other_transport_failures_return_controlled_502(run_proxy, error_type):
    def handler(request):
        raise error_type("Simulated transport failure", request=request)

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 502
        assert response.json() == {"detail": "Backend Request Failed"}

    run_proxy(handler, exercise)


@pytest.mark.parametrize(
    "error_type, expected_status", [(httpx.ReadError, 502), (httpx.ReadTimeout, 504)],
    ids=["connection-lost", "response-timeout"],
)
def test_a_post_is_not_blindly_retried_after_an_ambiguous_failure(
    run_proxy, error_type, expected_status,
):
    seen = []

    def handler(request):
        seen.append((request.url.host, request.content))
        raise error_type("The backend may already have processed the body", request=request)

    async def exercise(client):
        response = await client.post("/create", json={"order": 42})
        assert response.status_code == expected_status

    run_proxy(handler, exercise)
    assert len(seen) == 1
    assert seen[0][0] == "backend-a"


def test_unimplemented_delete_method_returns_405_without_contacting_a_backend(run_proxy):
    def handler(request):
        pytest.fail("Only GET and POST are currently implemented by the proxy route")

    async def exercise(client):
        response = await client.delete("/items/42")
        assert response.status_code == 405

    run_proxy(handler, exercise)


def test_a_waiting_request_does_not_block_a_second_request(run_proxy):
    gates = {}

    async def handler(request):
        if request.url.host == "backend-a":
            gates["started"].set()
            await gates["release"].wait()
        return httpx.Response(200, json={"server": request.url.host})

    async def exercise(client):
        waiting_started = asyncio.Event()
        release_waiting = asyncio.Event()
        gates["started"] = waiting_started
        gates["release"] = release_waiting
        waiting_request = asyncio.create_task(client.get("/slow"))
        try:
            await asyncio.wait_for(waiting_started.wait(), timeout=2.0)
            quick_response = await asyncio.wait_for(client.get("/fast"), timeout=2.0)
            assert quick_response.json() == {"server": "backend-b"}
            assert not waiting_request.done()
        finally:
            release_waiting.set()
            slow_response = await asyncio.wait_for(waiting_request, timeout=2.0)
        assert slow_response.json() == {"server": "backend-a"}

    run_proxy(handler, exercise)
