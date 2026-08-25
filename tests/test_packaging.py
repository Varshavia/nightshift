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
