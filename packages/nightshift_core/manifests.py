"""Reading and rewriting Python dependency manifests.

Pure text in, pure data out. No filesystem, no network, no subprocess — which is
why this module can be tested against the ugly real-world manifests that break
naive parsers, and why those tests run in milliseconds.

Only **exact pins** become a :class:`~nightshift_core.models.Dependency`. A range
like ``requests>=2.0`` has no single installed version to compare against an OSV
advisory, so reporting it would mean guessing. Guessing is how a scanner
produces confident nonsense, so unpinned requirements are skipped and counted
rather than resolved.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable

from packaging.utils import canonicalize_name

from nightshift_core.models import Dependency

__all__ = [
    "RECOGNISED_MANIFESTS",
    "parse_manifest",
    "parse_pyproject",
    "parse_requirements",
    "rewrite_pin",
]

#: Manifests the scanner knows how to read. PyPI only, per ADR 0001.
RECOGNISED_MANIFESTS: tuple[str, ...] = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements/base.txt",
    "requirements/dev.txt",
    "requirements/test.txt",
    "test-requirements.txt",
    "pyproject.toml",
)

#: ``name[extra1,extra2]==1.2.3`` — the only form that carries a usable version.
_PIN_RE = re.compile(
    r"""
    ^\s*
    (?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)      # distribution name
    (?P<extras>\[[^\]]*\])?                   # optional extras
    \s*==\s*
    (?P<version>[A-Za-z0-9][A-Za-z0-9.*+!-]*) # exact version
    \s*
    (?P<rest>[;#].*)?                         # environment marker or comment
    $
    """,
    re.VERBOSE,
)

#: Lines that are instructions to pip rather than requirements.
_OPTION_PREFIXES = ("-", "--")


def _significant_lines(text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(index, line)`` for lines that could be a requirement.

    Continuations (a line ending in a backslash) are joined, because a manifest
    that splits a pin across two lines is still a pin.
    """
    joined: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for index, raw in enumerate(text.splitlines()):
        line = raw.rstrip()
        if not buffer:
            start = index
        if line.endswith("\\"):
            buffer += line[:-1].strip() + " "
            continue
        candidate = (buffer + line).strip()
        buffer = ""
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.startswith(_OPTION_PREFIXES):
            continue
        if "://" in candidate:  # direct URL or VCS reference; no version to read
            continue
        joined.append((start, candidate))
    return joined


def parse_requirements(text: str, path: str = "requirements.txt") -> list[Dependency]:
    """Exact pins from a pip requirements file.

    Comments, ``-r`` includes, editable installs, hashes, direct URLs and
    unpinned ranges are all skipped rather than guessed at.
    """
    found: list[Dependency] = []
    for _, line in _significant_lines(text):
        match = _PIN_RE.match(line.split("--hash")[0].strip())
        if match is None:
            continue
        version = match.group("version")
        if "*" in version:  # `==1.2.*` is a range wearing a pin's clothes
            continue
        found.append(
            Dependency(
                name=str(canonicalize_name(match.group("name"))),
                version=version,
                manifest_path=path,
            )
        )
    return found


def parse_pyproject(text: str, path: str = "pyproject.toml") -> list[Dependency]:
    """Exact pins from ``[project]`` dependencies and optional dependency groups.

    Poetry's ``[tool.poetry.dependencies]`` is read too, but only its ``==``
    entries: ``^1.2`` and ``~1.2`` are ranges, and a range has no installed
    version for OSV to answer about.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []

    specs: list[str] = []
    project = data.get("project", {})
    if isinstance(project.get("dependencies"), list):
        specs.extend(str(item) for item in project["dependencies"])
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for group in optional.values():
            if isinstance(group, list):
                specs.extend(str(item) for item in group)

    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    if isinstance(poetry, dict):
        for name, constraint in poetry.items():
            if name.lower() == "python" or not isinstance(constraint, str):
                continue
            joiner = "" if constraint.startswith("==") else " "
            specs.append(f"{name}{joiner}{constraint}")

    found: list[Dependency] = []
    for spec in specs:
        match = _PIN_RE.match(spec.strip())
        if match is None or "*" in match.group("version"):
            continue
        found.append(
            Dependency(
                name=str(canonicalize_name(match.group("name"))),
                version=match.group("version"),
                manifest_path=path,
            )
        )
    return found


def parse_manifest(text: str, path: str) -> list[Dependency]:
    """Dispatch on the manifest's filename."""
    if path.endswith(".toml"):
        return parse_pyproject(text, path)
    return parse_requirements(text, path)


def rewrite_pin(text: str, package: str, new_version: str, path: str) -> str:
    """Return ``text`` with ``package`` repinned to ``new_version``.

    Everything else on the line is preserved — extras, environment markers,
    trailing comments, indentation. The upgrade should read as a one-token diff,
    because a reviewer scanning the pull request must be able to see at a glance
    that nothing else moved.

    Raises ``LookupError`` when the package is not pinned in this manifest, so a
    silent no-op cannot be mistaken for a successful upgrade.
    """
    target = canonicalize_name(package)
    changed = False
    lines = text.splitlines(keepends=True)

    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Match inside quotes too, so pyproject dependency strings are covered.
        candidate = stripped.strip('",\'')
        match = _PIN_RE.match(candidate.split("--hash")[0].strip())
        if match is None or canonicalize_name(match.group("name")) != target:
            continue
        old = f"=={match.group('version')}"
        lines[index] = raw.replace(old, f"=={new_version}", 1)
        changed = True

    if not changed:
        raise LookupError(f"{package} is not pinned in {path}")
    return "".join(lines)
