from threading import Lock
from config import get_settings

settings = get_settings()
BACKENDS = [backend.base_url for backend in settings.backends]
BACKEND_WEIGHTS = {
    backend.base_url: backend.weight
    for backend in settings.backends
}
BALANCING_ALGORITHM = settings.balancing_algorithm

next_index = 0
least_next_index = 0
current_scores = {}
in_flight = {}
selection_lock = Lock()


def start_backend_request(backend: str) -> None:
    with selection_lock:
        in_flight[backend] = in_flight.get(backend, 0) + 1


def finish_backend_request(backend: str) -> None:
    with selection_lock:
        count = in_flight.get(backend, 0)
        if count <= 0:
            raise ValueError(f"No active request for backend: {backend}")
        in_flight[backend] = count - 1


def get_in_flight() -> dict[str, int]:
    with selection_lock:
        return in_flight.copy()


def choose_backend(backends: list[str]) -> str | None:
    global next_index

    with selection_lock:
        if not backends:
            return None
        next_index = next_index % len(backends)
        url = backends[next_index]
        next_index = (next_index + 1) % len(backends)
        return url


def choose_weighted_backend(backends: list[str]) -> str | None:
    with selection_lock:
        if not backends:
            return None

        total_weight = 0
        selected = None

        for backend in backends:
            weight = BACKEND_WEIGHTS.get(backend, 1)
            current_scores[backend] = current_scores.get(backend, 0) + weight
            total_weight += weight

            if (
                selected is None
                or current_scores[backend] > current_scores[selected]
            ):
                selected = backend

        current_scores[selected] -= total_weight
        return selected


def choose_least_in_flight_backend(backends: list[str]) -> str | None:
    """Choose an eligible backend and reserve one attempt under the same lock."""
    global least_next_index

    with selection_lock:
        if not backends:
            return None

        lowest_count = min(
            in_flight.get(backend, 0)
            for backend in backends
        )
        candidates = [
            backend
            for backend in backends
            if in_flight.get(backend, 0) == lowest_count
        ]

        least_next_index %= len(candidates)
        selected = candidates[least_next_index]
        least_next_index = (least_next_index + 1) % len(candidates)

        in_flight[selected] = in_flight.get(selected, 0) + 1
        return selected


def choose_untried_backend(
    backends: list[str],
    attempted: set[str],
) -> str | None:
    """Select an untried backend and reserve exactly one active attempt."""
    candidates = []

    for backend in backends:
        if backend not in attempted:
            candidates.append(backend)

    if BALANCING_ALGORITHM == "round_robin":
        selected = choose_backend(candidates)
    elif BALANCING_ALGORITHM == "weighted_round_robin":
        selected = choose_weighted_backend(candidates)
    elif BALANCING_ALGORITHM == "least_in_flight":
        return choose_least_in_flight_backend(candidates)
    else:
        raise ValueError(f"Unknown balancing algorithm: {BALANCING_ALGORITHM}")

    if selected is not None:
        start_backend_request(selected)
    return selected


if __name__ == "__main__":
    for _ in range(6):
        backend = choose_untried_backend(BACKENDS, set())
        print(backend)
        if backend is not None:
            finish_backend_request(backend)
