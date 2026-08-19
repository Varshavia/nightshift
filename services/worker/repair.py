"""The bounded repair loop.

This is the product. Its contract is deliberately narrow:

- **The suite decides.** The agent is never asked whether it succeeded. It makes
  a change; the repository's own tests are re-run; that result is the truth.
- **Every attempt is recorded**, successful or not. A failed attempt is the input
  to the next one, and afterwards it is the material the Ledger is built from.
- **The ceiling is checked before each attempt**, through the same policy engine
  that gates every tool call, so there is exactly one implementation of "enough".

The agent arrives through :class:`RepairAgent`, a Protocol, so the whole loop can
be exercised with a scripted stand-in and no token spent.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from nightshift_core.models import RepairAttempt, RepoJob, Vulnerability
from nightshift_core.policy import Budget, PolicyEngine, ToolCall
from services.worker.toolchain import (
    Sandbox,
    TestReport,
    UpgradeDrift,
    capture_diff,
    run_tests,
    upgrade_drift,
)
from services.worker.tools import SandboxTools

__all__ = [
    "DRIFT_PREAMBLE",
    "RepairAgent",
    "RepairContext",
    "RepairProposal",
    "run_repair_loop",
]

log = logging.getLogger("nightshift.repair")

#: What the agent is told when it made the suite green by undoing the upgrade.
#: Phrased as a failure because that is what it is — the job is not closer to
#: done than it was before the attempt.
DRIFT_PREAMBLE = (
    "The suite passes, but the upgrade is no longer installed, so this proves "
    "nothing. The upgrade is the point; the tests exist to show the calling code "
    "survives it.\n"
)


@dataclass(frozen=True, slots=True)
class RepairContext:
    """Everything the agent is given for one attempt, and nothing else."""

    repo: str
    vulnerabilities: tuple[Vulnerability, ...]
    failing_output: str
    attempt: int
    previous: tuple[RepairAttempt, ...] = ()
    #: What the Ledger offered, already rendered with its confidence stated.
    #: Empty on a miss. It is prior art the agent may use, never an instruction
    #: it must follow — the suite remains the only measure of success, so a
    #: wrong recipe costs attempts and cannot manufacture a green.
    recipe: str = ""


@dataclass(frozen=True, slots=True)
class RepairProposal:
    """What the agent reports back. Notably absent: whether it worked."""

    rationale: str
    tokens_used: int = 0


class RepairAgent(Protocol):
    """Anything that can make one conceptual fix inside a sandbox."""

    def attempt(self, context: RepairContext, tools: SandboxTools) -> RepairProposal: ...


def run_repair_loop(
    job: RepoJob,
    sandbox: Sandbox,
    failure: TestReport,
    policy: PolicyEngine,
    budget: Budget,
    agent: RepairAgent,
    *,
    tools: SandboxTools | None = None,
    run_suite: Callable[..., TestReport] | None = None,
    capture: Callable[[Sandbox], str] | None = None,
    check_drift: Callable[..., Sequence[UpgradeDrift]] | None = None,
    recipe: str = "",
) -> bool:
    """Repair until the suite is green or a ceiling is reached. True when green.

    Never finishes the job — the caller owns the ``Outcome``, because the same
    green suite means ``PATCHED_REPAIRED`` here and something else in a probe.
    """
    tools = tools or SandboxTools(sandbox=sandbox, policy=policy, budget=budget)
    # Resolved here rather than as default arguments: a default binds the
    # function object at definition time, so patching the module attribute in a
    # test would never reach it and the suite would quietly run for real.
    run_suite = run_suite or run_tests
    capture = capture or capture_diff
    check_drift = check_drift or upgrade_drift
    failing_output = failure.output

    while True:
        attempt_number = budget.attempts + 1

        # The ceiling is asked about the attempt we are *about to* make, not the
        # ones already made. ``Budget.attempts`` counts completed attempts, and
        # the engine denies when that count exceeds the ceiling — so checking the
        # live budget here would let a fourth attempt run under a ceiling of
        # three. This is the same rule the engine applies to every tool call;
        # only the question is phrased in the future tense.
        prospective = Budget(
            attempts=attempt_number,
            tokens=budget.tokens,
            elapsed_seconds=budget.elapsed_seconds,
        )
        decision = policy.check(ToolCall("run_command", {"command": ["pytest"]}), prospective)
        if not decision.allowed:
            log.info(
                "job %s stopped before attempt %d by %s",
                job.job_id,
                attempt_number,
                decision.rule,
            )
            return False

        started = time.monotonic()
        context = RepairContext(
            repo=job.repo,
            vulnerabilities=tuple(job.vulnerabilities),
            failing_output=failing_output,
            attempt=attempt_number,
            previous=tuple(job.repair_attempts),
            recipe=recipe,
        )
        proposal = agent.attempt(context, tools)

        verdict = run_suite(sandbox)
        duration = time.monotonic() - started

        # A green suite is necessary and not sufficient. Every tool the agent has
        # is gated, but the engine reasons about actions and there are more ways
        # to change an environment than an allowlist can enumerate. So the
        # outcome is checked directly: if the library we came to upgrade is not
        # the version we upgraded it to, the suite passing means nothing.
        drift = tuple(check_drift(sandbox, job.vulnerabilities)) if verdict.passed else ()
        green = verdict.passed and not drift

        job.record_attempt(
            RepairAttempt(
                attempt=attempt_number,
                failing_output=failing_output,
                diff=capture(sandbox),
                rationale=proposal.rationale,
                tests_passed=green,
                tokens_used=proposal.tokens_used,
                duration_seconds=duration,
            )
        )
        budget.spend(tokens=proposal.tokens_used, attempts=1)
        budget.tick(time.monotonic())

        if green:
            log.info("job %s repaired on attempt %d", job.job_id, attempt_number)
            return True

        if drift:
            log.warning(
                "job %s went green on attempt %d by undoing the upgrade: %s",
                job.job_id,
                attempt_number,
                "; ".join(str(entry) for entry in drift),
            )
            failing_output = DRIFT_PREAMBLE + "\n".join(str(entry) for entry in drift)
        else:
            failing_output = verdict.output
