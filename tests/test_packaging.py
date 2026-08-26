"""Every service image must install what the domain package imports.

The services put ``packages/`` on ``PYTHONPATH`` rather than pip-installing it,
which is fast and simple and means nothing installs the dependencies
``pyproject.toml`` declares for ``nightshift_core``. Each service's
``requirements.txt`` has to carry them itself.

The gap is invisible locally — the development environment has everything — and
shows up only when a container starts in the cloud. It cost one deployment
already: adding ``packaging`` to models.py for advisory consolidation broke the
API image alone, and the symptom was a revision that never answered its health
probe, three build-and-deploy cycles away from the cause.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ("api", "scanner", "worker")


def distribution(requirement: str) -> str:
    """`httpx>=0.27` and `uvicorn[standard]>=0.30` both name one distribution."""
    return re.split(r"[<>=!\[; ]", requirement.strip(), maxsplit=1)[0].lower()


def domain_dependencies() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {distribution(item) for item in data["project"]["dependencies"]}


def service_requirements(service: str) -> set[str]:
    text = (ROOT / "services" / service / "requirements.txt").read_text(encoding="utf-8")
    return {
        distribution(line)
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


#: Which distribution provides which ``google.cloud`` module. Short, explicit,
#: and only what this project actually imports — inferring it would mean
#: querying PyPI from a unit test, which is a worse idea than typing four lines.
_GOOGLE_CLOUD_DISTRIBUTIONS = {
    "firestore": "google-cloud-firestore",
    "pubsub_v1": "google-cloud-pubsub",
    "trace_v1": "google-cloud-trace",
    "storage": "google-cloud-storage",
}


def google_cloud_imports(service: str) -> set[str]:
    """Distributions a service imports from ``google.cloud``, found by reading.

    These imports live *inside* functions, deliberately: it keeps the domain
    runnable on a laptop with no cloud libraries. The cost is that a missing one
    is invisible until a container reaches that exact line in production, which
    is how the worker shipped without `google-cloud-pubsub` and died the first
    time anything asked it to consume a message.
    """
    found: set[str] = set()
    for path in (ROOT / "services" / service).rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for module in re.findall(r"from google\.cloud import (\w+)", text):
            found.add(_GOOGLE_CLOUD_DISTRIBUTIONS.get(module, f"google-cloud-{module}"))
        for module in re.findall(r"import google\.cloud\.(\w+)", text):
            found.add(_GOOGLE_CLOUD_DISTRIBUTIONS.get(module, f"google-cloud-{module}"))
    return found


@pytest.mark.parametrize("service", SERVICES)
def test_a_service_installs_the_cloud_libraries_it_imports(service: str) -> None:
    """The check that would have caught the worker shipping without Pub/Sub.

    The earlier test asserts each image installs what the *domain* declares.
    This one asks the narrower and more dangerous question: does the service
    install what its own code reaches for at run time?
    """
    missing = google_cloud_imports(service) - service_requirements(service)
    assert not missing, (
        f"services/{service} imports {sorted(missing)} but does not install it. "
        "The import is inside a function, so this fails in the cloud rather than here."
    )


@pytest.mark.parametrize("service", SERVICES)
def test_a_service_installs_everything_the_domain_imports(service: str) -> None:
    missing = domain_dependencies() - service_requirements(service)
    assert not missing, (
        f"services/{service}/requirements.txt is missing {sorted(missing)}, which "
        "nightshift_core needs. The container will start, import the domain, and die "
        "on the health probe."
    )


def test_the_domain_declares_what_it_actually_imports() -> None:
    """Guards the other direction: an import that pyproject never declared.

    Adding one to a module is easy and the development environment hides it,
    because everything is installed there. The list below is short by design —
    the domain is meant to depend on almost nothing.
    """
    declared = domain_dependencies()
    assert declared == {"httpx", "packaging", "opentelemetry-api"}, (
        "the domain's dependencies changed; add the new one to every "
        "services/*/requirements.txt in the same commit"
    )


def test_both_images_can_fetch_an_interpreter() -> None:
    """`uv` is what makes interpreter.py more than a preference.

    Without it the module falls back to the worker's own Python, silently and
    by design — so an image that ships the code and not the tool would keep
    offering 3.12 to projects that asked for 3.9 while the tests all passed.
    """
    root = Path(__file__).resolve().parent.parent
    for name in ("services/worker/Dockerfile", "infra/probe.Dockerfile"):
        text = (root / name).read_text(encoding="utf-8")
        assert "astral-sh/uv" in text, f"{name} cannot fetch an interpreter"


def test_the_probe_and_the_worker_are_built_the_same_way() -> None:
    """The probe predicts what the fleet will do. It cannot do that from a
    richer environment than the fleet has — or a poorer one."""
    root = Path(__file__).resolve().parent.parent
    worker = (root / "services/worker/Dockerfile").read_text(encoding="utf-8")
    probe = (root / "infra/probe.Dockerfile").read_text(encoding="utf-8")
    for library in ("default-libmysqlclient-dev", "libpq-dev", "libxslt1-dev", "astral-sh/uv"):
        assert (library in worker) == (library in probe), f"{library} is in one image only"
