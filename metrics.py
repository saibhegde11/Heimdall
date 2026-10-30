from threading import Lock

stats_lock = Lock()

totals = {
    "requests_total": 0,
    "backend_attempts_total": 0,
    "retries_total": 0,
    "rate_limited_total": 0,
}


def increment_counter(name: str) -> None:
    with stats_lock:
        totals[name] += 1


def get_totals() -> dict[str, int]:
    with stats_lock:
        return totals.copy()
