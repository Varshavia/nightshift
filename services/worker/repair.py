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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from nightshift_core.models import RepairAttempt, RepoJob, Vulnerability
from nightshift_core.policy import Budget, PolicyEngine, ToolCall
from services.worker.toolchain import Sandbox, TestReport, capture_diff, run_tests
from services.worker.tools import SandboxTools

__all__ = [
    "RepairAgent",
    "RepairContext",
    "RepairProposal",
    "run_repair_loop",
]

log = logging.getLogger("nightshift.repair")


@dataclass(frozen=True, slots=True)
class RepairContext:
    """Everything the agent is given for one attempt, and nothing else."""

    repo: str
    vulnerabilities: tuple[Vulnerability, ...]
    failing_output: str
    attempt: int
    previous: tuple[RepairAttempt, ...] = ()


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
    run_suite: Callable[..., TestReport] = run_tests,
    capture: Callable[[Sandbox], str] = capture_diff,
) -> bool:
    """Repair until the suite is green or a ceiling is reached. True when green.

    Never finishes the job — the caller owns the ``Outcome``, because the same
    green suite means ``PATCHED_REPAIRED`` here and something else in a probe.
    """
    tools = tools or SandboxTools(sandbox=sandbox, policy=policy, budget=budget)
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
        )
        proposal = agent.attempt(context, tools)

        verdict = run_suite(sandbox)
        duration = time.monotonic() - started

        job.record_attempt(
            RepairAttempt(
                attempt=attempt_number,
                failing_output=failing_output,
                diff=capture(sandbox),
                rationale=proposal.rationale,
                tests_passed=verdict.passed,
                tokens_used=proposal.tokens_used,
                duration_seconds=duration,
            )
        )
        budget.spend(tokens=proposal.tokens_used, seconds=duration, attempts=1)

        if verdict.passed:
            log.info("job %s repaired on attempt %d", job.job_id, attempt_number)
            return True

        failing_output = verdict.output
