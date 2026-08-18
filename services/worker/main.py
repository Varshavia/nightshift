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

Everything up to and including VERIFY is implemented and calls no model — see
``toolchain.py``. That is what lets ``scripts/probe_fleet.py`` measure the whole
fleet for free before a token is spent.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nightshift_core.config import Settings, get_settings
from nightshift_core.models import Outcome, Phase, RepoJob
from nightshift_core.policy import Budget, PolicyEngine
from nightshift_core.store import JobStore
from services.worker.agent import build_repair_agent
from services.worker.repair import RepairAgent, run_repair_loop
from services.worker.toolchain import (
    EnvironmentBuildError,
    Sandbox,
    TestReport,
    UpgradeError,
    apply_upgrade,
    build_environment,
    clone,
    run_tests,
)

log = logging.getLogger("nightshift.worker")


def repair(
    job: RepoJob,
    sandbox: Sandbox,
    failure: TestReport,
    policy: PolicyEngine,
    budget: Budget,
    agent: RepairAgent,
) -> bool:
    """Run the bounded repair loop. True when the suite ends green.

    Each turn: give the agent the failing output and the diff so far, let it
    make one conceptual fix, re-run the suite, record a
    :class:`~nightshift_core.models.RepairAttempt` whether or not it worked.

    The only place in the worker where a model is called, and the only place
    with a ceiling on attempts, wall-clock and tokens. The implementation lives
    in ``repair.py`` so that the loop can be tested with a scripted agent and no
    token spent; this stays as the worker's own vocabulary.
    """
    return run_repair_loop(job, sandbox, failure, policy, budget, agent)


def open_pull_request(job: RepoJob, sandbox: Sandbox, policy: PolicyEngine) -> str:
    """Open the PR from ``templates/pr_body.md``. Returns its url.

    The body carries the advisory, the version transition, the repair diff, the
    agent's explanation, and the AI-authorship disclosure. Nothing merges itself.
    """
    raise NotImplementedError("worker: open_pull_request")


def handle(job: RepoJob, store: JobStore, settings: Settings | None = None) -> RepoJob:
    """Process one job to a terminal outcome. Checkpointed at every phase."""
    settings = settings or get_settings()
    budget = Budget()
    workspace = Path(settings.workspace_root) / job.job_id.replace(":", "_").replace("/", "_")

    def checkpoint(phase: Phase) -> None:
        job.advance(phase)
        store.put(job)

    def finish(outcome: Outcome, *, pr_url: str | None = None, notes: str = "") -> RepoJob:
        job.finish(outcome, pr_url=pr_url, notes=notes)
        store.put(job)
        log.info("job %s finished as %s (%d tokens)", job.job_id, job.outcome, job.tokens_used)
        return job

    checkpoint(Phase.CLONING)
    try:
        repo_path = clone(job.repo, workspace, token=settings.github_token)
    except EnvironmentBuildError as exc:
        return finish(Outcome.INFRA_ERROR, notes=f"clone failed: {exc}"[:500])

    # Built here, not before the clone: the engine judges paths against the real
    # workspace, and until the clone lands there is no real workspace to judge
    # against. Constructing it earlier made every local path look like an escape.
    policy = PolicyEngine(settings=settings, workspace=repo_path.as_posix())

    checkpoint(Phase.BASELINE)
    try:
        sandbox = build_environment(repo_path)
    except EnvironmentBuildError as exc:
        return finish(Outcome.UNBUILDABLE, notes=str(exc)[:500])

    baseline = run_tests(sandbox)
    job.baseline_green = baseline.passed
    if baseline.internal_error:
        return finish(Outcome.INFRA_ERROR, notes=f"pytest exit {baseline.exit_code}")
    if not baseline.collected:
        # No tests means the suite cannot serve as evidence that a repair worked.
        # Reported as UNBUILDABLE with an explicit note rather than given its own
        # enum member: adding one requires an ADR. See docs/decisions/0003.
        return finish(Outcome.UNBUILDABLE, notes="pytest collected no tests")
    if not baseline.passed:
        return finish(Outcome.BASELINE_RED, notes="suite was already failing before the upgrade")

    checkpoint(Phase.UPGRADE)
    fixable = job.actionable_vulnerabilities
    if not fixable:
        return finish(Outcome.NO_FIX_AVAILABLE, notes="no published fix for any advisory")
    try:
        apply_upgrade(sandbox, fixable)
    except UpgradeError as exc:
        return finish(Outcome.INFRA_ERROR, notes=f"upgrade failed: {exc}"[:500])

    checkpoint(Phase.VERIFY)
    verified = run_tests(sandbox)

    repaired = False
    if not verified.passed:
        checkpoint(Phase.REPAIR)
        # Built here rather than at the top of ``handle``: a PATCHED_CLEAN job
        # never reaches this line, and it should never pay to construct an agent
        # it will not use.
        repaired = repair(job, sandbox, verified, policy, budget, build_repair_agent(settings))
        if not repaired:
            return finish(
                Outcome.REPAIR_EXHAUSTED, notes="ceiling reached with the suite still red"
            )

    checkpoint(Phase.OPENING_PR)
    pr_url = open_pull_request(job, sandbox, policy)
    return finish(
        Outcome.PATCHED_REPAIRED if repaired else Outcome.PATCHED_CLEAN, pr_url=pr_url
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("the worker is driven by Pub/Sub; use `make run-local REPO=owner/name`")
