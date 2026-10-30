import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

import config


PROJECT_DIR = Path(__file__).resolve().parent


def run_child(tmp_path, code, config_path):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_DIR)
    if config_path is None:
        environment.pop("HEIMDALL_CONFIG", None)
    else:
        environment["HEIMDALL_CONFIG"] = str(config_path)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )


@pytest.mark.integration
def test_custom_config_controls_real_proxy_components_in_a_fresh_process(tmp_path):
    path = tmp_path / "custom.json"
    path.write_text(json.dumps({
        "backends": [
            {"url": "http://backend-a:9001/", "weight": 2},
            {"url": "http://backend-b:9002/", "weight": 1},
        ],
        "balancing_algorithm": "weighted_round_robin",
        "backend_timeout": 0.4,
        "health_check_timeout": 0.2,
        "health_check_interval": 0.1,
        "forwarding_timeout": 0.8,
        "rate_limit_capacity": 7,
        "rate_limit_refill_rate": 1.5,
    }), encoding="utf-8")

    result = run_child(tmp_path, r'''
        import asyncio
        import json
        import os
        from pathlib import Path
        from types import SimpleNamespace

        import httpx

        selected_path = Path(os.environ["HEIMDALL_CONFIG"])
        original_read = Path.read_text
        reads = []

        def tracked_read(self, *args, **kwargs):
            if self == selected_path:
                reads.append(str(self))
            return original_read(self, *args, **kwargs)

        Path.read_text = tracked_read

        async def reject_async_network(self, request):
            raise AssertionError("Configuration tests must not use real HTTP")

        def reject_sync_network(self, request):
            raise AssertionError("Configuration tests must not use real HTTP")

        httpx.AsyncHTTPTransport.handle_async_request = reject_async_network
        httpx.HTTPTransport.handle_request = reject_sync_network

        import balancer
        import proxy
        import rate_limiter

        rate_clock = {"now": 1000.0}
        rate_limiter.time = SimpleNamespace(monotonic=lambda: rate_clock["now"])
        clients = []
        requests = []

        def handler(request):
            assert "//" not in request.url.path
            requests.append({
                "host": request.url.host,
                "path": request.url.path,
                "timeout": request.extensions["timeout"],
            })
            return httpx.Response(200, json={"server": request.url.host})

        def make_client(**kwargs):
            assert kwargs.get("trust_env") is False
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                **kwargs,
            )
            clients.append(client)
            return client

        proxy.httpx = SimpleNamespace(
            AsyncClient=make_client,
            RequestError=httpx.RequestError,
            TimeoutException=httpx.TimeoutException,
        )

        async def scenario():
            monitor_started = asyncio.Event()
            sleeps = []

            async def paused_sleep(delay):
                sleeps.append(delay)
                monitor_started.set()
                await asyncio.Event().wait()

            proxy.asyncio = SimpleNamespace(
                sleep=paused_sleep,
                create_task=asyncio.create_task,
                CancelledError=asyncio.CancelledError,
                timeout=asyncio.timeout,
            )

            async with proxy.app.router.lifespan_context(proxy.app):
                await asyncio.wait_for(monitor_started.wait(), timeout=2.0)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=proxy.app),
                    base_url="http://heimdall.test", trust_env=False,
                ) as incoming:
                    responses = [await incoming.get("/") for _ in range(8)]
                    rate_clock["now"] = 1000.5
                    responses.append(await incoming.get("/"))
                    rate_clock["now"] = 1001.0
                    responses.append(await incoming.get("/"))

            print("CONFIG_RESULT=" + json.dumps({
                "backends": balancer.BACKENDS,
                "weights": balancer.BACKEND_WEIGHTS,
                "algorithm": balancer.BALANCING_ALGORITHM,
                "client_timeouts": [client.timeout.read for client in clients],
                "requests": requests,
                "sleep_intervals": sleeps,
                "forwarding_timeout": proxy.FORWARDING_TIMEOUT,
                "capacity": rate_limiter.CAPACITY,
                "refill_rate": rate_limiter.REFILL_RATE,
                "statuses": [response.status_code for response in responses],
                "servers": [response.json().get("server") for response in responses],
                "config_reads": len(reads),
                "in_flight": balancer.in_flight,
                "remaining_tokens": rate_limiter.buckets["127.0.0.1"]["tokens"],
                "clients_closed": all(client.is_closed for client in clients),
            }))

        asyncio.run(scenario())
    ''', path)

    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.removeprefix("CONFIG_RESULT=") for line in result.stdout.splitlines()
             if line.startswith("CONFIG_RESULT=")]
    assert len(lines) == 1
    report = json.loads(lines[0])
    assert report["backends"] == ["http://backend-a:9001", "http://backend-b:9002"]
    assert report["weights"] == {"http://backend-a:9001": 2, "http://backend-b:9002": 1}
    assert report["algorithm"] == "weighted_round_robin"
    assert report["client_timeouts"] == [0.4, 0.2]
    probes = [request for request in report["requests"] if request["path"] == "/health"]
    forwards = [request for request in report["requests"] if request["path"] == "/"]
    assert len(probes) == 2
    assert len(forwards) == 8
    assert all(set(request["timeout"].values()) == {0.2} for request in probes)
    assert all(set(request["timeout"].values()) == {0.4} for request in forwards)
    assert report["sleep_intervals"] == [0.1]
    assert report["forwarding_timeout"] == 0.8
    assert report["capacity"] == 7
    assert report["refill_rate"] == 1.5
    assert report["statuses"] == [200] * 7 + [429, 429, 200]
    assert report["servers"][:6] == ["backend-a", "backend-b", "backend-a", "backend-a", "backend-b", "backend-a"]
    assert report["config_reads"] == 1
    assert report["in_flight"] == {"http://backend-a:9001": 0, "http://backend-b:9002": 0}
    assert report["remaining_tokens"] == 0.5
    assert report["clients_closed"] is True


@pytest.mark.integration
@pytest.mark.parametrize("invalid_kind, error_field", [
    ("zero-refill", "rate_limit_refill_rate"),
    ("duplicate-backend", "Duplicate backend URL"),
    ("malformed-json", "json_invalid"),
])
def test_invalid_config_prevents_proxy_import_before_client_creation(tmp_path, invalid_kind, error_field):
    path = tmp_path / "invalid.json"
    data = {"backends": [{"url": "http://backend-a"}]}
    if invalid_kind == "zero-refill":
        data["rate_limit_refill_rate"] = 0
    elif invalid_kind == "duplicate-backend":
        data["backends"].append({"url": "HTTP://BACKEND-A:80/"})
    path.write_text(
        '{"backends": [}' if invalid_kind == "malformed-json" else json.dumps(data),
        encoding="utf-8",
    )

    result = run_child(tmp_path, '''
        import httpx

        def reject_client_creation(*args, **kwargs):
            raise AssertionError("Client created before configuration validation")

        httpx.AsyncClient = reject_client_creation
        import proxy
        print("PROXY_IMPORTED")
    ''', path)

    assert result.returncode != 0
    assert "ValidationError" in result.stderr
    assert error_field in result.stderr
    assert "PROXY_IMPORTED" not in result.stdout
    assert "Client created before configuration validation" not in result.stderr


@pytest.mark.integration
def test_fresh_proxy_import_uses_bundled_config_from_another_working_directory(tmp_path):
    result = run_child(tmp_path, '''
        import json
        import proxy
        import balancer
        print(json.dumps({
            "backends": balancer.BACKENDS,
            "algorithm": balancer.BALANCING_ALGORITHM,
        }))
    ''', None)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    expected = config.load_settings(config.DEFAULT_CONFIG_PATH)
    assert report["backends"] == [backend.base_url for backend in expected.backends]
    assert report["algorithm"] == expected.balancing_algorithm


@pytest.mark.integration
def test_loader_can_read_explicit_file_without_loading_invalid_server_selection(tmp_path):
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({
        "backends": [{"url": "http://backend-a"}],
        "rate_limit_capacity": 7,
    }), encoding="utf-8")
    result = run_child(tmp_path, '''
        from config import load_settings
        print(load_settings("explicit.json").rate_limit_capacity)
    ''', tmp_path / "missing-server-config.json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "7"
