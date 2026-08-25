"""Nightly scanner: read manifests, ask OSV once, triage, publish, exit.

Cloud Scheduler wakes this as a Cloud Run Job. It is deliberately short-lived
and stateless — it fans work out and dies, so a slow repository cannot hold the
scan open and a crash costs one night's scan rather than one night's work.

Shape of a run:

    load fleet          which repositories are ours to touch    [implemented]
    read manifests      pinned dependencies, per repository      [implemented]
    query OSV           one batched request for the whole fleet  [implemented]
    triage              severity floor now; the Gemma pass in Block 3  [partial]
    publish             one Pub/Sub message per affected repo    [implemented]

Everything marked ``[stub]`` raises ``NotImplementedError`` on purpose. A stub
that returns an empty list would make a broken scan look like a quiet night,
which is the failure mode this project is least willing to have.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from nightshift_core.config import Settings, get_settings
from nightshift_core.fleet import load_pool
from nightshift_core.github import GitHubClient
from nightshift_core.manifests import RECOGNISED_MANIFESTS, parse_manifest
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

    A missing pool raises rather than returning nothing, because a scan of zero
    repositories and a quiet night look identical in the morning.
    """
    return load_pool(settings.fleet_pool_path).repos


def read_manifests(repo: str, client: GitHubClient | None = None) -> Sequence[Dependency]:
    """Pinned dependencies for one repository.

    Read through the GitHub contents API rather than by cloning: the scan
    touches hundreds of repositories and wants two small files from each, while
    only the worker needs a working tree. PyPI only, per ADR 0001, behind the
    manifest adapter so a second ecosystem is an addition rather than a rewrite.

    A dependency that appears in several manifests is returned once. The same
    pin listed in ``requirements.txt`` and ``requirements/dev.txt`` is one
    dependency, and counting it twice would double it in the OSV batch.
    """
    owned = client is None
    client = client or GitHubClient(get_settings().github_token or "")
    try:
        found: list[Dependency] = []
        seen: set[tuple[str, str]] = set()
        for name in RECOGNISED_MANIFESTS:
            text = client.get_file(repo, name)
            if not text:
                continue
            for dependency in parse_manifest(text, name):
                key = (dependency.name, dependency.version)
                if key not in seen:
                    seen.add(key)
                    found.append(dependency)
        return found
    finally:
        if owned:
            client.close()


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


@lru_cache(maxsize=1)
def _publisher() -> Any:
    """One publisher for the process, built on first use.

    Imported inside the function rather than at module load so the scanner stays
    importable on a machine with no cloud libraries — the same rule the job
    store follows, and one that CI asserts on directly.

    Cached because a scan fans out to every affected repository in the fleet,
    and a client per message would open a connection per message.
    """
    from google.cloud import pubsub_v1

    return pubsub_v1.PublisherClient()


def publish(job: RepoJob, settings: Settings, *, timeout: float = 30.0) -> str:
    """Publish one job to Pub/Sub. Returns the message id.

    One message per repository, not per advisory: the worker builds the
    environment once and applies every upgrade that repository needs in a single
    pass, because the environment build is the expensive part.

    **This blocks on the result.** Pub/Sub's publish is asynchronous and hands
    back a future; a scan that fired three hundred of them and exited would
    report three hundred jobs published having published an unknown number,
    because the process can die with messages still sitting in the client's
    buffer. Waiting costs milliseconds per repository and makes the count in the
    log a fact rather than an intention — which matters here more than usual,
    since a scan that publishes nothing and a quiet night look identical in the
    morning.

    The repository and job id ride along as attributes. The body carries them
    too, but an attribute can be read by a subscription filter and shown in the
    console without parsing JSON, which is the difference between debugging a
    night's run and reading it.
    """
    publisher = _publisher()
    topic = publisher.topic_path(settings.gcp_project, settings.jobs_topic)
    future = publisher.publish(
        topic,
        json.dumps(job.to_dict()).encode("utf-8"),
        repo=job.repo,
        job_id=job.job_id,
    )
    return str(future.result(timeout=timeout))


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
