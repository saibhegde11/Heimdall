from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

import balancer
import proxy


def configure_weights(monkeypatch, weights):
    monkeypatch.setattr(balancer, "BACKEND_WEIGHTS", weights.copy())
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", "weighted_round_robin")


def test_weighted_empty_candidates_returns_none(monkeypatch):
    configure_weights(monkeypatch, {"A": 2, "B": 1})
    assert balancer.choose_weighted_backend([]) is None
    assert balancer.current_scores == {}


def test_weighted_single_candidate_is_always_selected(monkeypatch):
    configure_weights(monkeypatch, {"A": 2, "B": 1})
    assert [balancer.choose_weighted_backend(["A"]) for _ in range(6)] == ["A"] * 6


def test_weights_two_to_one_produce_the_expected_smooth_sequence(monkeypatch):
    configure_weights(monkeypatch, {"A": 2, "B": 1})
    results = [balancer.choose_weighted_backend(["A", "B"]) for _ in range(6)]
    assert results == ["A", "B", "A", "A", "B", "A"]
    assert balancer.current_scores == {"A": 0, "B": 0}


def test_unspecified_weights_default_to_one(monkeypatch):
    configure_weights(monkeypatch, {})
    results = [balancer.choose_weighted_backend(["A", "B"]) for _ in range(6)]
    assert results == ["A", "B"] * 3


def test_weighted_selection_preserves_configuration_and_candidate_list(monkeypatch):
    configure_weights(monkeypatch, {"A": 2, "B": 1})
    candidates = ["A", "B"]
    for _ in range(12):
        balancer.choose_weighted_backend(candidates)
    assert candidates == ["A", "B"]
    assert balancer.BACKEND_WEIGHTS == {"A": 2, "B": 1}


def test_ineligible_backends_do_not_gain_score(monkeypatch):
    configure_weights(monkeypatch, {"A": 2, "B": 1})
    results = [balancer.choose_weighted_backend(["B"]) for _ in range(6)]
    assert results == ["B"] * 6
    assert "A" not in balancer.current_scores


def test_three_backend_distribution_matches_configured_weights(monkeypatch):
    configure_weights(monkeypatch, {"A": 5, "B": 1, "C": 1})
    results = [balancer.choose_weighted_backend(["A", "B", "C"]) for _ in range(70)]
    assert Counter(results) == {"A": 50, "B": 10, "C": 10}


def test_concurrent_weighted_selection_preserves_the_distribution(monkeypatch):
    configure_weights(monkeypatch, {"A": 2, "B": 1})
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(
            lambda _: balancer.choose_weighted_backend(["A", "B"]), range(600),
        ))
    assert Counter(results) == {"A": 400, "B": 200}


def test_dispatch_can_select_ordinary_round_robin(monkeypatch):
    configure_weights(monkeypatch, {"A": 2, "B": 1})
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", "round_robin")
    results = [balancer.choose_untried_backend(["A", "B"], set()) for _ in range(6)]
    assert results == ["A", "B"] * 3


def test_dispatch_can_select_weighted_round_robin(monkeypatch):
    configure_weights(monkeypatch, {"A": 2, "B": 1})
    results = [balancer.choose_untried_backend(["A", "B"], set()) for _ in range(6)]
    assert results == ["A", "B", "A", "A", "B", "A"]


def test_weight_does_not_override_attempted_backend_exclusion(monkeypatch):
    configure_weights(monkeypatch, {"A": 100, "B": 1})
    assert balancer.choose_untried_backend(["A", "B"], {"A"}) == "B"


def test_all_attempted_returns_none_in_weighted_mode(monkeypatch):
    configure_weights(monkeypatch, {"A": 2, "B": 1})
    assert balancer.choose_untried_backend(["A", "B"], {"A", "B"}) is None


def test_unknown_algorithm_is_reported_instead_of_silently_falling_back(monkeypatch):
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", "unknown")
    with pytest.raises(ValueError, match="Unknown balancing algorithm"):
        balancer.choose_untried_backend(["A", "B"], set())


def test_proxy_uses_the_weighted_sequence(run_proxy, monkeypatch):
    configure_weights(monkeypatch, {"http://backend-a": 2, "http://backend-b": 1})

    def handler(request):
        return httpx.Response(200, json={"server": request.url.host})

    async def exercise(client):
        responses = [await client.get("/") for _ in range(6)]
        assert all(response.status_code == 200 for response in responses)
        assert [response.json()["server"] for response in responses] == [
            "backend-a", "backend-b", "backend-a",
            "backend-a", "backend-b", "backend-a",
        ]

    run_proxy(handler, exercise)


def test_proxy_only_uses_the_eligible_backend_in_weighted_mode(run_proxy, monkeypatch):
    configure_weights(monkeypatch, {"http://backend-a": 100, "http://backend-b": 1})

    def handler(request):
        assert request.url.host == "backend-b"
        return httpx.Response(200, json={"server": "B"})

    async def exercise(client):
        for _ in range(4):
            response = await client.get("/")
            assert response.status_code == 200
            assert response.json() == {"server": "B"}

    run_proxy(handler, exercise, backends=["http://backend-b"])


def test_weighted_get_failure_can_still_fail_over_once(run_proxy, monkeypatch):
    configure_weights(monkeypatch, {"http://backend-a": 2, "http://backend-b": 1})
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "backend-a":
            raise httpx.ConnectError("A failed", request=request)
        return httpx.Response(200, json={"server": "B"})

    async def exercise(client):
        response = await client.get("/")
        assert response.status_code == 200
        assert response.json() == {"server": "B"}

    run_proxy(handler, exercise)
    assert seen == ["backend-a", "backend-b"]


def test_weighted_post_is_not_retried(run_proxy, monkeypatch):
    configure_weights(monkeypatch, {"http://backend-a": 2, "http://backend-b": 1})
    seen = []

    def handler(request):
        seen.append(request.url.host)
        raise httpx.ReadTimeout("The POST response was lost", request=request)

    async def exercise(client):
        response = await client.post("/echo", json={"count": 2})
        assert response.status_code == 504

    run_proxy(handler, exercise)
    assert seen == ["backend-a"]


def test_weighted_routing_handles_eligibility_changes_and_recovery(run_proxy, monkeypatch):
    configure_weights(monkeypatch, {"http://backend-a": 2, "http://backend-b": 1})

    def handler(request):
        return httpx.Response(200, json={"server": request.url.host})

    async def exercise(client):
        first = [await client.get("/") for _ in range(3)]
        assert [response.json()["server"] for response in first] == [
            "backend-a", "backend-b", "backend-a",
        ]
        proxy.app.state.healthy_backends = ["http://backend-b"]
        while_only_b = [await client.get("/") for _ in range(4)]
        assert all(response.json()["server"] == "backend-b" for response in while_only_b)
        proxy.app.state.healthy_backends = ["http://backend-a", "http://backend-b"]
        recovered = [await client.get("/") for _ in range(3)]
        assert [response.json()["server"] for response in recovered] == [
            "backend-a", "backend-b", "backend-a",
        ]

    run_proxy(handler, exercise)


@pytest.mark.integration
def test_actual_backend_apps_receive_the_configured_share(
    run_proxy, real_backend_router, monkeypatch,
):
    configure_weights(monkeypatch, {"http://backend-a": 2, "http://backend-b": 1})

    async def exercise(client):
        responses = [await client.get("/") for _ in range(6)]
        assert all(response.status_code == 200 for response in responses)
        assert [response.json()["server"] for response in responses] == [
            "A", "B", "A", "A", "B", "A",
        ]

    run_proxy(real_backend_router, exercise)
