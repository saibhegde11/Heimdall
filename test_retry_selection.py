import balancer


def test_excludes_a_backend_already_attempted():
    assert balancer.choose_untried_backend(["A", "B"], {"A"}) == "B"


def test_no_attempts_preserves_round_robin():
    results = [balancer.choose_untried_backend(["A", "B"], set()) for _ in range(6)]
    assert results == ["A", "B"] * 3


def test_all_candidates_attempted_returns_none():
    assert balancer.choose_untried_backend(["A", "B"], {"A", "B"}) is None


def test_empty_candidates_returns_none():
    assert balancer.choose_untried_backend([], set()) is None


def test_filtering_handles_the_counter_left_by_the_first_attempt():
    assert balancer.choose_untried_backend(["A", "B"], set()) == "A"
    assert balancer.choose_untried_backend(["A", "B"], {"A"}) == "B"


def test_selection_does_not_modify_the_inputs():
    backends = ["A", "B"]
    attempted = {"A"}
    assert balancer.choose_untried_backend(backends, attempted) == "B"
    assert backends == ["A", "B"]
    assert attempted == {"A"}
