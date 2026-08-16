"""Settings, read from the environment exactly once.

No secret is ever committed: every credential-shaped value is read from the
environment and has no default. Ceilings do have defaults, deliberately
conservative ones — a missing environment variable must never mean "unbounded".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

__all__ = ["Ceilings", "Settings", "get_settings"]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Ceilings:
    """Hard stops. Every loop has a ceiling — there are no exceptions.

    A job that hits any one of these terminates with a real ``Outcome`` rather
    than an error: hitting a ceiling is an answer, not a failure of the system.
    """

    max_repair_attempts: int = 4
    max_job_seconds: int = 1800
    max_job_tokens: int = 400_000
    max_concurrent_workers: int = 25

    @classmethod
    def from_env(cls) -> Ceilings:
        return cls(
            max_repair_attempts=_env_int("NIGHTSHIFT_MAX_REPAIR_ATTEMPTS", 4),
            max_job_seconds=_env_int("NIGHTSHIFT_MAX_JOB_SECONDS", 1800),
            max_job_tokens=_env_int("NIGHTSHIFT_MAX_JOB_TOKENS", 400_000),
            max_concurrent_workers=_env_int("NIGHTSHIFT_MAX_CONCURRENT_WORKERS", 25),
        )


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the fleet needs to know about where it is running."""

    gcp_project: str = ""
    gcp_region: str = "us-central1"
    jobs_topic: str = "nightshift-jobs"
    firestore_database: str = "(default)"

    gemini_model: str = "gemini-3.5-pro"
    triage_model: str = "gemma-3-27b-it"
    model_backend: str = "vertex"

    fork_org: str = ""
    #: Read from the environment, never logged, never persisted, never defaulted.
    github_token: str | None = None

    allow_upstream_prs: bool = False
    ceilings: Ceilings = field(default_factory=Ceilings)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            gcp_project=_env("NIGHTSHIFT_GCP_PROJECT"),
            gcp_region=_env("NIGHTSHIFT_GCP_REGION", "us-central1"),
            jobs_topic=_env("NIGHTSHIFT_JOBS_TOPIC", "nightshift-jobs"),
            firestore_database=_env("NIGHTSHIFT_FIRESTORE_DATABASE", "(default)"),
            gemini_model=_env("NIGHTSHIFT_GEMINI_MODEL", "gemini-3.5-pro"),
            triage_model=_env("NIGHTSHIFT_TRIAGE_MODEL", "gemma-3-27b-it"),
            model_backend=_env("NIGHTSHIFT_MODEL_BACKEND", "vertex"),
            fork_org=_env("NIGHTSHIFT_FORK_ORG"),
            github_token=_env("GITHUB_TOKEN") or None,
            allow_upstream_prs=_env_bool("ALLOW_UPSTREAM_PRS", False),
            ceilings=Ceilings.from_env(),
        )

    def require_cloud(self) -> None:
        """Fail loudly at startup rather than mysteriously at the first call."""
        missing = [
            name
            for name, value in (
                ("NIGHTSHIFT_GCP_PROJECT", self.gcp_project),
                ("NIGHTSHIFT_FORK_ORG", self.fork_org),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "missing required environment variables: " + ", ".join(sorted(missing))
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so the environment is read once."""
    return Settings.from_env()
