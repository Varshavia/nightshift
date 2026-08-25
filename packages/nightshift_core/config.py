"""Settings, read from the environment exactly once.

No secret is ever committed: every credential-shaped value is read from the
environment and has no default. Ceilings do have defaults, deliberately
conservative ones — a missing environment variable must never mean "unbounded".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

__all__ = ["Ceilings", "Settings", "get_settings", "load_env_file"]


#: Read once per process. Reading it again would not pick up edits anyway,
#: because the values have already been copied into ``os.environ``.
_ENV_FILE_LOADED = False


def load_env_file(path: str | Path = ".env") -> int:
    """Copy ``KEY=value`` lines from a dotenv file into the environment.

    The repository has shipped a ``.env.example`` since the first commit and
    nothing read the ``.env`` you were meant to copy it to, so every shell
    needed the variables exported by hand and a forgotten export looked like a
    missing token.

    **A real environment variable always wins.** Cloud Run sets its own, and a
    stale ``.env`` left in an image must not be able to override them — so this
    fills gaps rather than assigning. That also means exporting a variable for
    one command still does what you expect.

    No dependency: the format is four rules (comments, blank lines, ``export``
    prefixes, optional quotes) and importing a library to apply them would be a
    larger commitment than the parsing.
    """
    global _ENV_FILE_LOADED
    target = Path(path)
    if _ENV_FILE_LOADED or not target.is_file():
        return 0

    loaded = 0
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1

    _ENV_FILE_LOADED = True
    get_settings.cache_clear()
    return loaded


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
    #: Where clones are built. ``/workspace`` in the container; a temp directory
    #: locally, because a laptop has no ``/workspace`` and should not need one.
    workspace_root: str = "/workspace"

    #: The repair agent. Flash by default — the hackathon brief names it, it is
    #: markedly cheaper, and most breaks are a moved import rather than a puzzle.
    repair_model: str = "gemini-3.5-flash"
    #: Reached for once Flash has had its attempts. See ADR 0004.
    escalation_model: str = "gemini-3.5-pro"
    triage_model: str = "gemma-3-27b-it"
    model_backend: str = "vertex"

    fork_org: str = ""
    #: The reviewed list of repositories the fleet may touch. A path rather than
    #: a query: the fleet never discovers its own targets. See ADR 0002.
    fleet_pool_path: str = "fleet/pool.json"
    #: Read from the environment, never logged, never persisted, never defaulted.
    github_token: str | None = None
    #: Guards the one write the control tower exposes. Empty means the endpoint
    #: refuses everything, which is the right default: a dashboard deployed
    #: without one can be read by anyone and can send nothing upstream.
    approval_key: str = ""

    allow_upstream_prs: bool = False
    ceilings: Ceilings = field(default_factory=Ceilings)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            gcp_project=_env("NIGHTSHIFT_GCP_PROJECT"),
            gcp_region=_env("NIGHTSHIFT_GCP_REGION", "us-central1"),
            jobs_topic=_env("NIGHTSHIFT_JOBS_TOPIC", "nightshift-jobs"),
            firestore_database=_env("NIGHTSHIFT_FIRESTORE_DATABASE", "(default)"),
            workspace_root=_env("NIGHTSHIFT_WORKSPACE_ROOT", "/workspace"),
            repair_model=_env("NIGHTSHIFT_REPAIR_MODEL", "gemini-3.5-flash"),
            escalation_model=_env("NIGHTSHIFT_ESCALATION_MODEL", "gemini-3.5-pro"),
            triage_model=_env("NIGHTSHIFT_TRIAGE_MODEL", "gemma-3-27b-it"),
            model_backend=_env("NIGHTSHIFT_MODEL_BACKEND", "vertex"),
            fork_org=_env("NIGHTSHIFT_FORK_ORG"),
            fleet_pool_path=_env("NIGHTSHIFT_FLEET_POOL", "fleet/pool.json"),
            github_token=_env("GITHUB_TOKEN") or None,
            approval_key=_env("NIGHTSHIFT_APPROVAL_KEY"),
            allow_upstream_prs=_env_bool("ALLOW_UPSTREAM_PRS", False),
            ceilings=Ceilings.from_env(),
        )

    def require_project(self) -> None:
        """Enough to reach Firestore and Vertex, and nothing about forking.

        Split out from :meth:`require_cloud` because the control tower needs a
        project and has no opinion about where forks live — it never forks. The
        combined check made a read-only dashboard fail with "missing
        NIGHTSHIFT_FORK_ORG", which is true, unhelpful, and about a capability
        the service does not have.
        """
        if not self.gcp_project:
            raise RuntimeError("missing required environment variable: NIGHTSHIFT_GCP_PROJECT")

    def require_cloud(self) -> None:
        """What a service that writes to GitHub needs. Fail loudly at startup
        rather than mysteriously at the first call.

        The fork organisation is in here rather than in
        :meth:`require_project` because a worker that does not know where its
        forks live would discover that only when it tried to open a pull
        request, having already spent the tokens to earn one.
        """
        self.require_project()
        if not self.fork_org:
            raise RuntimeError(
                "missing required environment variable: NIGHTSHIFT_FORK_ORG — "
                "the fleet never operates outside it"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so the environment is read once."""
    load_env_file()
    return Settings.from_env()
