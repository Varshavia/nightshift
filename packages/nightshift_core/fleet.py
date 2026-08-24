"""The fork pool: which repositories the fleet is allowed to touch, and why.

Not a wildcard search evaluated at run time. The fleet operates on an explicit,
reviewable list, and this module is that list's format and its selection rule.
See ADR 0002 and RESPONSIBLE_USE.md — a system that can open three hundred pull
requests overnight should not also be choosing its own targets.

Every entry carries the evidence for its own selection: the licence, how many
dependencies are actually pinned, which manifests they came from. That is what
makes the pool reviewable rather than merely reviewed — a human scanning the
file can see *why* each repository is in it and disagree with a specific reason.

**The selection rule the first real probe taught us.** Libraries do not pin. They
declare ranges, correctly: `requests>=2.0`, `click~=8.0`. A range has no single
installed version to ask OSV about, so a fleet of libraries has nothing to scan.
``itsdangerous`` and ``tenacity`` both came back NOT_AFFECTED for exactly this
reason. Applications pin — services, CLI tools, deployed Django and Flask
projects — and applications are therefore what the pool must contain. Getting
this wrong means forking three hundred repositories and discovering that half of
them are unscannable.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from packaging.utils import canonicalize_name

__all__ = [
    "BUILD_TOOLING",
    "MAX_REPO_SIZE_KB",
    "MIN_PINNED_DEPENDENCIES",
    "PERMISSIVE_LICENCES",
    "POOL_SCHEMA",
    "Candidate",
    "FleetEntry",
    "FleetPool",
    "eligibility",
    "load_pool",
    "save_pool",
]

#: Bumped when the file format changes, so a pool built weeks ago still reads.
POOL_SCHEMA = 1

#: We copy and modify this code, so the licence has to allow it. Copyleft is not
#: excluded because it is bad — it is excluded because complying with it
#: properly is a conversation, and a script should not be having conversations.
PERMISSIVE_LICENCES: frozenset[str] = frozenset(
    {"mit", "bsd-2-clause", "bsd-3-clause", "apache-2.0", "isc", "0bsd", "unlicense"}
)

#: Distributions a project builds or tests *with* rather than calls. An
#: advisory against one of these is worth fixing and is not evidence that an
#: upgrade would break anything, because nothing imports them at runtime.
#:
#: Short and specific on purpose. Anything genuinely ambiguous — `requests`,
#: `jinja2`, `click` — stays out: a CLI really does break when click changes.
BUILD_TOOLING: frozenset[str] = frozenset(
    {
        "black",
        "build",
        "coverage",
        "filelock",
        "flake8",
        "isort",
        "mypy",
        "pip",
        "pip-tools",
        "pytest",
        "ruff",
        "setuptools",
        "tox",
        "twine",
        "virtualenv",
        "wheel",
    }
)

#: Below this, a repository is not really pinning — it has one or two incidental
#: `==` lines and is a library wearing an application's clothes. Three is a
#: judgement call; it is here as a constant so a fleet run can argue with it.
MIN_PINNED_DEPENDENCIES = 3

#: Repositories larger than this do not fit inside a job. Not an aesthetic
#: preference: ``toolchain.INSTALL_TIMEOUT`` and ``TEST_TIMEOUT`` are fifteen
#: minutes each, and projects of home-assistant's or superset's size exceed both
#: before they have installed. Forking them would fill the pool with jobs that
#: can only ever end UNBUILDABLE — an honest outcome, but a wasted night and a
#: denominator full of repositories we never had a chance with.
MAX_REPO_SIZE_KB = 50_000


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A repository being considered, with everything selection depends on."""

    repo: str
    stars: int = 0
    license_id: str = ""
    archived: bool = False
    fork: bool = False
    size_kb: int = 0
    has_tests: bool = False
    pinned_dependencies: int = 0
    manifests: tuple[str, ...] = ()

    #: Whether OSV was actually asked about this repository's pins. Kept apart
    #: from the count because "nobody looked" and "we looked and there is
    #: nothing" must not be the same value: only the second is grounds for
    #: rejecting a repository.
    advisories_checked: bool = False
    #: Advisories against the current pins that have a published fix.
    actionable_advisories: int = 0
    #: Which distributions they are against, so a reviewer can see at a glance
    #: whether the finding is a real dependency or a linter in a dev extra.
    advisory_packages: tuple[str, ...] = ()

    @property
    def call_path_advisories(self) -> int:
        """Advisories against something the repository actually calls.

        The count alone misleads. One survey put `apiflask` forward with three
        advisories, and all three were against ``filelock``, ``virtualenv`` and
        ``wheel`` — build tooling, which the code never imports and upgrading
        cannot break. Meanwhile ``flask-jwt-extended`` showed four, of which
        ``pyjwt`` and ``cryptography`` are the library's entire subject matter.

        Nightshift is about upgrades that break calling code, so this is the
        number selection should rank on.
        """
        return sum(
            1
            for package in self.advisory_packages
            if str(canonicalize_name(package)) not in BUILD_TOOLING
        )

    @property
    def normalised_licence(self) -> str:
        return self.license_id.strip().lower()


@dataclass(frozen=True, slots=True)
class FleetEntry:
    """One repository in the pool, and the evidence for it being there."""

    repo: str
    upstream: str = ""
    license_id: str = ""
    stars: int = 0
    pinned_dependencies: int = 0
    manifests: tuple[str, ...] = ()
    has_tests: bool = True
    size_kb: int = 0
    added_at: datetime = field(default_factory=_now)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "upstream": self.upstream,
            "license_id": self.license_id,
            "stars": self.stars,
            "pinned_dependencies": self.pinned_dependencies,
            "manifests": list(self.manifests),
            "has_tests": self.has_tests,
            "size_kb": self.size_kb,
            "added_at": self.added_at.isoformat(),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            repo=data["repo"],
            upstream=data.get("upstream", ""),
            license_id=data.get("license_id", ""),
            stars=int(data.get("stars", 0)),
            pinned_dependencies=int(data.get("pinned_dependencies", 0)),
            manifests=tuple(data.get("manifests", ())),
            has_tests=bool(data.get("has_tests", True)),
            size_kb=int(data.get("size_kb", 0)),
            added_at=datetime.fromisoformat(data["added_at"])
            if data.get("added_at")
            else _now(),
            notes=data.get("notes", ""),
        )

    @classmethod
    def from_candidate(cls, candidate: Candidate, *, repo: str, upstream: str) -> Self:
        return cls(
            repo=repo,
            upstream=upstream,
            license_id=candidate.license_id,
            stars=candidate.stars,
            pinned_dependencies=candidate.pinned_dependencies,
            manifests=candidate.manifests,
            has_tests=candidate.has_tests,
            size_kb=candidate.size_kb,
        )


@dataclass(frozen=True, slots=True)
class FleetPool:
    """The whole list. What the scanner loads and a human reviews."""

    entries: tuple[FleetEntry, ...] = ()
    schema: int = POOL_SCHEMA
    generated_at: datetime = field(default_factory=_now)

    @property
    def repos(self) -> list[str]:
        return [entry.repo for entry in self.entries]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generated_at": self.generated_at.isoformat(),
            "count": len(self.entries),
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        schema = int(data.get("schema", 0))
        if schema != POOL_SCHEMA:
            raise ValueError(
                f"fork pool schema {schema} is not {POOL_SCHEMA}; rebuild it rather than "
                "guessing what the old fields meant"
            )
        return cls(
            entries=tuple(FleetEntry.from_dict(e) for e in data.get("entries", [])),
            schema=schema,
            generated_at=datetime.fromisoformat(data["generated_at"])
            if data.get("generated_at")
            else _now(),
        )

    def merged_with(self, entries: Iterable[FleetEntry]) -> FleetPool:
        """Add entries, keeping the existing one where a repository repeats.

        The one already in the pool has been reviewed and may have been edited
        by hand; a fresh proposal has neither of those things going for it.
        """
        known = {entry.repo for entry in self.entries}
        additions = tuple(e for e in entries if e.repo not in known)
        return FleetPool(entries=self.entries + additions, generated_at=self.generated_at)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def eligibility(candidate: Candidate) -> tuple[bool, str]:
    """Whether to propose this repository, and the reason either way.

    The reason is returned rather than logged because rejections are as much a
    part of a reviewable pool as acceptances: a human asking "why is this
    project not in here" should get an answer without rerunning anything.

    Checked in order of cost to the reader, not cost to compute — a licence
    problem is a hard no and should be the first thing said about a repository.
    """
    if candidate.normalised_licence not in PERMISSIVE_LICENCES:
        return False, f"licence {candidate.license_id or 'unknown'} does not permit this"
    if candidate.archived:
        return False, "archived; a pull request would go nowhere"
    if candidate.fork:
        return False, "already a fork; the upstream is the interesting one"
    if not candidate.has_tests:
        return False, "no test suite, so nothing can serve as evidence of a repair"
    if candidate.size_kb > MAX_REPO_SIZE_KB:
        return False, (
            f"{candidate.size_kb // 1000} MB; larger than a job's install and test "
            "ceilings allow, so it could only ever end UNBUILDABLE"
        )
    if candidate.pinned_dependencies < MIN_PINNED_DEPENDENCIES:
        return False, (
            f"{candidate.pinned_dependencies} exact pins; libraries declare ranges and "
            "a range has no version to ask OSV about"
        )
    # Last, because it costs a network call the earlier checks do not, and there
    # is no sense asking OSV about a repository we have already refused.
    #
    # This closes the loop the first six probes exposed. Every earlier check asks
    # "could we work on this repository", and two repositories passed all of them
    # and came back NOT_AFFECTED — nothing to fix. That is the query arguing with
    # itself: `pushed:>...` selects maintained projects, maintained projects keep
    # their dependencies current, and current dependencies have no advisories.
    # No GitHub search qualifier expresses "green suite but stale pins", so the
    # question gets asked directly instead.
    if candidate.advisories_checked and candidate.actionable_advisories == 0:
        return False, (
            "no advisory with a published fix against its current pins; there would "
            "be nothing for the fleet to upgrade"
        )
    if candidate.advisories_checked:
        return True, (
            f"{candidate.actionable_advisories} fixable advisories against "
            + ", ".join(candidate.advisory_packages[:4])
            + f"; {candidate.pinned_dependencies} pins"
        )
    return True, f"{candidate.pinned_dependencies} pins across {len(candidate.manifests)} manifests"


def propose(candidates: Sequence[Candidate]) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """Split candidates into accepted and ``(repo, reason)`` rejections."""
    accepted: list[Candidate] = []
    rejected: list[tuple[str, str]] = []
    # Deduplicated here as well as at the search, because a proposal containing
    # the same repository twice would be forked twice and counted twice, and the
    # second copy would quietly inflate every number computed over the pool.
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.repo in seen:
            continue
        seen.add(candidate.repo)
        ok, reason = eligibility(candidate)
        if ok:
            accepted.append(candidate)
        else:
            rejected.append((candidate.repo, reason))
    return accepted, rejected


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def load_pool(path: str | Path) -> FleetPool:
    """Read the pool. A missing file is an error, never an empty fleet.

    A scanner that read a missing pool as "no repositories" would report a quiet
    night, which is the failure mode this project is least willing to have.
    """
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(
            f"no fork pool at {target}; build one with scripts/build_fork_pool.py"
        )
    return FleetPool.from_dict(json.loads(target.read_text(encoding="utf-8")))


def save_pool(pool: FleetPool, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(pool.to_dict(), indent=2) + "\n", encoding="utf-8")
