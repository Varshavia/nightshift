"""Which Python a repository wants, and how to get hold of it.

The fleet built every repository with one interpreter — the one running the
worker, 3.12 — and recorded whatever happened. What happened, in a fifty-seven
repository run, was that fifty-one never reached the upgrade. Some of that is
genuinely other people's environments. A large part of it is this file's
absence.

Python 3.12 removed ``distutils``. A great many projects pinned before 2023
install through a ``setup.py`` that imports it, and they fail on a line that has
nothing to do with the project: they were written for an interpreter we simply
declined to offer. Old pins also frequently have no 3.12 wheel, so pip falls
back to building from source and fails for a third unrelated reason.

Every one of those arrives as ``UNBUILDABLE``, which reads as a fact about the
repository. It was a fact about our container.

**The repository already says what it wants.** ``requires-python`` in
``pyproject.toml``, ``python_requires`` in ``setup.cfg`` or ``setup.py``, the
``python-version`` its own CI runs. This module reads that and asks ``uv`` for a
matching interpreter, which takes seconds and is cached after the first time.

When nothing says, or ``uv`` is not installed, the caller keeps the interpreter
it already had. A missing tool degrades the fleet to what it does today; it does
not fail a job.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet

__all__ = [
    "CANDIDATES",
    "FALLBACK",
    "InterpreterChoice",
    "choose_interpreter",
    "declared_requirement",
    "resolve",
]

log = logging.getLogger("nightshift.interpreter")

#: The versions the fleet is willing to fetch, newest first.
#:
#: Newest first because a project that accepts a range is usually happiest at
#: the top of it — its dependencies have wheels there and its CI probably runs
#: there. The floor is 3.8: below that pip itself starts to disagree with the
#: modern packaging stack, and a repository that cannot be built on 3.8 is not
#: one this project can help tonight.
CANDIDATES = ("3.12", "3.11", "3.10", "3.9", "3.8")

#: The interpreter to try when the repository said nothing and the modern one
#: could not build it.
#:
#: `requires-python` only became common around 2019, so a repository dormant
#: since before then declares nothing and gets whatever the worker is running.
#: What it actually pins is the world of its last commit — `click 5.0`,
#: `flask 1.1.1`, `jinja2 2.10.1` — and those were built for an interpreter that
#: still had `distutils` and still had wheels published for it.
#:
#: 3.9 rather than 3.8: 3.8 is far enough past its end of life that PyPI is
#: starting to lose the wheels, and a fallback that has to build everything from
#: source is a fallback that times out.
FALLBACK = "3.9"

#: How long `uv` gets to fetch an interpreter. Generous because the first fetch
#: for a version downloads a build; every later one is a cache hit.
INSTALL_TIMEOUT = 300.0

_PYTHON_REQUIRES = re.compile(r"""python_requires\s*=\s*["']([^"']+)["']""")
_CI_VERSION = re.compile(r"""python[-_]version:\s*["']?([0-9]+\.[0-9]+)""")


@dataclass(frozen=True, slots=True)
class InterpreterChoice:
    """Which interpreter to build with, and why that one.

    ``source`` is carried because an ``UNBUILDABLE`` verdict is only useful if
    the next person can tell whether we offered the repository what it asked
    for. "We tried 3.12 because nothing said otherwise" and "we tried 3.9
    because pyproject.toml asked for <3.10" are different failures.
    """

    python: Path
    version: str = ""
    requirement: str = ""
    source: str = "the worker's own interpreter"


def declared_requirement(repo_path: Path) -> tuple[str, str]:
    """What the repository says about the interpreter it wants.

    Returns the specifier and where it came from, or two empty strings. The
    order is by authority, not convenience: packaging metadata is a promise the
    project makes to anyone installing it, while a CI workflow is one machine's
    habit — usually right, occasionally a leftover.
    """
    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        declared = data.get("project", {}).get("requires-python")
        if isinstance(declared, str) and declared.strip():
            return declared.strip(), "pyproject.toml requires-python"

    for name in ("setup.cfg", "setup.py"):
        candidate = repo_path / name
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _PYTHON_REQUIRES.search(text)
        if match:
            return match.group(1).strip(), f"{name} python_requires"

    # Last, and as a single version rather than a range: a workflow says "we run
    # on 3.9", not "we support 3.8 upwards". Treated as an equality so the fleet
    # builds what their CI builds.
    workflows = repo_path / ".github" / "workflows"
    if workflows.is_dir():
        for path in sorted(workflows.glob("*.y*ml")):
            try:
                match = _CI_VERSION.search(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if match:
                return f"=={match.group(1)}.*", f".github/workflows/{path.name}"

    return "", ""


def _satisfying(requirement: str) -> list[str]:
    """The candidate versions the requirement allows, best first."""
    try:
        specifier = SpecifierSet(requirement)
    except InvalidSpecifier:
        log.warning("could not read python requirement %r; ignoring it", requirement)
        return []
    # `.0` because a bare "3.9" is not a version a specifier can test, and the
    # patch level never appears in a `requires-python` bound that matters here.
    return [version for version in CANDIDATES if specifier.contains(f"{version}.0")]


def resolve(version: str, *, timeout: float = INSTALL_TIMEOUT) -> Path | None:
    """Get an interpreter for ``version`` from ``uv``. None when we cannot.

    Install first and find second, because ``uv python install`` is a no-op on a
    version already present and saves a round trip on the common path.
    """
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        subprocess.run(
            [uv, "python", "install", version],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        found = subprocess.run(
            [uv, "python", "find", version],
            capture_output=True, text=True, timeout=30.0, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("uv could not provide python %s: %s", version, exc)
        return None

    path = Path((found.stdout or "").strip())
    return path if found.returncode == 0 and path.is_file() else None


def choose_interpreter(repo_path: Path) -> InterpreterChoice:
    """The interpreter to build this repository with.

    Never raises and never returns nothing: a repository that says nothing, a
    requirement we cannot parse, and a machine without ``uv`` all end at the
    interpreter already running, which is exactly what the fleet did before this
    file existed. The point is not to be clever; it is to stop offering 3.12 to
    a project that told us it wanted 3.9.
    """
    fallback = InterpreterChoice(python=Path(sys.executable))

    requirement, source = declared_requirement(repo_path)
    if not requirement:
        return fallback

    allowed = _satisfying(requirement)
    if not allowed:
        # Worth saying out loud rather than quietly falling back. A repository
        # that wants 3.6 is not one we are failing to build; it is one outside
        # the range this fleet supports, and those are different sentences.
        log.info("no supported interpreter satisfies %r (%s)", requirement, source)
        return fallback

    running = ".".join(str(part) for part in sys.version_info[:2])
    if running in allowed:
        return InterpreterChoice(
            python=Path(sys.executable), version=running, requirement=requirement, source=source
        )

    for version in allowed:
        python = resolve(version)
        if python is not None:
            log.info("building with python %s because %s says %r", version, source, requirement)
            return InterpreterChoice(
                python=python, version=version, requirement=requirement, source=source
            )

    log.info("could not obtain any of %s; falling back", ", ".join(allowed))
    return fallback
