"""Clone, build, test, upgrade. The unglamorous half, and the hard one.

Test environment setup is the real difficulty in this project — not the agent.
A large fraction of third-party Python repositories will not install cleanly in
a fresh container, and a further fraction arrive with a suite that is already
failing. Everything here is written on that assumption: each step reports a
result rather than raising a surprise, and the failure modes map onto members of
:class:`~nightshift_core.models.Outcome` rather than into a log nobody reads.

This module calls no model. That is what makes the probe in
``scripts/probe_fleet.py`` free to run across the whole fleet, and it is why
this code can be developed and trusted before a single token is spent.

A note on the policy engine: it gates the calls the *agent* makes, because the
agent is the untrusted party. The toolchain is our own code running our own
fixed command lines, so it does not route through the engine — wrapping it would
be theatre, and worse, it would blur where the trust boundary actually is.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from nightshift_core.manifests import RECOGNISED_MANIFESTS, parse_manifest, rewrite_pin
from nightshift_core.models import Dependency, Vulnerability

__all__ = [
    "DiffStats",
    "EnvironmentBuildError",
    "Sandbox",
    "TestReport",
    "UpgradeError",
    "apply_upgrade",
    "build_environment",
    "capture_diff",
    "clone",
    "diff_stats",
    "discover_manifests",
    "run_tests",
]

log = logging.getLogger("nightshift.toolchain")

#: How much test output to keep. The tail is what matters — the traceback and
#: the summary line live at the end — and the head is usually collection noise.
#: This bound is also a token bound: it is what the repair agent will be reading.
MAX_OUTPUT_CHARS = 20_000

CLONE_TIMEOUT = 180
INSTALL_TIMEOUT = 900
TEST_TIMEOUT = 900


class EnvironmentBuildError(RuntimeError):
    """The repository's dependencies could not be installed.

    Its own type rather than a bare ``RuntimeError`` because this is the most
    common way a job ends at fleet scale, and it must map to ``UNBUILDABLE`` — a
    counted result — rather than disappear into a generic error path.
    """


class UpgradeError(RuntimeError):
    """The manifest could not be repinned, or the new version would not install."""


@dataclass(frozen=True, slots=True)
class TestReport:
    """One invocation of the repository's own test suite."""

    passed: bool
    output: str
    duration_seconds: float
    #: False when pytest collected nothing (exit 5). A repository with no tests
    #: cannot serve as its own evidence, so it is not one we can help.
    collected: bool = True
    exit_code: int = 0

    @property
    def internal_error(self) -> bool:
        """True for pytest's own failures (3) and misuse of its CLI (4).

        These are faults on *our* side of the line — a broken invocation, not a
        broken repository — so they become ``INFRA_ERROR`` rather than being
        blamed on the code under test.

        Exit 2 is deliberately not in here. It means collection was interrupted,
        which after an upgrade is the single most common shape of a real break:
        the new version removed a name and the import fails before any test
        runs. Treating it as our error would discard exactly the cases this
        project exists to repair.
        """
        return self.exit_code in {3, 4}


@dataclass(slots=True)
class Sandbox:
    """A cloned repository and the interpreter its dependencies live in."""

    repo_path: Path
    python: Path
    install_log: list[str] = field(default_factory=list)

    def run(
        self, argv: Sequence[str | Path], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        """Run a command in the clone with a clean, non-interactive environment."""
        env = dict(os.environ)
        env.update(
            PIP_DISABLE_PIP_VERSION_CHECK="1",
            PYTHONDONTWRITEBYTECODE="1",
            GIT_TERMINAL_PROMPT="0",
            # Stops a repository's own tooling from opening a pager or an editor
            # and hanging the worker until the wall-clock ceiling kills it.
            PAGER="cat",
            EDITOR="true",
        )
        # Fixed argv, never a shell string: nothing here is interpolated by a shell.
        return subprocess.run(
            [str(part) for part in argv],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )


def _tail(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return "... [truncated] ...\n" + text[-limit:]


# --------------------------------------------------------------------------- #
# Clone
# --------------------------------------------------------------------------- #


def clone(repo: str, workspace: Path, *, token: str | None = None) -> Path:
    """Shallow-clone ``owner/name`` into ``workspace/repo``.

    Shallow because the worker needs a working tree, not a history: full clones
    of a few hundred repositories a night is bandwidth spent on nothing. The
    token, when present, is passed through the URL and never logged.
    """
    destination = workspace / "repo"
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    credentials = f"x-access-token:{token}@" if token else ""
    url = f"https://{credentials}github.com/{repo}.git"

    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", url, str(destination)],
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        redacted = result.stderr.replace(token, "***") if token else result.stderr
        raise EnvironmentBuildError(f"clone of {repo} failed: {_tail(redacted, 2000)}")
    return destination


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


def discover_manifests(repo_path: Path) -> dict[str, str]:
    """Recognised manifests present in the clone, as ``{relative path: text}``."""
    found: dict[str, str] = {}
    for name in RECOGNISED_MANIFESTS:
        candidate = repo_path / name
        if candidate.is_file():
            try:
                found[name] = candidate.read_text(encoding="utf-8")
            except UnicodeDecodeError:  # pragma: no cover - rare, but real
                log.warning("%s is not utf-8, skipping", name)
    return found


def read_dependencies(repo_path: Path) -> list[Dependency]:
    """Every exact pin in the clone, across every manifest we recognise."""
    dependencies: list[Dependency] = []
    seen: set[tuple[str, str]] = set()
    for path, text in discover_manifests(repo_path).items():
        for dependency in parse_manifest(text, path):
            key = (dependency.name, dependency.version)
            if key not in seen:
                seen.add(key)
                dependencies.append(dependency)
    return dependencies


def _install_plan(repo_path: Path) -> list[list[str]]:
    """Candidate install commands, best first.

    The extras are tried before the bare install on purpose. A project's test
    dependencies almost always live in an extra, and installing without them
    produces a suite that fails at import — which we would then mislabel
    ``BASELINE_RED`` and blame on the repository. Getting this order wrong
    poisons the headline number, so it is worth the extra attempts.
    """
    plan: list[list[str]] = []
    packaged = (repo_path / "pyproject.toml").is_file() or (repo_path / "setup.py").is_file()
    if packaged:
        plan.extend(
            [["-e", f".[{extra}]"] for extra in ("test", "tests", "dev", "testing", "all")]
        )
        plan.append(["-e", "."])
    for name in RECOGNISED_MANIFESTS:
        if name.endswith(".txt") and (repo_path / name).is_file():
            plan.append(["-r", name])
    return plan


def build_environment(repo_path: Path, *, venv_path: Path | None = None) -> Sandbox:
    """Create a virtualenv and install the repository into it.

    Raises :class:`EnvironmentBuildError` when nothing works — which becomes
    ``UNBUILDABLE``, a counted outcome rather than a swallowed exception.
    """
    venv_path = venv_path or repo_path.parent / ".venv"
    created = subprocess.run(
        ["python3", "-m", "venv", str(venv_path)],
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT,
        check=False,
    )
    if created.returncode != 0:
        raise EnvironmentBuildError(f"virtualenv creation failed: {_tail(created.stderr, 2000)}")

    python = venv_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    sandbox = Sandbox(repo_path=repo_path, python=python)

    sandbox.run(
        [python, "-m", "pip", "install", "-q", "-U", "pip", "setuptools", "wheel"],
        timeout=INSTALL_TIMEOUT,
    )

    plan = _install_plan(repo_path)
    if not plan:
        raise EnvironmentBuildError("no recognised manifest and no packaging metadata")

    installed = False
    for arguments in plan:
        result = sandbox.run([python, "-m", "pip", "install", "-q", *arguments],
                             timeout=INSTALL_TIMEOUT)
        sandbox.install_log.append(f"pip install {' '.join(arguments)} -> {result.returncode}")
        if result.returncode == 0:
            installed = True
            break

    if not installed:
        raise EnvironmentBuildError(
            "every install strategy failed:\n" + "\n".join(sandbox.install_log)
        )

    # pytest may not be a declared dependency even when the suite is written for
    # it. Installing it is not "fixing" the repository — it is supplying the
    # runner, the way CI would.
    if sandbox.run([python, "-c", "import pytest"], timeout=60).returncode != 0:
        sandbox.run([python, "-m", "pip", "install", "-q", "pytest"], timeout=INSTALL_TIMEOUT)

    return sandbox


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def run_tests(sandbox: Sandbox, *, timeout: int = TEST_TIMEOUT) -> TestReport:
    """Run the repository's own suite. Never modifies it.

    pytest's exit codes are meaningful and are kept: ``5`` means nothing was
    collected, which is a different statement from "the tests failed" and must
    not be flattened into one.
    """
    import time

    started = time.monotonic()
    try:
        result = sandbox.run(
            [sandbox.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no"],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TestReport(
            passed=False,
            output=f"test run exceeded {timeout}s and was killed",
            duration_seconds=float(timeout),
            collected=True,
            exit_code=124,
        )

    duration = time.monotonic() - started
    output = _tail((result.stdout or "") + (result.stderr or ""))
    return TestReport(
        passed=result.returncode == 0,
        output=output,
        duration_seconds=duration,
        collected=result.returncode != 5,
        exit_code=result.returncode,
    )


# --------------------------------------------------------------------------- #
# Upgrade
# --------------------------------------------------------------------------- #


def apply_upgrade(sandbox: Sandbox, vulnerabilities: Sequence[Vulnerability]) -> list[str]:
    """Repin every fixable advisory and install the new versions.

    Returns the manifest paths that changed. Raises :class:`UpgradeError` if a
    package could not be repinned or the new version would not install — an
    upgrade that half-happened must not be reported as an upgrade.
    """
    manifests = discover_manifests(sandbox.repo_path)
    changed: list[str] = []

    for vulnerability in vulnerabilities:
        if not vulnerability.fixed_version:
            continue
        repinned = False
        for path, text in list(manifests.items()):
            try:
                updated = rewrite_pin(
                    text, vulnerability.package, vulnerability.fixed_version, path
                )
            except LookupError:
                continue
            (sandbox.repo_path / path).write_text(updated, encoding="utf-8")
            manifests[path] = updated
            if path not in changed:
                changed.append(path)
            repinned = True
        if not repinned:
            raise UpgradeError(
                f"{vulnerability.package} was reported at {vulnerability.installed_version} "
                "but is not pinned in any manifest we can rewrite"
            )

    specifications = [
        f"{v.package}=={v.fixed_version}" for v in vulnerabilities if v.fixed_version
    ]
    if specifications:
        result = sandbox.run(
            [sandbox.python, "-m", "pip", "install", "-q", *specifications],
            timeout=INSTALL_TIMEOUT,
        )
        if result.returncode != 0:
            raise UpgradeError(
                "the fixed versions would not install:\n" + _tail(result.stderr, 4000)
            )
    return changed


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DiffStats:
    """Size of a repair, for the pull-request body and for the record.

    A small diff that is obviously right is worth more than a large one that
    happens to pass, so this number is reported rather than merely logged.
    """

    files: int = 0
    added: int = 0
    removed: int = 0


def capture_diff(sandbox: Sandbox) -> str:
    """Every change in the clone against HEAD, including new files.

    ``git add -N`` stages the *existence* of untracked files without their
    content, which is what makes them visible to ``git diff``. Without it a
    repair that adds a file would produce an empty diff and the pull request
    would understate what it is asking a human to approve.
    """
    sandbox.run(["git", "add", "-N", "."], timeout=60)
    result = sandbox.run(["git", "diff", "--no-color"], timeout=60)
    if result.returncode != 0:
        log.warning("git diff failed: %s", _tail(result.stderr, 500))
        return ""
    return _tail(result.stdout or "")


def diff_stats(diff: str) -> DiffStats:
    """Count files and changed lines in a unified diff."""
    files = added = removed = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return DiffStats(files=files, added=added, removed=removed)
