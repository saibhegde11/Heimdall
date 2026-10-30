from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

import balancer


ALGORITHMS = ["round_robin", "weighted_round_robin", "least_in_flight"]


def test_start_tracks_each_backend_independently():
    balancer.start_backend_request("A")
    balancer.start_backend_request("A")
    balancer.start_backend_request("B")
    assert balancer.in_flight == {"A": 2, "B": 1}


def test_finish_releases_one_attempt_without_affecting_other_backends():
    balancer.start_backend_request("A")
    balancer.start_backend_request("A")
    balancer.start_backend_request("B")
    balancer.finish_backend_request("A")
    assert balancer.in_flight == {"A": 1, "B": 1}
    balancer.finish_backend_request("A")
    assert balancer.in_flight == {"A": 0, "B": 1}


@pytest.mark.parametrize("initial_count", [None, 0, -1], ids=["missing", "zero", "negative"])
def test_finish_rejects_inactive_backend_without_changing_counts(initial_count):
    balancer.in_flight["B"] = 2
    if initial_count is not None:
        balancer.in_flight["A"] = initial_count
    before = balancer.in_flight.copy()
    with pytest.raises(ValueError, match="No active request"):
        balancer.finish_backend_request("A")
    assert balancer.in_flight == before


def test_counter_helpers_preserve_concurrent_starts_and_finishes():
    backends = ["A", "B"] * 300
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(balancer.start_backend_request, backends))
        assert balancer.in_flight == {"A": 300, "B": 300}
        list(pool.map(balancer.finish_backend_request, backends))
    assert balancer.in_flight == {"A": 0, "B": 0}


def test_empty_least_selection_does_not_reserve_or_change_the_tie_cursor(monkeypatch):
    monkeypatch.setattr(balancer, "least_next_index", 7)
    balancer.in_flight["A"] = 2
    assert balancer.choose_least_in_flight_backend([]) is None
    assert balancer.in_flight == {"A": 2}
    assert balancer.least_next_index == 7


def test_least_selection_reserves_the_backend_with_the_smallest_count():
    balancer.in_flight.update({"A": 5, "B": 1, "C": 3})
    assert balancer.choose_least_in_flight_backend(["A", "B", "C"]) == "B"
    assert balancer.in_flight == {"A": 5, "B": 2, "C": 3}


def test_equal_idle_backends_rotate_after_each_attempt_finishes():
    selected = []
    for _ in range(6):
        backend = balancer.choose_least_in_flight_backend(["A", "B"])
        selected.append(backend)
        balancer.finish_backend_request(backend)
    assert selected == ["A", "B", "A", "B", "A", "B"]
    assert balancer.in_flight == {"A": 0, "B": 0}


def test_rotation_only_includes_backends_tied_for_the_smallest_count():
    balancer.in_flight.update({"A": 2, "B": 5, "C": 2})
    selected = []
    for _ in range(6):
        backend = balancer.choose_least_in_flight_backend(["A", "B", "C"])
        selected.append(backend)
        balancer.finish_backend_request(backend)
    assert selected == ["A", "C", "A", "C", "A", "C"]
    assert balancer.in_flight == {"A": 2, "B": 5, "C": 2}


def test_tie_cursor_handles_a_smaller_candidate_list(monkeypatch):
    monkeypatch.setattr(balancer, "least_next_index", 99)
    balancer.in_flight.update({"A": 0, "B": 4})
    assert balancer.choose_least_in_flight_backend(["B"]) == "B"
    assert balancer.in_flight == {"A": 0, "B": 5}


def test_pending_reservations_affect_the_very_next_selection():
    first = balancer.choose_least_in_flight_backend(["A", "B"])
    second = balancer.choose_least_in_flight_backend(["A", "B"])
    assert first == "A"
    assert second == "B"
    assert balancer.in_flight == {"A": 1, "B": 1}


def test_concurrent_least_selection_balances_and_reserves_every_attempt():
    with ThreadPoolExecutor(max_workers=12) as pool:
        selected = list(pool.map(
            lambda _: balancer.choose_least_in_flight_backend(["A", "B"]),
            range(600),
        ))
    assert Counter(selected) == {"A": 300, "B": 300}
    assert balancer.in_flight == {"A": 300, "B": 300}
    for backend in selected:
        balancer.finish_backend_request(backend)
    assert balancer.in_flight == {"A": 0, "B": 0}


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_dispatch_reserves_exactly_once_for_every_algorithm(monkeypatch, algorithm):
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", algorithm)
    monkeypatch.setattr(balancer, "BACKEND_WEIGHTS", {"A": 2, "B": 1})
    balancer.in_flight.update({"A": 4, "B": 8})
    selected = balancer.choose_untried_backend(["A", "B"], set())
    assert selected == "A"
    assert balancer.in_flight == {"A": 5, "B": 8}
    balancer.finish_backend_request(selected)
    assert balancer.in_flight == {"A": 4, "B": 8}


def test_least_dispatch_excludes_attempted_backend_even_when_it_is_idle(monkeypatch):
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", "least_in_flight")
    balancer.in_flight.update({"A": 0, "B": 5})
    backends = ["A", "B"]
    attempted = {"A"}
    assert balancer.choose_untried_backend(backends, attempted) == "B"
    assert balancer.in_flight == {"A": 0, "B": 6}
    assert backends == ["A", "B"]
    assert attempted == {"A"}


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_dispatch_with_no_untried_backend_leaves_counts_untouched(monkeypatch, algorithm):
    monkeypatch.setattr(balancer, "BALANCING_ALGORITHM", algorithm)
    balancer.in_flight["A"] = 3
    assert balancer.choose_untried_backend(["A"], {"A"}) is None
    assert balancer.choose_untried_backend([], set()) is None
    assert balancer.in_flight == {"A": 3}


def test_least_selection_does_not_change_weights_scores_or_candidates(monkeypatch):
    monkeypatch.setattr(balancer, "BACKEND_WEIGHTS", {"A": 100, "B": 1})
    balancer.current_scores.update({"A": 8, "B": -8})
    candidates = ["A", "B"]
    balancer.in_flight.update({"A": 2, "B": 0})
    assert balancer.choose_least_in_flight_backend(candidates) == "B"
    assert candidates == ["A", "B"]
    assert balancer.BACKEND_WEIGHTS == {"A": 100, "B": 1}
    assert balancer.current_scores == {"A": 8, "B": -8}
