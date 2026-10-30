import pytest

from benchmarks.load import aggregate_trials, percentile, summarize_samples
from benchmarks.run_benchmark import parse_args


@pytest.mark.parametrize("values, proportion, expected", [
    ([], 0.95, None),
    ([7.0], 0.95, 7.0),
    ([4, 1, 3, 2], 0.50, 2),
    ([1, 2, 3, 4, 5], 0.95, 5),
    (list(range(1, 101)), 0.99, 99),
])
def test_percentiles_use_nearest_rank_and_handle_empty_samples(values, proportion, expected):
    assert percentile(values, proportion) == expected


@pytest.mark.parametrize("proportion", [-0.1, 1.1])
def test_percentile_rejects_invalid_proportions(proportion):
    with pytest.raises(ValueError):
        percentile([1, 2], proportion)


def sample(latency, status=200, server="A", ok=True, error=None):
    return {
        "latency_ms": latency, "status_code": status,
        "server": server, "ok": ok, "error_type": error,
    }


def test_fast_errors_do_not_inflate_successful_throughput_or_latency():
    samples = [
        sample(100, server="A"),
        sample(200, server="B"),
        sample(0.1, status=503, server=None, ok=False),
        sample(0.2, server=None, ok=False, error="InvalidPayload"),
        sample(10000, status=None, server=None, ok=False, error="ReadTimeout"),
    ]
    report = summarize_samples(samples, 4.0)
    assert report["requests"] == 5
    assert report["successes"] == 2
    assert report["errors"] == 3
    assert report["successful_rps"] == 0.5
    assert report["http_errors"] == 1
    assert report["payload_errors"] == 1
    assert report["transport_errors"] == 1
    assert report["p95_ms"] == 200
    assert report["backend_counts"] == {"A": 1, "B": 1}


def test_all_failed_trial_has_no_success_latency():
    report = summarize_samples([sample(0.1, status=503, server=None, ok=False)], 1.0)
    assert report["successful_rps"] == 0.0
    assert report["successes"] == 0
    assert report["errors"] == 1
    assert report["p50_ms"] is None
    assert report["p95_ms"] is None
    assert report["p99_ms"] is None


@pytest.mark.parametrize("elapsed", [0, -1, float("inf"), float("nan")])
def test_summary_requires_valid_elapsed_time(elapsed):
    with pytest.raises(ValueError):
        summarize_samples([], elapsed)


def trial(rate=100.0, p95=10.0, concurrency=8):
    return {
        "profile": "equal", "mode": "round_robin", "concurrency": concurrency,
        "successful_rps": rate, "p50_ms": 5.0, "p95_ms": p95, "p99_ms": 20.0,
        "requests": 3, "successes": 3, "errors": 0,
        "backend_counts": {"A": 2, "B": 1},
    }


def test_summary_uses_median_trials_and_retains_the_range():
    result = aggregate_trials([trial(100, 10), trial(150, 20), trial(1000, 100)])[0]
    assert result["trials"] == 3
    assert result["successful_rps_median"] == 150
    assert result["successful_rps_min"] == 100
    assert result["successful_rps_max"] == 1000
    assert result["p95_ms_median"] == 20
    assert result["backend_a_share_percent"] == pytest.approx(200 / 3)


def test_different_concurrency_levels_are_not_merged():
    result = aggregate_trials([trial(concurrency=1), trial(concurrency=8)])
    assert {row["concurrency"] for row in result} == {1, 8}
    assert all(row["trials"] == 1 for row in result)


def test_failed_trial_is_retained_and_makes_latency_median_unavailable():
    failed = {
        **trial(0), "successes": 0, "errors": 3, "backend_counts": {},
        "p50_ms": None, "p95_ms": None, "p99_ms": None,
    }
    result = aggregate_trials([trial(100), failed])[0]
    assert result["trials"] == 2
    assert result["successful_rps_median"] == 50
    assert result["errors"] == 3
    assert result["p95_ms_median"] is None


@pytest.mark.parametrize("arguments", [
    ["--duration", "0"], ["--duration", "nan"], ["--duration", "inf"],
    ["--warmup", "-1"], ["--concurrency", "0"],
    ["--repetitions", "0"], ["--slots", "0"],
])
def test_cli_rejects_invalid_workload_values(arguments):
    with pytest.raises(SystemExit) as raised:
        parse_args(arguments)
    assert raised.value.code == 2
