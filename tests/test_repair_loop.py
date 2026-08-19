"""The repair loop, exercised with a fake agent and no model call.

The loop's contract is narrow and worth stating: it decides success from the
test suite, never from the agent's own report, and it stops at the ceiling.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from services.worker.repair import RepairContext, RepairProposal, run_repair_loop
from services.worker.toolchain import Sandbox, TestReport, UpgradeDrift
from services.worker.tools import SandboxTools

from nightshift_core.config import Ceilings, Settings
from nightshift_core.models import RepoJob, Severity, Vulnerability
from nightshift_core.policy import Budget, PolicyEngine

VULNERABILITY = Vulnerability(
    osv_id="GHSA-test",
    package="jinja2",
    installed_version="2.11.3",
    fixed_version="3.1.2",
    severity=Severity.HIGH,
)

Fixtures = tuple[RepoJob, Sandbox, SandboxTools, PolicyEngine, Budget]


class ScriptedAgent:
    """Returns a fixed rationale per attempt and records what it was given."""

    def __init__(self, *, tokens: int = 1000) -> None:
        self.contexts: list[RepairContext] = []
        self._tokens = tokens

    def attempt(self, context: RepairContext, tools: SandboxTools) -> RepairProposal:
        self.contexts.append(context)
        tools.write_file("app.py", f"# attempt {context.attempt}\n")
        return RepairProposal(rationale=f"attempt {context.attempt}", tokens_used=self._tokens)


def make_suite(results: list[bool]) -> Callable[..., TestReport]:
    """A stand-in for ``run_tests`` that yields the given pass/fail sequence."""
    remaining = list(results)

    def run_suite(sandbox: Sandbox, **kwargs: object) -> TestReport:
        passed = remaining.pop(0) if remaining else False
        return TestReport(
            passed=passed, output="green" if passed else "boom", duration_seconds=0.1
        )

    return run_suite


def no_drift(sandbox: Sandbox, vulnerabilities: object) -> list[UpgradeDrift]:
    """The upgrade is still installed.

    Injected into the tests below because they are about control flow — attempts,
    ceilings, what the agent is handed. Whether a green suite is *trustworthy* is
    a separate question with its own file, tests/test_false_green.py.
    """
    return []


def failure(output: str = "ImportError") -> TestReport:
    return TestReport(passed=False, output=output, duration_seconds=0.1)


@pytest.fixture
def fixture_set(tmp_path: Path) -> Fixtures:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    settings = Settings(
        fork_org="nightshift-fleet",
        ceilings=Ceilings(max_repair_attempts=3, max_job_seconds=600, max_job_tokens=100_000),
    )
    policy = PolicyEngine(settings=settings, workspace=root.as_posix())
    budget = Budget()
    sandbox = Sandbox(repo_path=root, python=Path("/usr/bin/python3"))
    tools = SandboxTools(sandbox=sandbox, policy=policy, budget=budget)
    job = RepoJob(job_id="run:owner/name", repo="owner/name", vulnerabilities=[VULNERABILITY])
    return job, sandbox, tools, policy, budget


def test_a_repair_that_works_on_the_first_attempt(fixture_set: Fixtures) -> None:
    job, sandbox, tools, policy, budget = fixture_set
    repaired = run_repair_loop(
        job, sandbox, failure(), policy, budget, ScriptedAgent(),
        tools=tools, run_suite=make_suite([True]),
        check_drift=no_drift,
    )
    assert repaired is True
    assert len(job.repair_attempts) == 1
    assert job.repair_attempts[0].tests_passed is True


def test_the_loop_stops_at_the_attempt_ceiling(fixture_set: Fixtures) -> None:
    job, sandbox, tools, policy, budget = fixture_set
    repaired = run_repair_loop(
        job, sandbox, failure(), policy, budget, ScriptedAgent(),
        tools=tools, run_suite=make_suite([False, False, False, False]),
        check_drift=no_drift,
    )
    assert repaired is False
    assert len(job.repair_attempts) == 3, "ceilings.max_repair_attempts is 3"


def test_every_attempt_is_recorded_even_when_it_fails(fixture_set: Fixtures) -> None:
    """A failed attempt is the input to the next one and the material for the Ledger."""
    job, sandbox, tools, policy, budget = fixture_set
    run_repair_loop(
        job, sandbox, failure(), policy, budget, ScriptedAgent(),
        tools=tools, run_suite=make_suite([False, True]),
        check_drift=no_drift,
    )
    assert [a.tests_passed for a in job.repair_attempts] == [False, True]
    assert all(a.rationale for a in job.repair_attempts)


def test_the_agent_sees_the_previous_failure_not_the_original(fixture_set: Fixtures) -> None:
    job, sandbox, tools, policy, budget = fixture_set
    agent = ScriptedAgent()
    run_repair_loop(
        job, sandbox, failure("first failure"), policy, budget, agent,
        tools=tools, run_suite=make_suite([False, True]),
        check_drift=no_drift,
    )
    assert agent.contexts[0].failing_output == "first failure"
    assert agent.contexts[1].failing_output == "boom", "attempt 2 sees attempt 1's result"
    assert len(agent.contexts[1].previous) == 1


def test_tokens_are_spent_against_the_budget(fixture_set: Fixtures) -> None:
    job, sandbox, tools, policy, budget = fixture_set
    run_repair_loop(
        job, sandbox, failure("boom"), policy, budget, ScriptedAgent(tokens=1500),
        tools=tools, run_suite=make_suite([False, True]),
        check_drift=no_drift,
    )
    assert budget.tokens == 3000
    assert job.tokens_used == 3000


def test_the_token_ceiling_ends_the_loop(fixture_set: Fixtures) -> None:
    """A ceiling is a real result, not an error."""
    job, sandbox, tools, policy, budget = fixture_set
    repaired = run_repair_loop(
        job, sandbox, failure("boom"), policy, budget, ScriptedAgent(tokens=60_000),
        tools=tools, run_suite=make_suite([False, False, False]),
        check_drift=no_drift,
    )
    assert repaired is False
    assert len(job.repair_attempts) == 2, "the third attempt exceeds max_job_tokens"


def test_a_diff_is_recorded_on_each_attempt(fixture_set: Fixtures) -> None:
    job, sandbox, tools, policy, budget = fixture_set
    run_repair_loop(
        job, sandbox, failure("boom"), policy, budget, ScriptedAgent(),
        tools=tools, run_suite=make_suite([True]),
        check_drift=no_drift,
        capture=lambda sandbox: "diff --git a/app.py b/app.py\n+# attempt 1\n",
    )
    assert "attempt 1" in job.repair_attempts[0].diff


def test_the_job_is_never_finished_by_the_loop(fixture_set: Fixtures) -> None:
    """Outcome is the caller's decision; the loop only reports green or not."""
    job, sandbox, tools, policy, budget = fixture_set
    run_repair_loop(
        job, sandbox, failure("boom"), policy, budget, ScriptedAgent(),
        tools=tools, run_suite=make_suite([True]),
        check_drift=no_drift,
    )
    assert job.outcome is None
    assert job.phase is not None
