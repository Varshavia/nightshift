"""Nightly scanner: read manifests, ask OSV once, triage, publish, exit.

Cloud Scheduler wakes this as a Cloud Run Job. It is deliberately short-lived
and stateless — it fans work out and dies, so a slow repository cannot hold the
scan open and a crash costs one night's scan rather than one night's work.

Shape of a run:

    load fleet          which repositories are ours to touch          [stub]
    read manifests      pinned dependencies, per repository            [stub]
    query OSV           one batched request for the whole fleet  [implemented]
    triage              severity floor now; the Gemma pass in Block 3  [partial]
    publish             one Pub/Sub message per affected repo          [stub]

Everything marked ``[stub]`` raises ``NotImplementedError`` on purpose. A stub
that returns an empty list would make a broken scan look like a quiet night,
which is the failure mode this project is least willing to have.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from nightshift_core.config import Settings, get_settings
from nightshift_core.models import Dependency, RepoJob, Severity, Vulnerability
from nightshift_core.osv import OSVClient

log = logging.getLogger("nightshift.scanner")

#: Below this, an advisory is logged and left alone. Waking a worker, building an
#: environment and spending Gemini tokens on a LOW advisory in a transitive test
#: dependency costs more than the fix is worth.
TRIAGE_FLOOR = Severity.MODERATE


@dataclass(frozen=True, slots=True)
class ScanResult:
    run_id: str
    repos_scanned: int
    dependencies_seen: int
    jobs_published: int


def load_fleet(settings: Settings) -> Sequence[str]:
    """The repositories this fleet is allowed to touch, as ``owner/name``.

    Read from the fork pool built by ``scripts/build_fork_pool.py``, never from
    a wildcard search: the set of repositories we operate on is an explicit,
    reviewable list. See RESPONSIBLE_USE.md.
    """
    raise NotImplementedError("scanner: load_fleet")


def read_manifests(repo: str) -> Sequence[Dependency]:
    """Pinned dependencies for one repository.

    Reads the manifest through the GitHub contents API rather than cloning —
    the scan touches hundreds of repositories and only the worker needs a
    working tree. PyPI only, per ADR 0001, behind an adapter so a second
    ecosystem is an addition rather than a rewrite.
    """
    raise NotImplementedError("scanner: read_manifests")


def triage(vulnerabilities: Sequence[Vulnerability]) -> Sequence[Vulnerability]:
    """Cheap pass over raw advisories before any expensive work is scheduled.

    Runs on Gemma, not Gemini: the judgement needed here — is this advisory
    real, does it plausibly reach this codebase, is the summary describing
    something exploitable — is small, and doing it on the expensive model for
    every advisory in a 300-repository fleet is how a $150 credit disappears
    before the first repair.

    Block 1 implements the deterministic half only: the severity floor, and the
    "is there anything to upgrade to" check. The Gemma pass that judges whether
    an advisory plausibly reaches a given codebase is Block 3 — it needs Vertex,
    and gating local development on a credential would be the wrong trade.

    Order is preserved. The scanner pairs these back to dependencies by
    ``(package, version)`` and a reordering here would not break that, but it
    would make a scan's logs harder to read against its input.
    """
    return [
        vulnerability
        for vulnerability in vulnerabilities
        if vulnerability.actionable and vulnerability.severity.rank >= TRIAGE_FLOOR.rank
    ]


def publish(job: RepoJob, settings: Settings) -> str:
    """Publish one job to Pub/Sub. Returns the message id.

    One message per repository, not per advisory: the worker builds the
    environment once and applies every upgrade that repository needs in a single
    pass, because the environment build is the expensive part.
    """
    raise NotImplementedError("scanner: publish")


def scan(settings: Settings | None = None) -> ScanResult:
    """One nightly scan. The only function Cloud Run Jobs calls."""
    settings = settings or get_settings()
    settings.require_cloud()
    run_id = uuid.uuid4().hex[:12]

    repos = load_fleet(settings)
    dependencies: dict[str, Sequence[Dependency]] = {
        repo: read_manifests(repo) for repo in repos
    }

    flat = [dep for deps in dependencies.values() for dep in deps]
    with OSVClient() as osv:
        found = osv.find_vulnerabilities(flat)

    by_package = {(v.package, v.installed_version): v for v in triage(found)}

    published = 0
    for repo, deps in dependencies.items():
        hits = [by_package[(d.name, d.version)] for d in deps if (d.name, d.version) in by_package]
        if not hits:
            continue
        job = RepoJob(job_id=f"{run_id}:{repo}", repo=repo, vulnerabilities=list(hits))
        publish(job, settings)
        published += 1

    log.info("run %s scanned %d repos, published %d jobs", run_id, len(repos), published)
    return ScanResult(
        run_id=run_id,
        repos_scanned=len(repos),
        dependencies_seen=len(flat),
        jobs_published=published,
    )


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    print(scan())
