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

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from packaging.version import InvalidVersion, Version

from nightshift_core.manifests import (
    RECOGNISED_MANIFESTS,
    declared_extras,
    dependency_group_specs,
    parse_manifest,
    rewrite_pin,
)
from nightshift_core.models import Dependency, Vulnerability
from services.worker.interpreter import choose_interpreter

__all__ = [
    "DiffStats",
    "EnvironmentBuildError",
    "Sandbox",
    "TestReport",
    "UpgradeDrift",
    "UpgradeError",
    "apply_upgrade",
    "build_environment",
    "capture_diff",
    "clone",
    "collection_counts",
    "diff_stats",
    "discover_manifests",
    "failing_ids",
    "installed_versions",
    "run_tests",
    "upgrade_drift",
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

    #: Not a test class. The name is right for the domain and wrong for pytest's
    #: collector, which would otherwise warn on every module that imports it.
    __test__ = False

    passed: bool
    output: str
    duration_seconds: float
    #: False when pytest collected nothing (exit 5). A repository with no tests
    #: cannot serve as its own evidence, so it is not one we can help.
    collected: bool = True
    exit_code: int = 0
    #: How many tests pytest found. Zero with no errors means the repository has
    #: no suite; zero *with* errors means we could not build its environment.
    tests_collected: int = 0
    #: How many modules failed to import. Compared across the upgrade rather
    #: than judged on its own: the number that matters is whether it grew.
    collection_errors: int = 0
    #: Which tests and modules were red. A set, not a count, so that the run
    #: after the upgrade can be diffed against the run before it.
    failures: frozenset[str] = frozenset()

    @property
    def internal_error(self) -> bool:
        """True for pytest's own failures (3) and misuse of its CLI (4).

        These were read as faults on *our* side of the line — a broken
        invocation rather than a broken repository. A fleet run corrected that:
        two repositories returned exit 3 and exit 4 on every single delivery
        while thirty-seven others took the identical invocation and ran. An
        argument list that is wrong is wrong everywhere, so what this actually
        marks is a repository whose own test runner will not start here —
        deterministic, and therefore not something a retry can improve.

        Exit 2 is deliberately not in here. It means collection was interrupted,
        which after an upgrade is the single most common shape of a real break:
        the new version removed a name and the import fails before any test
        runs. Treating it as our error would discard exactly the cases this
        project exists to repair.
        """
        return self.exit_code in {3, 4}

    @property
    def collection_failed(self) -> bool:
        """The suite could not be assembled, as opposed to run and failed.

        The same exit code means opposite things either side of an upgrade, and
        this property exists so the caller can tell them apart *by phase* rather
        than by guessing.

        Before we change anything, a collection error means the environment is
        incomplete — a Django project with no ``DJANGO_SETTINGS_MODULE``, a suite
        whose fixtures want a database that is not running, a project driven by
        tox rather than by bare pytest. Seven of eleven repositories in the first
        real pool landed here, and calling that ``BASELINE_RED`` claimed those
        repositories arrived broken. They did not; we could not run them. The
        difference matters because ``BASELINE_RED`` is subtracted from the
        denominator of the number this project is judged on, and a denominator
        padded with our own failures flatters us.

        After an upgrade the identical output means the new version removed a
        name and the import died — the break we exist to repair. So this is never
        consulted there.
        """
        return self.exit_code == 2 and (
            "error" in self.output.lower() and "collect" in self.output.lower()
        )


@dataclass(slots=True)
class Sandbox:
    """A cloned repository and the interpreter its dependencies live in."""

    repo_path: Path
    python: Path
    install_log: list[str] = field(default_factory=list)
    #: Extra environment the suite needs to run at all — in practice
    #: ``DJANGO_SETTINGS_MODULE``. Discovered rather than configured, and kept
    #: here so that every later command sees the same environment the successful
    #: collection saw. Never a place for credentials: the policy engine forbids
    #: the agent reading it, and nothing in it survives the job.
    env: dict[str, str] = field(default_factory=dict)

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
        env.update(self.env)
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


def _silent_failure(returncode: int) -> str:
    """What to record when a failed command wrote nothing at all.

    `SkyRL` and `VeOmni` both arrived as "the project would not install" with an
    empty `last error:` under it, which is the least useful sentence the fleet
    can produce: it names a repository without naming a cause, and two of them
    in one run look like a pattern in other people's code rather than one in
    ours.

    A negative return code is a signal, and a build that is killed mid-wheel has
    not written its error yet — the usual reason being that the container ran
    out of memory linking something large. That is a line in the deployment, not
    a fact about the repository, and the note has to be able to say so.
    """
    if returncode < 0:
        return (
            f"the command was killed by signal {-returncode} before it wrote anything; "
            "on this fleet that is almost always the container running out of memory "
            "while a wheel is being built"
        )
    return f"the command exited {returncode} without writing anything to stdout or stderr"


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


#: Names projects conventionally give the group that holds their test
#: dependencies, whether as an extra or as a PEP 735 dependency group.
TEST_GROUP_NAMES: tuple[str, ...] = ("test", "tests", "dev", "testing", "all")

#: pip says this and exits zero. Treating that as success is how a build
#: installs nothing and calls itself finished.
_MISSING_EXTRA = "does not provide the extra"


def _base_install_plan(repo_path: Path) -> list[list[str]]:
    """How to get the project itself installed. Nothing about tests yet."""
    plan: list[list[str]] = []
    if (repo_path / "pyproject.toml").is_file() or (repo_path / "setup.py").is_file():
        plan.append(["-e", "."])
    for name in RECOGNISED_MANIFESTS:
        if name.endswith(".txt") and (repo_path / name).is_file():
            plan.append(["-r", name])
    return plan


def _test_dependency_plan(repo_path: Path) -> list[tuple[str, list[str]]]:
    """Ways to get the test dependencies, best first, as ``(label, pip args)``.

    Read out of the project rather than guessed at. ``pip install .[test]``
    exits **zero** when no such extra exists, so a builder that guesses names
    and trusts the exit code reports success having installed nothing — and the
    suite then fails at import. We would record that as ``BASELINE_RED`` and
    blame the repository for breakage we caused ourselves, which quietly
    corrupts the one number this project is judged on.

    Both places modern projects put these are covered: ``optional-dependencies``
    extras, and PEP 735 ``dependency-groups`` (which is where ``itsdangerous``
    and ``loguru`` keep theirs).
    """
    plan: list[tuple[str, list[str]]] = []
    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - rare, but real
            text = ""
        extras = set(declared_extras(text))
        for name in TEST_GROUP_NAMES:
            if name in extras:
                plan.append((f"extra:{name}", ["-e", f".[{name}]"]))
        for name in TEST_GROUP_NAMES:
            specs = dependency_group_specs(text, name)
            if specs:
                plan.append((f"group:{name}", specs))

    for name in RECOGNISED_MANIFESTS:
        if (
            name.endswith(".txt")
            and any(marker in name for marker in ("dev", "test"))
            and (repo_path / name).is_file()
        ):
            plan.append((f"file:{name}", ["-r", name]))
    return plan


#: How deep to look for a settings module. Django projects keep them near the
#: root or one package down; going further finds vendored copies and example
#: applications, which are the wrong answer and slow to rule out.
_SETTINGS_SEARCH_DEPTH = 4


def _looks_like_django(sandbox: Sandbox) -> bool:
    """Whether Django is installed in the sandbox at all.

    Asked of the environment rather than the manifest, because the manifest that
    names Django is often the dev-requirements file we already installed, and a
    project can depend on it transitively without ever naming it.
    """
    return sandbox.run([sandbox.python, "-c", "import django"], timeout=60).returncode == 0


def _django_settings_candidates(repo_path: Path) -> list[str]:
    """Dotted paths to plausible settings modules, most likely first.

    Test-specific settings are preferred over a project's real ones: they are
    written to run without a database server, which is the difference between a
    suite that starts and one that hangs waiting for postgres.
    """
    found: list[tuple[int, str]] = []
    for path in repo_path.rglob("*settings*.py"):
        relative = path.relative_to(repo_path)
        if len(relative.parts) > _SETTINGS_SEARCH_DEPTH:
            continue
        skip = {".venv", "venv", "node_modules", "build", "dist"}
        if any(part in skip for part in relative.parts):
            continue
        dotted = ".".join(relative.with_suffix("").parts)
        name = relative.name.lower()
        rank = 0 if "test" in name or "test" in str(relative.parent).lower() else 1
        found.append((rank, dotted))
    # Bounded: trying every settings module in a large repository would cost more
    # than the suite it is trying to start.
    return [dotted for _, dotted in sorted(found)][:6]


#: pytest reports its totals two different ways and we see both. A collection
#: run says "107 tests collected, 3 errors in 0.47s"; a full run says
#: "1 failed, 106 passed, 3 errors in 0.76s" and never uses the word collected.
#: Reading only the first shape reports an empty suite for a suite that ran.
_COLLECTED_RE = re.compile(r"(\d+)\s+tests?\s+collected")
_ERRORS_RE = re.compile(r"(\d+)\s+errors?\b")
_OUTCOME_RE = re.compile(r"(\d+)\s+(passed|failed|skipped|xfailed|xpassed)\b")

#: The individual casualties, which is what makes a before-and-after comparison
#: possible: "FAILED tests/test_auth.py::test_token - AssertionError" and
#: "ERROR tests/test_config.py".
_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


def collection_counts(output: str) -> tuple[int, int]:
    """How many tests pytest accounted for, and how many modules it could not import."""
    collected = _COLLECTED_RE.search(output)
    if collected:
        total = int(collected.group(1))
    else:
        total = sum(int(match.group(1)) for match in _OUTCOME_RE.finditer(output))
    errors = _ERRORS_RE.findall(output)
    return total, int(errors[-1]) if errors else 0


def failing_ids(output: str) -> frozenset[str]:
    """Which tests and modules pytest reported as failed or errored.

    Identities rather than a count, because the useful question is never "how
    many are red" but "which ones went red that were not red before". A suite
    with one pre-existing failure is still perfectly good evidence for whether
    an upgrade broke something — as long as that failure is named and set aside
    rather than used to disqualify the whole repository.
    """
    return frozenset(_FAILED_LINE_RE.findall(output))


def _collects(sandbox: Sandbox) -> bool:
    """Did pytest find any tests at all?

    Not "did pytest exit zero". That was the earlier rule and it discarded
    working repositories wholesale: ``flask-jwt-extended`` collects 107 tests
    and fails on three modules that import ``dateutil``, and a whole repository
    with a hundred usable tests was thrown away over three of them. Twelve of
    twenty-four repositories in the last pool were refused this way.

    A suite that yields some tests is a suite we can use as evidence. Which
    modules failed to import is recorded separately and compared before and
    after the upgrade — a module that imported cleanly and stops importing is
    not noise, it is the break.

    Exit 5 — nothing collected, no errors — still counts as a working
    environment: the repository simply has no tests, which is its own finding.
    """
    result = sandbox.run(
        [sandbox.python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        timeout=300,
    )
    if result.returncode in {0, 5}:
        return True
    collected, _ = collection_counts((result.stdout or "") + (result.stderr or ""))
    return collected > 0


def build_environment(repo_path: Path, *, venv_path: Path | None = None) -> Sandbox:
    """Create a virtualenv, install the project, then make its suite importable.

    Two phases on purpose. Getting the project installed is not the same problem
    as getting its tests runnable, and conflating them is what produced a
    misleading ``BASELINE_RED`` on three of the first four real repositories we
    probed.

    Raises :class:`EnvironmentBuildError` when the project itself cannot be
    installed — which becomes ``UNBUILDABLE``, a counted outcome rather than a
    swallowed exception.
    """
    venv_path = venv_path or repo_path.parent / ".venv"

    # Which Python, before anything is installed with it. The fleet used to
    # offer every repository the interpreter running the worker, and a project
    # that asked for 3.9 got 3.12 and failed on `distutils` — a verdict about
    # our container, filed as one about the repository. See interpreter.py.
    choice = choose_interpreter(repo_path)
    created = subprocess.run(
        # Never a bare `python3` we hope is on PATH: on a Windows machine that is
        # either absent or the Store's stub that opens a shop page, and the
        # failure arrives as "virtualenv creation failed" with an empty stderr.
        [str(choice.python), "-m", "venv", str(venv_path)],
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT,
        check=False,
    )
    if created.returncode != 0:
        raise EnvironmentBuildError(f"virtualenv creation failed: {_tail(created.stderr, 2000)}")

    python = venv_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    sandbox = Sandbox(repo_path=repo_path, python=python)
    # First line of the install log on purpose. Every later line is about the
    # project; this one is about what we offered it, and it is the first thing
    # worth knowing when the rest of the log is a wall of build errors.
    sandbox.install_log.append(
        f"interpreter {choice.version or 'inherited'} ({choice.source})"
        + (f" for {choice.requirement}" if choice.requirement else "")
    )

    # Why the last attempt failed, not merely that it did. The install log used
    # to record "base:-e . -> failed" and throw the output away, so every
    # unbuildable repository looked identical. They are not: one wanted Python
    # 3.12 and we offered 3.11, another wanted a MySQL client library that was
    # not in the image. The first is a repository we could build by choosing a
    # different interpreter, the second is a line in a Dockerfile — and neither
    # is "this project is broken", which is what the bare verdict implied.
    last_failure: list[str] = []

    def pip(arguments: Sequence[str], label: str) -> bool:
        # Not `-q`. Quiet pip is quiet about failures too, and the tail of a
        # verbose log is the error message we came for; the head of it is
        # discarded a few lines below anyway.
        try:
            result = sandbox.run(
                [python, "-m", "pip", "install", *arguments], timeout=INSTALL_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            sandbox.install_log.append(f"{label} -> timed out after {INSTALL_TIMEOUT}s")
            last_failure[:] = [f"pip was still running after {INSTALL_TIMEOUT}s and was killed"]
            return False
        combined = (result.stdout or "") + (result.stderr or "")
        ok = result.returncode == 0 and _MISSING_EXTRA not in combined
        sandbox.install_log.append(
            f"{label} -> " + ("ok" if ok else f"failed (exit {result.returncode})")
        )
        if not ok:
            last_failure[:] = [_tail(combined, 1200) or _silent_failure(result.returncode)]
        return ok

    pip(["-U", "pip", "setuptools", "wheel"], "bootstrap")

    # -- phase one: the project itself -------------------------------------- #
    base = _base_install_plan(repo_path)
    if not base:
        raise EnvironmentBuildError("no recognised manifest and no packaging metadata")
    if not any(pip(arguments, f"base:{' '.join(arguments)}") for arguments in base):
        raise EnvironmentBuildError(
            "the project would not install:\n"
            + "\n".join(sandbox.install_log)
            + ("\nlast error:\n" + last_failure[0] if last_failure else "")
        )

    # pytest may not be declared anywhere even when the suite is written for it.
    # Installing it is not "fixing" the repository — it is supplying the runner,
    # the way CI would.
    if sandbox.run([python, "-c", "import pytest"], timeout=60).returncode != 0:
        pip(["pytest"], "runner:pytest")

    # -- phase two: make the suite importable ------------------------------- #
    if _collects(sandbox):
        return sandbox
    for label, arguments in _test_dependency_plan(repo_path):
        pip(arguments, label)
        if _collects(sandbox):
            return sandbox

    # -- phase three: the settings module Django suites cannot start without -- #
    #
    # Narrow on purpose. Django is a large enough slice of the applications this
    # fleet targets to be worth its own strategy, and its failure is uniform: the
    # suite imports fine and dies on `DJANGO_SETTINGS_MODULE`, which is set by
    # tox or manage.py in every one of these projects and by nothing at all when
    # pytest is invoked bare. Every other framework gets no special case, because
    # a builder that accumulates one per framework becomes a worse version of
    # tox that nobody maintains.
    if _looks_like_django(sandbox):
        pip(["pytest-django"], "runner:pytest-django")
        for dotted in _django_settings_candidates(repo_path):
            sandbox.env["DJANGO_SETTINGS_MODULE"] = dotted
            sandbox.install_log.append(f"django:DJANGO_SETTINGS_MODULE={dotted}")
            if _collects(sandbox):
                return sandbox
        sandbox.env.pop("DJANGO_SETTINGS_MODULE", None)

    # Collection still fails. Not an error: the suite may be genuinely broken,
    # which is a real finding. The caller sees the exit code and the install log
    # and decides — this function does not get to make that call.
    sandbox.install_log.append("collection still failing after every strategy")
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
            [
                sandbox.python,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--color=no",
                # Run the tests that *did* import rather than refusing the whole
                # suite over a module that wants an optional dependency. The
                # modules that failed are counted and compared across the
                # upgrade, so nothing is quietly excused: one that imported
                # before and does not now is the break we came for.
                "--continue-on-collection-errors",
            ],
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
    combined = (result.stdout or "") + (result.stderr or "")
    collected, collection_errors = collection_counts(combined)
    return TestReport(
        passed=result.returncode == 0,
        output=_tail(combined),
        duration_seconds=duration,
        collected=result.returncode != 5,
        exit_code=result.returncode,
        tests_collected=collected,
        collection_errors=collection_errors,
        failures=failing_ids(combined),
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


# --------------------------------------------------------------------------- #
# Verifying that the upgrade survived the repair
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class UpgradeDrift:
    """An upgraded package that is no longer at the version we upgraded it to."""

    package: str
    expected: str
    installed: str | None

    def __str__(self) -> str:
        found = self.installed or "not installed"
        return f"{self.package}: expected {self.expected}, found {found}"


def installed_versions(sandbox: Sandbox, packages: Sequence[str]) -> dict[str, str | None]:
    """What is actually importable in the sandbox right now.

    Read from the environment rather than from the manifest, because the
    manifest is what we wrote and the environment is what the tests ran against.
    Those two can disagree, and when they do it is the environment that decides
    whether a green suite means anything.
    """
    if not packages:
        return {}
    script = (
        "import json\n"
        "from importlib.metadata import PackageNotFoundError, version\n"
        f"names = {list(packages)!r}\n"
        "out = {}\n"
        "for name in names:\n"
        "    try:\n"
        "        out[name] = version(name)\n"
        "    except PackageNotFoundError:\n"
        "        out[name] = None\n"
        "print(json.dumps(out))\n"
    )
    result = sandbox.run([sandbox.python, "-c", script], timeout=120)
    if result.returncode != 0:
        log.warning("could not read installed versions: %s", _tail(result.stderr, 500))
        return dict.fromkeys(packages)
    try:
        parsed: dict[str, str | None] = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return dict.fromkeys(packages)
    return parsed


def upgrade_drift(
    sandbox: Sandbox, vulnerabilities: Sequence[Vulnerability]
) -> list[UpgradeDrift]:
    """Packages that are no longer at the fixed version. Empty means intact.

    This is the check that makes a green suite mean something. Every tool the
    repair agent has is gated by the policy engine, but the engine reasons about
    *actions*, and there are more ways to change an environment than any
    allowlist will ever enumerate — ``pip install`` at an older version being the
    obvious one. So rather than trying to forbid each of them, the outcome is
    verified directly: if the library we came to upgrade is not the version we
    upgraded it to, the suite passing proves nothing at all.

    Without this, an agent that runs ``pip install jinja2==2.11.3`` produces a
    green suite, a manifest that claims 3.1.2, a PATCHED_REPAIRED outcome and an
    open pull request — with the advisory still unfixed. The instruction tells it
    not to. This project's whole argument is that an instruction is not a
    guarantee.
    """
    expected = {v.package: v.fixed_version for v in vulnerabilities if v.fixed_version}
    if not expected:
        return []
    found = installed_versions(sandbox, sorted(expected))
    drift: list[UpgradeDrift] = []
    for package, wanted in sorted(expected.items()):
        actual = found.get(package)
        if actual is None or _version_or_none(actual) != _version_or_none(wanted):
            drift.append(UpgradeDrift(package=package, expected=wanted, installed=actual))
    return drift


def _version_or_none(raw: str) -> Version | str:
    """Compare as versions where possible, as text otherwise.

    ``3.1.2`` and ``3.1.2.post0`` are different releases and must not be treated
    as equal, but ``1.0`` and ``1.0.0`` are the same one and must not be treated
    as different.
    """
    try:
        return Version(raw)
    except InvalidVersion:
        return raw
