"""Runtime settings read from environment variables.

Every service and mock builds one ``Settings`` object at start-up. Values come from the
container environment (see ``docker-compose.yml`` and ``.env.example``). Plain Pydantic
is used rather than a settings library, to keep the dependency list short.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel, Field


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


class BackoffSettings(BaseModel):
    """Retry backoff for relays and consumers. Real time, never the shared clock."""

    base_seconds: float
    factor: float
    cap_seconds: float
    max_attempts: int

    def delay(self, attempt: int) -> float:
        """Delay before retry number ``attempt`` (1-based)."""
        return min(self.cap_seconds, self.base_seconds * (self.factor ** max(0, attempt - 1)))


class Settings(BaseModel):
    service_name: str
    port: int
    log_level: str = "INFO"
    test_mode: bool = False
    test_key: str = ""

    database_url: str | None = None
    testsupport_database_url: str | None = None
    kafka_bootstrap: str | None = None

    jwt_issuer: str = "cardvault-auth"
    jwt_audience: str = "cardvault"
    auth_jwks_url: str | None = None

    # One key per caller (spec 16.6 rule 40). Caller name -> key.
    service_keys: dict[str, str] = Field(default_factory=dict)

    event_backoff: BackoffSettings
    http_backoff: BackoffSettings

    @classmethod
    def from_env(cls) -> Settings:
        test_mode = _bool("TEST_MODE")
        # Fast defaults under TEST_MODE so tests do not wait on long retries (rule 31).
        base_default = 0.2 if test_mode else 1.0
        cap_default = 2.0 if test_mode else 60.0
        keys = {
            caller: value
            for caller, env in (
                ("network-mock", "SERVICE_KEY_NETWORK_MOCK"),
                ("wallet-operator-mock", "SERVICE_KEY_OPERATOR_MOCK"),
                ("tests", "SERVICE_KEY_TESTS"),
            )
            if (value := os.environ.get(env))
        }
        return cls(
            service_name=os.environ.get("SERVICE_NAME", "unknown"),
            port=_int("PORT", 8000),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            test_mode=test_mode,
            test_key=os.environ.get("TEST_KEY", ""),
            database_url=os.environ.get("DATABASE_URL") or None,
            testsupport_database_url=os.environ.get("TESTSUPPORT_DATABASE_URL") or None,
            kafka_bootstrap=os.environ.get("KAFKA_BOOTSTRAP") or None,
            jwt_issuer=os.environ.get("JWT_ISSUER", "cardvault-auth"),
            jwt_audience=os.environ.get("JWT_AUDIENCE", "cardvault"),
            auth_jwks_url=os.environ.get("AUTH_JWKS_URL") or None,
            service_keys=keys,
            event_backoff=BackoffSettings(
                base_seconds=_float("RELAY_BACKOFF_BASE_SECONDS", base_default),
                factor=_float("RELAY_BACKOFF_FACTOR", 2.0),
                cap_seconds=_float("RELAY_BACKOFF_CAP_SECONDS", cap_default),
                max_attempts=0,  # the event relay retries forever (rule 31)
            ),
            http_backoff=BackoffSettings(
                base_seconds=_float("HTTP_BACKOFF_BASE_SECONDS", base_default),
                factor=_float("HTTP_BACKOFF_FACTOR", 2.0),
                cap_seconds=_float("HTTP_BACKOFF_CAP_SECONDS", cap_default),
                max_attempts=_int("HTTP_BACKOFF_MAX_ATTEMPTS", 5),
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
