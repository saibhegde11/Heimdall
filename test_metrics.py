from concurrent.futures import ThreadPoolExecutor

import pytest

import balancer
import metrics


def test_counters_start_at_zero():
    assert metrics.get_totals() == {
        "requests_total": 0,
        "backend_attempts_total": 0,
        "retries_total": 0,
        "rate_limited_total": 0,
    }


def test_increment_changes_only_the_requested_counter():
    metrics.increment_counter("requests_total")
    metrics.increment_counter("requests_total")
    metrics.increment_counter("backend_attempts_total")
    assert metrics.get_totals() == {
        "requests_total": 2,
        "backend_attempts_total": 1,
        "retries_total": 0,
        "rate_limited_total": 0,
    }


def test_totals_snapshot_cannot_mutate_internal_counters():
    metrics.increment_counter("requests_total")
    snapshot = metrics.get_totals()
    snapshot["requests_total"] = 999
    snapshot["unexpected"] = 1
    assert metrics.get_totals()["requests_total"] == 1
    assert "unexpected" not in metrics.get_totals()


def test_unknown_counter_is_reported_without_creating_a_new_key():
    before = metrics.get_totals()
    with pytest.raises(KeyError):
        metrics.increment_counter("requsets_total")
    assert metrics.get_totals() == before


def test_concurrent_counter_updates_are_not_lost():
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: metrics.increment_counter("requests_total"), range(600)))
    assert metrics.get_totals()["requests_total"] == 600


def test_in_flight_snapshot_cannot_change_backend_reservations():
    balancer.start_backend_request("A")
    snapshot = balancer.get_in_flight()
    snapshot["A"] = 999
    assert balancer.get_in_flight() == {"A": 1}
    balancer.finish_backend_request("A")
    assert balancer.get_in_flight() == {"A": 0}
