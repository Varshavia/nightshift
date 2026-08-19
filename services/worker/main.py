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
import time
from pathlib import Path

from nightshift_core import telemetry
from nightshift_core.config import Settings, get_settings
from nightshift_core.ledger import (
    LedgerHit,
    MigrationLedger,
    Retrieval,
    build_ledger,
    scopes_from_job,
)
from nightshift_core.models import Outcome, Phase, RepoJob
from nightshift_core.policy import Budget, PolicyEngine
from nightshift_core.store import JobStore
from services.worker.agent import build_repair_agent
from services.worker.librarian import Librarian, shelve_repair
from services.worker.pull_request import PullRequestBlocked, PyGithubClient, open_pr
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


def consult_ledger(job: RepoJob, ledger: MigrationLedger) -> tuple[Retrieval | None, str]:
    """Ask the Ledger about this job's transitions before the loop starts.

    A job can carry several advisories; the Ledger is asked about each and the
    best answer wins, exact before near. Offering every recipe at once would
    bury the relevant one and spend context on transitions that are not what
    broke this repository.

    Never raises. A Ledger outage degrades the fleet to cold repair — full price,
    correct result — and must not cost a repository its run.
    """
    best: Retrieval | None = None
    try:
        for scope in scopes_from_job(job.vulnerabilities):
            retrieval = ledger.lookup(scope)
            if retrieval.hit is LedgerHit.EXACT:
                best = retrieval
                break
            if retrieval.hit is LedgerHit.NEAR and best is None:
                best = retrieval
    except Exception:
        log.warning("ledger lookup failed; repairing cold", exc_info=True)
        return None, ""
    if best is None:
        return None, ""
    return best, best.as_prompt_section()


def record_in_ledger(
    job: RepoJob, ledger: MigrationLedger, retrieval: Retrieval | None, outcome: Outcome
) -> None:
    """Tell the Ledger what happened after it was consulted.

    Only retrievals are recorded. A repository the Ledger never helped says
    nothing about whether a recipe works, and counting it would turn the
    confirmation count into a measure of fleet size.
    """
    if retrieval is None or retrieval.recipe is None:
        return
    try:
        ledger.record_outcome(
            retrieval.requested,
            repo=job.repo,
            hit=retrieval.hit,
            outcome=outcome,
            attempts_used=len(job.repair_attempts),
            osv_id=next((v.osv_id for v in job.vulnerabilities), ""),
        )
    except Exception:
        log.warning("could not record outcome in the ledger", exc_info=True)


def repair(
    job: RepoJob,
    sandbox: Sandbox,
    failure: TestReport,
    policy: PolicyEngine,
    budget: Budget,
    agent: RepairAgent,
    recipe: str = "",
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
    return run_repair_loop(job, sandbox, failure, policy, budget, agent, recipe=recipe)


def open_pull_request(
    job: RepoJob, sandbox: Sandbox, policy: PolicyEngine, settings: Settings | None = None
) -> str:
    """Open the PR from ``templates/pr_body.md``. Returns its url.

    The body carries the advisory, the version transition, the repair diff, the
    agent's explanation, and the AI-authorship disclosure. Nothing merges itself.
    """
    settings = settings or get_settings()
    client = PyGithubClient(settings.github_token or "")
    return open_pr(
        job,
        sandbox,
        policy,
        settings,
        client,
        baseline_green=bool(job.baseline_green),
        model=settings.repair_model,
    )


def handle(
    job: RepoJob,
    store: JobStore,
    settings: Settings | None = None,
    ledger: MigrationLedger | None = None,
    librarian: Librarian | None = None,
) -> RepoJob:
    """Process one job to a terminal outcome. Checkpointed at every phase.

    Wrapped in one ``job`` span whose attributes are the cost curve: which
    Ledger tier answered, what the job cost, how it ended. They are written on
    exit, because two of the three are only known then.
    """
    settings = settings or get_settings()
    ledger = ledger or build_ledger(
        project=settings.gcp_project, database=settings.firestore_database
    )
    budget = Budget()
    budget.start(time.monotonic())
    workspace = Path(settings.workspace_root) / job.job_id.replace(":", "_").replace("/", "_")

    with telemetry.span(
        "job", **{telemetry.JOB_ID: job.job_id, telemetry.REPO: job.repo}
    ) as trace:
        result = _run(job, store, settings, ledger, budget, workspace, librarian)
        trace[telemetry.LEDGER_HIT] = result.ledger_hit
        trace[telemetry.TOKENS] = result.tokens_used
        trace[telemetry.ATTEMPT] = len(result.repair_attempts)
        trace[telemetry.OUTCOME] = str(result.outcome) if result.outcome else ""
        return result


def _run(
    job: RepoJob,
    store: JobStore,
    settings: Settings,
    ledger: MigrationLedger,
    budget: Budget,
    workspace: Path,
    librarian: Librarian | None,
) -> RepoJob:
    def checkpoint(phase: Phase) -> None:
        job.advance(phase)
        store.put(job)

    def finish(outcome: Outcome, *, pr_url: str | None = None, notes: str = "") -> RepoJob:
        job.finish(outcome, pr_url=pr_url, notes=notes)
        store.put(job)
        log.info(
            "job %s finished as %s (%s hit, %d tokens)",
            job.job_id, job.outcome, job.ledger_hit, job.tokens_used,
        )
        return job

    checkpoint(Phase.CLONING)
    try:
        repo_path = clone(job.repo, workspace, token=settings.github_token)
    except EnvironmentBuildError as exc:
        return finish(Outcome.INFRA_ERROR, notes=f"clone failed: {exc}"[:500])

    # Built here, not before the clone: the engine judges paths against the real
    # workspace, and until the clone lands there is no real workspace to judge
    # against. Constructing it earlier made every local path look like an escape.
    # The protected set is the packages this job came to upgrade. Passing it
    # here rather than hard-coding a rule keeps the engine general: it refuses to
    # let the agent reinstall *these* packages by name, and says nothing about
    # any others.
    policy = PolicyEngine(
        settings=settings,
        workspace=repo_path.as_posix(),
        protected_packages=[v.package for v in job.vulnerabilities],
    )

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
    # Clone, build and two full suite runs happened before this line, and they
    # are the slowest part of a job. The repair loop inherits the time they
    # spent rather than starting from zero.
    budget.tick(time.monotonic())

    repaired = False
    retrieval = None
    if not verified.passed:
        checkpoint(Phase.REPAIR)
        # Consulted only once the upgrade has actually broken something. A
        # PATCHED_CLEAN job has nothing to look up, and asking anyway would put
        # a hit on the curve for a repository that never needed one.
        retrieval, recipe = consult_ledger(job, ledger)
        job.ledger_hit = str(retrieval.hit) if retrieval else str(LedgerHit.MISS)
        store.put(job)
        # Built here rather than at the top of ``handle``: a PATCHED_CLEAN job
        # never reaches this line, and it should never pay to construct an agent
        # it will not use.
        repaired = repair(
            job, sandbox, verified, policy, budget, build_repair_agent(settings), recipe=recipe
        )
        if not repaired:
            record_in_ledger(job, ledger, retrieval, Outcome.REPAIR_EXHAUSTED)
            return finish(
                Outcome.REPAIR_EXHAUSTED, notes="ceiling reached with the suite still red"
            )

    checkpoint(Phase.OPENING_PR)
    try:
        pr_url = open_pull_request(job, sandbox, policy, settings)
    except PullRequestBlocked as exc:
        # A refusal here is one the job cannot proceed past — unlike a denied
        # tool call inside the repair loop, which the agent can recover from.
        return finish(Outcome.POLICY_BLOCKED, notes=str(exc)[:500])

    outcome = Outcome.PATCHED_REPAIRED if repaired else Outcome.PATCHED_CLEAN
    # Recorded after the pull request exists, not after the suite went green: a
    # repair nobody can review is not evidence that the recipe worked.
    record_in_ledger(job, ledger, retrieval, outcome)

    # Only a repair teaches anything, and only if there is a Librarian to ask.
    # Passed in rather than constructed here so that a fleet running without
    # model access for the write path still repairs — it simply stops learning,
    # which is a degradation and not a failure.
    if librarian is not None and repaired:
        for scope in scopes_from_job(job.actionable_vulnerabilities):
            shelve_repair(job, scope, ledger, librarian)

    return finish(outcome, pr_url=pr_url)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("the worker is driven by Pub/Sub; use `make run-local REPO=owner/name`")
