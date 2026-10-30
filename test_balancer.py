from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import balancer


def test_round_robin_alternates():
    backends = ["A", "B"]
    results = [balancer.choose_backend(backends) for _ in range(6)]
    assert results == ["A", "B", "A", "B", "A", "B"]


def test_selection_handles_a_shrinking_list():
    assert balancer.choose_backend(["A", "B"]) == "A"
    assert balancer.choose_backend(["B"]) == "B"


def test_single_backend_is_always_selected():
    results = [balancer.choose_backend(["B"]) for _ in range(6)]
    assert results == ["B"] * 6


def test_no_healthy_backend_returns_none():
    results = [balancer.choose_backend([]) for _ in range(6)]
    assert results == [None] * 6


def test_round_robin_wraps_across_three_backends():
    backends = ["A", "B", "C"]
    results = [balancer.choose_backend(backends) for _ in range(9)]
    assert results == ["A", "B", "C"] * 3


def test_selection_recovers_after_an_empty_list():
    assert balancer.choose_backend(["A", "B"]) == "A"
    assert balancer.choose_backend([]) is None
    assert balancer.choose_backend(["B"]) == "B"
    assert balancer.choose_backend(["A", "B"]) in {"A", "B"}


def test_selection_does_not_modify_the_candidate_list():
    backends = ["A", "B", "C"]
    original = backends.copy()
    for _ in range(10):
        balancer.choose_backend(backends)
    assert backends == original


def test_concurrent_selection_has_an_even_distribution():
    backends = ["A", "B", "C"]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: balancer.choose_backend(backends), range(600)))
    assert Counter(results) == {"A": 200, "B": 200, "C": 200}
