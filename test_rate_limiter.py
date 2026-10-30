from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

import rate_limiter as limiter


@pytest.mark.parametrize(
    "tokens, elapsed, capacity, refill_rate, expected",
    [
        (0.0, 0.0, 10, 2.0, 0.0),
        (3.0, 0.0, 10, 2.0, 3.0),
        (1.0, 0.5, 10, 2.0, 2.0),
        (0.0, 0.15, 10, 2.0, 0.3),
        (4.0, 3.0, 10, 2.0, 10.0),
        (9.0, 100.0, 10, 2.0, 10.0),
        (10.0, 0.5, 10, 2.0, 10.0),
        (0.75, 0.25, 1, 1.0, 1.0),
    ],
)
def test_refill_adds_elapsed_credit_without_exceeding_capacity(
    tokens, elapsed, capacity, refill_rate, expected,
):
    assert limiter.refill_tokens(tokens, elapsed, capacity, refill_rate) == pytest.approx(expected)


def test_new_client_gets_a_full_bucket_and_spends_one_token(rate_clock):
    assert limiter.allow_request("192.0.2.1") == (True, 0.0)
    assert limiter.buckets == {
        "192.0.2.1": {"tokens": 9.0, "last_refill": 1000.0},
    }


def test_initial_burst_allows_ten_then_rejects_the_next_request(rate_clock):
    assert [limiter.allow_request("192.0.2.1") for _ in range(10)] == [(True, 0.0)] * 10
    assert limiter.allow_request("192.0.2.1") == (False, 0.5)
    assert limiter.buckets["192.0.2.1"]["tokens"] == 0.0


def test_exactly_half_a_second_refills_one_token(rate_clock):
    for _ in range(10):
        limiter.allow_request("192.0.2.1")
    rate_clock["now"] = 1000.5
    assert limiter.allow_request("192.0.2.1") == (True, 0.0)
    assert limiter.buckets["192.0.2.1"] == {"tokens": 0.0, "last_refill": 1000.5}


def test_rejected_requests_preserve_fractional_progress_without_double_refill(rate_clock):
    for _ in range(10):
        limiter.allow_request("192.0.2.1")
    rate_clock["now"] = 1000.2
    allowed, wait = limiter.allow_request("192.0.2.1")
    assert allowed is False
    assert wait == pytest.approx(0.3)
    assert limiter.buckets["192.0.2.1"]["tokens"] == pytest.approx(0.4)
    rate_clock["now"] = 1000.3
    allowed, wait = limiter.allow_request("192.0.2.1")
    assert allowed is False
    assert wait == pytest.approx(0.2)
    assert limiter.buckets["192.0.2.1"]["tokens"] == pytest.approx(0.6)
    rate_clock["now"] = 1000.5
    assert limiter.allow_request("192.0.2.1") == (True, 0.0)


def test_repeated_rejections_at_the_same_time_do_not_spend_fractional_tokens(rate_clock):
    for _ in range(10):
        limiter.allow_request("192.0.2.1")
    rate_clock["now"] = 1000.1
    for _ in range(20):
        allowed, wait = limiter.allow_request("192.0.2.1")
        assert allowed is False
        assert wait == pytest.approx(0.4)
    assert limiter.buckets["192.0.2.1"]["tokens"] == pytest.approx(0.2)
    assert limiter.buckets["192.0.2.1"]["last_refill"] == 1000.1


def test_long_idle_time_cannot_create_a_burst_larger_than_capacity(rate_clock):
    limiter.allow_request("192.0.2.1")
    rate_clock["now"] = 1000000.0
    assert [limiter.allow_request("192.0.2.1") for _ in range(10)] == [(True, 0.0)] * 10
    assert limiter.allow_request("192.0.2.1") == (False, 0.5)


def test_each_client_has_an_independent_allowance(rate_clock):
    for _ in range(10):
        limiter.allow_request("192.0.2.1")
    assert limiter.allow_request("192.0.2.1") == (False, 0.5)
    assert limiter.allow_request("198.51.100.1") == (True, 0.0)
    assert limiter.buckets["192.0.2.1"]["tokens"] == 0.0
    assert limiter.buckets["198.51.100.1"]["tokens"] == 9.0


def test_refill_timestamp_moves_forward_even_when_bucket_reaches_capacity(rate_clock):
    limiter.allow_request("192.0.2.1")
    rate_clock["now"] = 1100.0
    assert limiter.allow_request("192.0.2.1") == (True, 0.0)
    assert limiter.buckets["192.0.2.1"] == {"tokens": 9.0, "last_refill": 1100.0}
    assert limiter.allow_request("192.0.2.1") == (True, 0.0)
    assert limiter.buckets["192.0.2.1"]["tokens"] == 8.0


def test_concurrent_requests_cannot_spend_the_last_token_more_than_once(rate_clock):
    for _ in range(9):
        limiter.allow_request("192.0.2.1")
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: limiter.allow_request("192.0.2.1"), range(600)))
    assert results.count((True, 0.0)) == 1
    assert results.count((False, 0.5)) == 599
    assert limiter.buckets["192.0.2.1"]["tokens"] == 0.0


def test_concurrent_clients_keep_separate_burst_budgets(rate_clock):
    clients = ["192.0.2.1", "198.51.100.1"] * 300

    def attempt(client_ip):
        allowed, _ = limiter.allow_request(client_ip)
        return client_ip, allowed

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, clients))
    accepted = Counter(client_ip for client_ip, allowed in results if allowed)
    assert accepted == {"192.0.2.1": 10, "198.51.100.1": 10}
    assert all(bucket["tokens"] == 0.0 for bucket in limiter.buckets.values())
