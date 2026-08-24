"""The domain vocabulary of a night's work.

The important type here is :class:`Outcome`. It is a *closed* enum: every job
ends in exactly one of these states and none of them is an exception. A
repository whose test suite was already failing before we touched it is
``BASELINE_RED``, not a crash; a repository whose environment cannot be built is
``UNBUILDABLE``, not a crash. That is what turns "repair rate" from a claim into
a number — the denominator is honest because the failures are named.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

__all__ = [
    "SUCCESS_OUTCOMES",
    "TERMINAL_OUTCOMES",
    "Dependency",
    "Outcome",
    "Phase",
    "RepairAttempt",
    "RepoJob",
    "Severity",
    "Vulnerability",
    "consolidate_upgrades",
]

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _now() -> datetime:
    return datetime.now(UTC)


class Severity(StrEnum):
    """OSV severity buckets, coarsened to what the triage pass acts on."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        order = {
            Severity.CRITICAL: 4,
            Severity.HIGH: 3,
            Severity.MODERATE: 2,
            Severity.LOW: 1,
            Severity.UNKNOWN: 0,
        }
        return order[self]


class Phase(StrEnum):
    """Where a job is. Checkpointed to Firestore on every transition.

    A worker that dies mid-flight is resumed from its last phase rather than
    restarted from the beginning, which matters when a night is 300 repositories
    long and the cheapest phase (``BASELINE``) is also the slowest.
    """

    QUEUED = "QUEUED"
    CLONING = "CLONING"
    BASELINE = "BASELINE"
    UPGRADE = "UPGRADE"
    VERIFY = "VERIFY"
    REPAIR = "REPAIR"
    OPENING_PR = "OPENING_PR"
    DONE = "DONE"


class Outcome(StrEnum):
    """How a job ended. Closed set — do not add a member without an ADR.

    ``PATCHED_REPAIRED`` is the one the project exists to produce: the upgrade
    broke the calling code and the agent rewrote it until the suite went green
    again. Everything else is either easier than that or an honest miss.
    """

    #: Upgrade applied, suite green, no code change needed beyond the version.
    PATCHED_CLEAN = "PATCHED_CLEAN"
    #: Upgrade broke the suite; the agent repaired the calling code. The product.
    PATCHED_REPAIRED = "PATCHED_REPAIRED"
    #: The agent hit a ceiling with the suite still red. Counted, not hidden.
    REPAIR_EXHAUSTED = "REPAIR_EXHAUSTED"
    #: The environment could not be built at all. Expected at fleet scale.
    UNBUILDABLE = "UNBUILDABLE"
    #: The suite was already failing before we changed anything. Not our doing.
    BASELINE_RED = "BASELINE_RED"
    #: The advisory has no fixed version yet. Nothing to upgrade to.
    NO_FIX_AVAILABLE = "NO_FIX_AVAILABLE"
    #: The policy engine refused a step the job could not proceed without.
    POLICY_BLOCKED = "POLICY_BLOCKED"
    #: Infrastructure fault on our side — the only member that is a real bug.
    INFRA_ERROR = "INFRA_ERROR"


#: Outcomes that mean a pull request exists and a human has something to review.
SUCCESS_OUTCOMES: frozenset[Outcome] = frozenset(
    {Outcome.PATCHED_CLEAN, Outcome.PATCHED_REPAIRED}
)

#: Every outcome is terminal. Spelled out so the scheduler can assert on it.
TERMINAL_OUTCOMES: frozenset[Outcome] = frozenset(Outcome)


@dataclass(frozen=True, slots=True)
class Vulnerability:
    """One OSV advisory as it applies to one pinned package version."""

    osv_id: str
    package: str
    installed_version: str
    fixed_version: str | None = None
    severity: Severity = Severity.UNKNOWN
    summary: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        """False when there is nothing to upgrade to — an honest dead end."""
        return bool(self.fixed_version)

    @property
    def cve(self) -> str | None:
        return next((alias for alias in self.aliases if alias.startswith("CVE-")), None)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = str(self.severity)
        data["aliases"] = list(self.aliases)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            osv_id=data["osv_id"],
            package=data["package"],
            installed_version=data["installed_version"],
            fixed_version=data.get("fixed_version"),
            severity=Severity(data.get("severity", "UNKNOWN")),
            summary=data.get("summary", ""),
            aliases=tuple(data.get("aliases", ())),
        )


def consolidate_upgrades(vulnerabilities: Iterable[Vulnerability]) -> list[Vulnerability]:
    """One upgrade per package: the highest fixed version, advisories merged.

    OSV answers per advisory, and a package that has been patched several times
    produces several advisories against the same pinned version. Treating each
    one as its own upgrade asks pip to install a package at three versions at
    once, which it correctly refuses::

        black 23.12.0 -> 24.3.0
        black 23.12.0 -> 26.3.0
        black 23.12.0 -> 26.3.1
        ERROR: ResolutionImpossible

    That is not a hypothetical. It is what a real repository in the pool did,
    and it was recorded as UPGRADE_FAILED — a verdict that reads as "this
    repository resisted being fixed" when what actually happened is that we
    asked for something impossible.

    The highest version is taken because security fixes accumulate: the release
    that fixes the newest advisory contains the earlier fixes too. Where that
    assumption fails, it fails safe — the suite is run afterwards either way,
    and an upgrade that does not resolve an advisory shows up as a repository
    still reported vulnerable rather than as a silent pass.

    Every merged advisory's identifier is kept in ``aliases``, so nothing is
    lost from the record: the PR still cites all four.
    """
    best: dict[str, Vulnerability] = {}
    merged: dict[str, list[str]] = {}

    for vulnerability in vulnerabilities:
        if not vulnerability.actionable:
            continue
        key = str(canonicalize_name(vulnerability.package))
        merged.setdefault(key, []).append(vulnerability.osv_id)
        incumbent = best.get(key)
        if incumbent is None or _is_higher(vulnerability.fixed_version, incumbent.fixed_version):
            best[key] = vulnerability

    consolidated: list[Vulnerability] = []
    for key, winner in sorted(best.items()):
        others = tuple(sorted(set(merged[key]) - {winner.osv_id}))
        consolidated.append(
            replace(winner, aliases=tuple(dict.fromkeys(winner.aliases + others)))
            if others
            else winner
        )
    return consolidated


def _is_higher(candidate: str | None, incumbent: str | None) -> bool:
    """Version comparison that never raises on a version it cannot parse.

    Some advisories carry fixed versions that are not PEP 440 — dates, git
    describes, vendor strings. Falling back to a string comparison is arbitrary
    but total, and an arbitrary choice between two unparseable versions is a
    much smaller problem than an exception thrown in the middle of a fleet run.
    """
    if candidate is None:
        return False
    if incumbent is None:
        return True
    try:
        return Version(candidate) > Version(incumbent)
    except InvalidVersion:
        return candidate > incumbent


@dataclass(frozen=True, slots=True)
class Dependency:
    """A pinned requirement read out of a manifest.

    ``ecosystem`` is carried even though ADR 0001 restricts the fleet to PyPI:
    the field is what lets a second ecosystem arrive behind the adapter
    interface without a migration of everything already in Firestore.
    """

    name: str
    version: str
    ecosystem: str = "PyPI"
    manifest_path: str = "requirements.txt"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("dependency name must not be empty")
        if not self.version:
            raise ValueError(f"dependency {self.name} must be pinned to a version")


@dataclass(frozen=True, slots=True)
class RepairAttempt:
    """One turn of the repair loop, recorded whether or not it worked.

    The failing output is kept because it is the input to the next attempt and,
    afterwards, the material the Memory Bank is keyed on: the same library moving
    through the same version transition tends to break the same way everywhere.
    """

    attempt: int
    failing_output: str
    diff: str = ""
    rationale: str = ""
    tests_passed: bool = False
    tokens_used: int = 0
    duration_seconds: float = 0.0
    started_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["started_at"] = self.started_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            attempt=int(data["attempt"]),
            failing_output=data.get("failing_output", ""),
            diff=data.get("diff", ""),
            rationale=data.get("rationale", ""),
            tests_passed=bool(data.get("tests_passed", False)),
            tokens_used=int(data.get("tokens_used", 0)),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            started_at=datetime.fromisoformat(data["started_at"])
            if data.get("started_at")
            else _now(),
        )


@dataclass
class RepoJob:
    """The aggregate: one repository, one night, one outcome.

    This is the unit the queue carries, the worker checkpoints, the dashboard
    renders and the final numbers are computed from. It is mutable because a
    worker advances it in place; it is serialisable because a worker may die and
    another must pick it up where it stopped.
    """

    job_id: str
    repo: str
    vulnerabilities: list[Vulnerability] = field(default_factory=list)
    phase: Phase = Phase.QUEUED
    outcome: Outcome | None = None
    repair_attempts: list[RepairAttempt] = field(default_factory=list)
    pr_url: str | None = None
    baseline_green: bool | None = None
    #: Which Ledger tier answered: "exact", "near" or "miss". The independent
    #: variable of the cost curve, so it is stored on the job as well as being
    #: put on the trace — a span that expired must not take the number with it.
    ledger_hit: str = "miss"
    tokens_used: int = 0
    cost_usd: float = 0.0
    notes: str = ""
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not _REPO_RE.match(self.repo):
            raise ValueError(f"repo must look like 'owner/name', got {self.repo!r}")

    # -- derived views ------------------------------------------------------ #

    @property
    def finished(self) -> bool:
        return self.outcome is not None

    @property
    def required_repair(self) -> bool:
        return bool(self.repair_attempts)

    @property
    def highest_severity(self) -> Severity:
        return max(
            (v.severity for v in self.vulnerabilities),
            key=lambda s: s.rank,
            default=Severity.UNKNOWN,
        )

    @property
    def actionable_vulnerabilities(self) -> list[Vulnerability]:
        """One upgrade per package, not one per advisory.

        See :func:`consolidate_upgrades`; the difference is what stopped a real
        repository from being upgraded at all.
        """
        return consolidate_upgrades(self.vulnerabilities)

    # -- transitions -------------------------------------------------------- #

    def advance(self, phase: Phase) -> None:
        """Move to a new phase. Refuses to move a job that already ended."""
        if self.finished:
            raise ValueError(f"job {self.job_id} already finished as {self.outcome}")
        self.phase = phase
        self.updated_at = _now()

    def record_attempt(self, attempt: RepairAttempt) -> None:
        self.repair_attempts.append(attempt)
        self.tokens_used += attempt.tokens_used
        self.updated_at = _now()

    def finish(self, outcome: Outcome, *, pr_url: str | None = None, notes: str = "") -> None:
        """Close the job. Idempotent only for an identical repeat."""
        if self.finished and self.outcome != outcome:
            raise ValueError(
                f"job {self.job_id} already finished as {self.outcome}, refusing {outcome}"
            )
        if outcome in SUCCESS_OUTCOMES and not (pr_url or self.pr_url):
            raise ValueError(f"{outcome} requires a pull request url")
        self.outcome = outcome
        self.pr_url = pr_url or self.pr_url
        self.notes = notes or self.notes
        self.phase = Phase.DONE
        self.updated_at = _now()

    # -- serialisation ------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "repo": self.repo,
            "vulnerabilities": [v.to_dict() for v in self.vulnerabilities],
            "phase": str(self.phase),
            "outcome": str(self.outcome) if self.outcome else None,
            "repair_attempts": [a.to_dict() for a in self.repair_attempts],
            "pr_url": self.pr_url,
            "baseline_green": self.baseline_green,
            "ledger_hit": self.ledger_hit,
            "tokens_used": self.tokens_used,
            "cost_usd": self.cost_usd,
            "notes": self.notes,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        job = cls(
            job_id=data["job_id"],
            repo=data["repo"],
            vulnerabilities=[Vulnerability.from_dict(v) for v in data.get("vulnerabilities", [])],
            phase=Phase(data.get("phase", "QUEUED")),
            outcome=Outcome(data["outcome"]) if data.get("outcome") else None,
            repair_attempts=[RepairAttempt.from_dict(a) for a in data.get("repair_attempts", [])],
            pr_url=data.get("pr_url"),
            baseline_green=data.get("baseline_green"),
            ledger_hit=data.get("ledger_hit", "miss"),
            tokens_used=int(data.get("tokens_used", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
            notes=data.get("notes", ""),
        )
        if data.get("created_at"):
            job.created_at = datetime.fromisoformat(data["created_at"])
        if data.get("updated_at"):
            job.updated_at = datetime.fromisoformat(data["updated_at"])
        return job


def summarise(jobs: Iterable[RepoJob]) -> dict[str, int]:
    """Count jobs by outcome. The nightly number, and the honest one.

    Unfinished jobs are counted under ``"IN_FLIGHT"`` rather than dropped, so the
    totals in the dashboard always add up to the size of the fleet.
    """
    counts: dict[str, int] = {str(o): 0 for o in Outcome}
    counts["IN_FLIGHT"] = 0
    for job in jobs:
        counts[str(job.outcome) if job.outcome else "IN_FLIGHT"] += 1
    return counts
