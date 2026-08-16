"""Per-repository worker: baseline → upgrade → verify → repair → PR.

One Pub/Sub message in, one :class:`RepoJob` with a terminal ``Outcome`` out.
The order matters and is not negotiable:

**Baseline first, tests untouched.** Before anything is changed, the suite is
run as it arrived. A repository whose suite is already red is ``BASELINE_RED``
and the worker stops there. Without this step every later number is worthless,
because we would be taking credit for repairing breakage we did not cause and
blame for breakage that was already present.

**Then upgrade, then verify.** If the suite is still green the job is
``PATCHED_CLEAN`` and no model has been called at all — a large fraction of
nights end here, and they are cheap.

**Only then repair.** The loop is bounded by the ceilings in
:class:`nightshift_core.config.Ceilings`, checked by the policy engine before
every tool call. Exhausting them is ``REPAIR_EXHAUSTED``, a real result.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nightshift_core.config import Settings, get_settings
from nightshift_core.models import Outcome, Phase, RepoJob
from nightshift_core.policy import Budget, PolicyEngine
from nightshift_core.store import JobStore

log = logging.getLogger("nightshift.worker")


class EnvironmentBuildError(RuntimeError):
    """The repository's dependencies could not be installed.

    Its own exception type rather than a bare ``RuntimeError`` because this is
    the most common way a job ends at fleet scale, and it must map to
    ``UNBUILDABLE`` — a counted result — rather than disappear into a generic
    error path.
    """


class TestRunResult:
    """Outcome of one test invocation: exit status plus the output to reason on."""

    def __init__(self, passed: bool, output: str, duration_seconds: float) -> None:
        self.passed = passed
        self.output = output
        self.duration_seconds = duration_seconds


def clone(job: RepoJob, workspace: Path) -> Path:
    """Shallow-clone the fork into the sandbox. Returns the working tree."""
    raise NotImplementedError("worker: clone")


def build_environment(repo_path: Path) -> None:
    """Install the repository's dependencies.

    Expected to fail often — this is the hardest part of the whole system, not
    the agent. Failure here raises :class:`EnvironmentBuildError` and becomes
    ``UNBUILDABLE``: a first-class outcome, counted and displayed, never
    swallowed as an error.
    """
    raise NotImplementedError("worker: build_environment")


def run_tests(repo_path: Path) -> TestRunResult:
    """Run the repository's own suite, exactly as it defines it."""
    raise NotImplementedError("worker: run_tests")


def apply_upgrade(job: RepoJob, repo_path: Path) -> None:
    """Rewrite the manifest to the fixed versions and reinstall."""
    raise NotImplementedError("worker: apply_upgrade")


def repair(job: RepoJob, repo_path: Path, failure: TestRunResult, policy: PolicyEngine) -> bool:
    """Run the bounded repair loop. True when the suite ends green.

    Each turn: give the agent the failing output and the diff so far, let it
    make one conceptual fix, re-run the suite, record a
    :class:`~nightshift_core.models.RepairAttempt` whether or not it worked.
    """
    raise NotImplementedError("worker: repair")


def open_pull_request(job: RepoJob, repo_path: Path, policy: PolicyEngine) -> str:
    """Open the PR from ``templates/pr_body.md``. Returns its url.

    The body carries the advisory, the version transition, the repair diff, the
    agent's explanation, and the AI-authorship disclosure. Nothing merges itself.
    """
    raise NotImplementedError("worker: open_pull_request")


def handle(job: RepoJob, store: JobStore, settings: Settings | None = None) -> RepoJob:
    """Process one job to a terminal outcome. Checkpointed at every phase."""
    settings = settings or get_settings()
    policy = PolicyEngine(settings=settings)
    budget = Budget()
    workspace = Path("/workspace") / job.job_id.replace(":", "_")

    def checkpoint(phase: Phase) -> None:
        job.advance(phase)
        store.put(job)

    checkpoint(Phase.CLONING)
    repo_path = clone(job, workspace)

    checkpoint(Phase.BASELINE)
    try:
        build_environment(repo_path)
    except EnvironmentBuildError:
        job.finish(Outcome.UNBUILDABLE, notes="dependency installation failed")
        store.put(job)
        return job

    baseline = run_tests(repo_path)
    job.baseline_green = baseline.passed
    if not baseline.passed:
        job.finish(Outcome.BASELINE_RED, notes="suite was already failing before the upgrade")
        store.put(job)
        return job

    checkpoint(Phase.UPGRADE)
    if not job.actionable_vulnerabilities:
        job.finish(Outcome.NO_FIX_AVAILABLE, notes="no published fix for any advisory")
        store.put(job)
        return job
    apply_upgrade(job, repo_path)

    checkpoint(Phase.VERIFY)
    verified = run_tests(repo_path)

    repaired = False
    if not verified.passed:
        checkpoint(Phase.REPAIR)
        repaired = repair(job, repo_path, verified, policy)
        if not repaired:
            job.finish(Outcome.REPAIR_EXHAUSTED, notes="ceiling reached with the suite still red")
            store.put(job)
            return job

    checkpoint(Phase.OPENING_PR)
    pr_url = open_pull_request(job, repo_path, policy)
    job.finish(
        Outcome.PATCHED_REPAIRED if repaired else Outcome.PATCHED_CLEAN,
        pr_url=pr_url,
    )
    store.put(job)
    log.info("job %s finished as %s (%d tokens)", job.job_id, job.outcome, budget.tokens)
    return job


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("the worker is driven by Pub/Sub; use `make run-local REPO=owner/name`")
