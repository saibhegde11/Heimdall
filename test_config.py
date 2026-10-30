import json

import pytest
from pydantic import ValidationError

import config
from config import BackendConfig, Settings, load_settings


@pytest.fixture(autouse=True)
def clear_configuration_override(monkeypatch):
    monkeypatch.delenv("HEIMDALL_CONFIG", raising=False)


def configuration(**overrides):
    data = {
        "backends": [
            {"url": "http://backend-a:8001", "weight": 2},
            {"url": "http://backend-b:8002", "weight": 1},
        ],
    }
    data.update(overrides)
    return data


def write_configuration(path, **overrides):
    path.write_text(json.dumps(configuration(**overrides)), encoding="utf-8")
    return path


def test_minimal_configuration_supplies_the_documented_defaults():
    loaded = Settings.model_validate(configuration())
    assert loaded.balancing_algorithm == "least_in_flight"
    assert loaded.rate_limit_capacity == 10
    assert loaded.rate_limit_refill_rate == 2.0
    assert loaded.backend_timeout == 2.0
    assert loaded.health_check_timeout == 1.0
    assert loaded.health_check_interval == 2.0
    assert loaded.forwarding_timeout == 3.0
    assert BackendConfig(url="http://backend-a").weight == 1


def test_supplied_settings_replace_defaults_without_changing_backend_order():
    loaded = Settings.model_validate(configuration(
        balancing_algorithm="weighted_round_robin",
        rate_limit_capacity=7,
        rate_limit_refill_rate=1.5,
        backend_timeout=0.4,
        health_check_timeout=0.2,
        health_check_interval=0.1,
        forwarding_timeout=0.8,
    ))
    assert [backend.base_url for backend in loaded.backends] == [
        "http://backend-a:8001", "http://backend-b:8002",
    ]
    assert [backend.weight for backend in loaded.backends] == [2, 1]
    assert loaded.balancing_algorithm == "weighted_round_robin"
    assert loaded.rate_limit_capacity == 7
    assert loaded.rate_limit_refill_rate == 1.5
    assert loaded.backend_timeout == 0.4
    assert loaded.health_check_timeout == 0.2
    assert loaded.health_check_interval == 0.1
    assert loaded.forwarding_timeout == 0.8


@pytest.mark.parametrize("field", [
    "rate_limit_refill_rate", "backend_timeout", "health_check_timeout",
    "health_check_interval", "forwarding_timeout",
])
def test_duration_and_refill_settings_require_positive_finite_numbers(field):
    for invalid in (0, -1, True, "2.0", None, float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValidationError, match=field):
            Settings.model_validate(configuration(**{field: invalid}))
    # Integer JSON numbers are also legitimate positive numeric settings.
    assert getattr(Settings.model_validate(configuration(**{field: 1})), field) == 1.0


@pytest.mark.parametrize("field", ["weight", "rate_limit_capacity"])
def test_weights_and_capacity_require_positive_integers(field):
    for invalid in (0, -1, True, "2", 2.0, None):
        data = configuration()
        if field == "weight":
            data["backends"][0][field] = invalid
        else:
            data[field] = invalid
        with pytest.raises(ValidationError, match=field):
            Settings.model_validate(data)


@pytest.mark.parametrize("algorithm", ["least_connections", "ROUND_ROBIN", True, None])
def test_unknown_or_incorrectly_typed_algorithms_are_rejected(algorithm):
    with pytest.raises(ValidationError, match="balancing_algorithm"):
        Settings.model_validate(configuration(balancing_algorithm=algorithm))


@pytest.mark.parametrize("backends", [[], None, ["http://backend-a"], [{}]])
def test_backend_list_requires_at_least_one_valid_backend(backends):
    with pytest.raises(ValidationError, match="backends"):
        Settings.model_validate(configuration(backends=backends))


def test_backend_list_is_required():
    with pytest.raises(ValidationError, match="backends"):
        Settings.model_validate({})


@pytest.mark.parametrize("location", ["settings", "backend"])
def test_unknown_fields_are_reported_instead_of_silently_ignored(location):
    data = configuration()
    if location == "settings":
        data["heath_check_timeout"] = 1.0
        misspelled = "heath_check_timeout"
    else:
        data["backends"][0]["wieght"] = 2
        misspelled = "wieght"
    with pytest.raises(ValidationError, match=misspelled):
        Settings.model_validate(data)


@pytest.mark.parametrize("url", [
    "not-a-url", "", "ftp://backend-a", "http://backend-a:99999",
    "http://backend-a/api", "http://backend-a/%2F",
    "http://backend-a/?q=1", "http://backend-a/?",
    "http://backend-a/#section", "http://backend-a/#",
    "http://user:password@backend-a", "http://user@backend-a",
])
def test_backend_addresses_must_be_http_origins(url):
    with pytest.raises(ValidationError, match="url"):
        BackendConfig(url=url)


@pytest.mark.parametrize("url, expected", [
    ("HTTP://BACKEND-A:80/", "http://backend-a"),
    ("http://127.0.0.1:8001/", "http://127.0.0.1:8001"),
    ("https://backend-a:443/", "https://backend-a"),
    ("http://[::1]:8001/", "http://[::1]:8001"),
])
def test_valid_backend_origins_have_a_normalized_base_string(url, expected):
    assert BackendConfig(url=url).base_url == expected


@pytest.mark.parametrize("first, second", [
    ("http://backend-a:8001", "http://backend-a:8001/"),
    ("http://backend-a", "HTTP://BACKEND-A:80/"),
    ("https://backend-a", "https://backend-a:443/"),
])
def test_duplicate_backends_are_rejected_after_url_normalization(first, second):
    with pytest.raises(ValidationError, match="Duplicate backend URL"):
        Settings.model_validate(configuration(backends=[{"url": first}, {"url": second}]))


def test_distinct_ports_on_one_host_are_valid_separate_backends():
    loaded = Settings.model_validate(configuration(backends=[
        {"url": "http://127.0.0.1:8001"}, {"url": "http://127.0.0.1:8002"},
    ]))
    assert len(loaded.backends) == 2


@pytest.mark.parametrize("as_string", [False, True], ids=["path-object", "path-string"])
def test_loader_reads_an_explicit_utf8_json_file(tmp_path, as_string):
    path = write_configuration(tmp_path / "custom.json", rate_limit_capacity=7)
    loaded = load_settings(str(path) if as_string else path)
    assert loaded.rate_limit_capacity == 7


def test_default_file_is_located_beside_config_module_despite_changed_cwd(tmp_path, monkeypatch):
    expected = Settings.model_validate_json(config.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    monkeypatch.chdir(tmp_path)
    assert load_settings() == expected


def test_environment_variable_selects_an_alternative_file(tmp_path, monkeypatch):
    path = write_configuration(tmp_path / "custom.json", rate_limit_capacity=7)
    monkeypatch.setenv("HEIMDALL_CONFIG", str(path))
    assert load_settings().rate_limit_capacity == 7


def test_explicit_path_takes_priority_over_environment_variable(tmp_path, monkeypatch):
    selected_by_env = write_configuration(tmp_path / "environment.json", rate_limit_capacity=3)
    selected_explicitly = write_configuration(tmp_path / "explicit.json", rate_limit_capacity=7)
    monkeypatch.setenv("HEIMDALL_CONFIG", str(selected_by_env))
    assert load_settings(selected_explicitly).rate_limit_capacity == 7


@pytest.mark.parametrize("route", ["argument", "environment"])
def test_relative_overrides_are_resolved_from_the_working_directory(tmp_path, monkeypatch, route):
    write_configuration(tmp_path / "custom.json", rate_limit_capacity=7)
    monkeypatch.chdir(tmp_path)
    if route == "environment":
        monkeypatch.setenv("HEIMDALL_CONFIG", "custom.json")
        loaded = load_settings()
    else:
        loaded = load_settings("custom.json")
    assert loaded.rate_limit_capacity == 7


@pytest.mark.parametrize("route", ["argument", "environment"])
def test_blank_configuration_path_is_an_error(monkeypatch, route):
    if route == "environment":
        monkeypatch.setenv("HEIMDALL_CONFIG", " ")
        path = None
    else:
        path = " "
    with pytest.raises(ValueError, match="path must not be empty"):
        load_settings(path)


@pytest.mark.parametrize("route", ["argument", "environment"])
def test_missing_selected_file_does_not_fall_back_to_defaults(tmp_path, monkeypatch, route):
    missing = tmp_path / "missing.json"
    if route == "environment":
        monkeypatch.setenv("HEIMDALL_CONFIG", str(missing))
        path = None
    else:
        path = missing
    with pytest.raises(FileNotFoundError):
        load_settings(path)


def test_invalid_json_is_reported_by_the_loader(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text('{"backends": [}', encoding="utf-8")
    with pytest.raises(ValidationError, match="json_invalid"):
        load_settings(path)


def test_invalid_selected_values_do_not_fall_back_to_defaults(tmp_path, monkeypatch):
    path = write_configuration(tmp_path / "invalid.json", rate_limit_refill_rate=0)
    monkeypatch.setenv("HEIMDALL_CONFIG", str(path))
    with pytest.raises(ValidationError, match="rate_limit_refill_rate"):
        load_settings()


@pytest.mark.parametrize("root", [[], None, "not an object"])
def test_json_configuration_root_must_be_an_object(tmp_path, root):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(root), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)
