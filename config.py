import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    HttpUrl,
    field_validator,
)

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")


class BackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    weight: int = Field(default=1, gt=0, strict=True)

    @field_validator("url")
    @classmethod
    def require_backend_origin(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("Backend URLs must not contain credentials")
        if value.path not in (None, "", "/"):
            raise ValueError("Backend URLs must not contain a path prefix")
        if value.query is not None or value.fragment is not None:
            raise ValueError("Backend URLs must not contain a query or fragment")
        return value

    @property
    def base_url(self) -> str:
        return str(self.url).rstrip("/")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backends: list[BackendConfig] = Field(min_length=1)

    balancing_algorithm: Literal[
        "round_robin",
        "weighted_round_robin",
        "least_in_flight",
    ] = "least_in_flight"

    rate_limit_capacity: int = Field(default=10, gt=0, strict=True)
    rate_limit_refill_rate: FiniteFloat = Field(default=2.0, gt=0, strict=True)
    backend_timeout: FiniteFloat = Field(default=2.0, gt=0, strict=True)
    health_check_timeout: FiniteFloat = Field(default=1.0, gt=0, strict=True)
    health_check_interval: FiniteFloat = Field(default=2.0, gt=0, strict=True)
    forwarding_timeout: FiniteFloat = Field(default=3.0, gt=0, strict=True)

    @field_validator("backends")
    @classmethod
    def require_unique_backends(
        cls, backends: list[BackendConfig],
    ) -> list[BackendConfig]:
        seen = set()
        for backend in backends:
            if backend.base_url in seen:
                raise ValueError(f"Duplicate backend URL: {backend.base_url}")
            seen.add(backend.base_url)
        return backends


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        path = os.environ.get("HEIMDALL_CONFIG")

    if path is None:
        config_path = DEFAULT_CONFIG_PATH
    else:
        if isinstance(path, str) and not path.strip():
            raise ValueError("Configuration path must not be empty")
        config_path = Path(path).expanduser()

    contents = config_path.read_text(encoding="utf-8")
    return Settings.model_validate_json(contents)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Reuse startup settings; explicit load_settings calls remain independent."""
    return load_settings()
