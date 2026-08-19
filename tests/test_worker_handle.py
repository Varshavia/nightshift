"""The worker's phase machine, end to end, with a scripted agent.

Every member of ``Outcome`` this block can produce has a test here. That is what
makes the repair rate a number rather than a claim — the failures are named and
they are exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services.worker import main as worker
from services.worker.pull_request import PullRequestBlocked
from services.worker.repair import RepairProposal
from services.worker.toolchain import EnvironmentBuildError, Sandbox, TestReport

from nightshift_core.config import Ceilings, Settings
from nightshift_core.models import Outcome, RepoJob, Severity, Vulnerability
from nightshift_core.store import MemoryJobStore

SETTINGS = Settings(fork_org="nightshift-fleet", workspace_root="/tmp/nightshift-test")


class AlwaysRepairs:
    def attempt(self, context: object, tools: object) -> RepairProposal:
        return RepairProposal(rationale="fixed the import", tokens_used=100)


class NeverRepairs:
    def attempt(self, context: object, tools: object) -> RepairProposal:
        return RepairProposal(rationale="no idea", tokens_used=100)


def make_job() -> RepoJob:
    return RepoJob(
        job_id="run1:nightshift-fleet/example",
        repo="nightshift-fleet/example",
        vulnerabilities=[
            Vulnerability(
                osv_id="GHSA-a",
                package="jinja2",
                installed_version="2.11.3",
                fixed_version="3.1.2",
                severity=Severity.HIGH,
            )
        ],
    )


def patch_suite(monkeypatch: pytest.MonkeyPatch, results: list[bool]) -> None:
    """Patch run_tests in BOTH modules that resolve it.

    ``services.worker.main`` and ``services.worker.repair`` each import
    ``run_tests`` into their own namespace, so patching one does not reach the
    other. Getting this wrong makes the repair-loop path silently run the real
    pytest against an empty directory.
    """
    remaining = iter(results)

    def fake(sandbox: object, **kwargs: object) -> TestReport:
        return TestReport(passed=next(remaining), output="x", duration_seconds=0.1)

    monkeypatch.setattr(worker, "run_tests", fake)
    monkeypatch.setattr("services.worker.repair.run_tests", fake)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """Stub the toolchain so the phase machine is what is under test."""
    root = tmp_path / "repo"
    root.mkdir()
    sandbox = Sandbox(repo_path=root, python=Path("/usr/bin/python3"))
    monkeypatch.setattr(worker, "clone", lambda repo, workspace, token=None: root)
    monkeypatch.setattr(worker, "build_environment", lambda path: sandbox)
    monkeypatch.setattr(worker, "apply_upgrade", lambda sandbox, vulns: ["requirements.txt"])
    monkeypatch.setattr(
        worker,
        "open_pull_request",
        lambda job, sandbox, policy, settings=None: "https://github.com/x/y/pull/1",
    )
    # capture_diff is resolved inside services.worker.repair, not here.
    monkeypatch.setattr("services.worker.repair.capture_diff", lambda sandbox: "")
    # The upgrade-drift check reads real installed versions out of a real
    # interpreter. Stubbed to "intact" here because these tests are about the
    # phase machine; drift has its own file, tests/test_false_green.py.
    monkeypatch.setattr("services.worker.repair.upgrade_drift", lambda sandbox, vulns: [])
    return monkeypatch


def run(store: MemoryJobStore | None = None, settings: Settings = SETTINGS) -> RepoJob:
    # `store if store is not None`, never `store or ...`: MemoryJobStore defines
    # __len__, so an empty one is falsy and `or` would silently swap in a fresh
    # store — losing every checkpoint the caller wanted to inspect.
    return worker.handle(make_job(), store if store is not None else MemoryJobStore(), settings)


def test_an_unbuildable_environment_is_counted_not_raised(patched: pytest.MonkeyPatch) -> None:
    def boom(path: Path) -> Sandbox:
        raise EnvironmentBuildError("no recognised manifest")

    patched.setattr(worker, "build_environment", boom)
    job = run()
    assert job.outcome is Outcome.UNBUILDABLE


def test_a_suite_that_collects_nothing_is_unbuildable(patched: pytest.MonkeyPatch) -> None:
    patched.setattr(
        worker,
        "run_tests",
        lambda sandbox, **kw: TestReport(
            passed=False, output="", duration_seconds=0.1, collected=False, exit_code=5
        ),
    )
    job = run()
    assert job.outcome is Outcome.UNBUILDABLE
    assert "collected no tests" in job.notes


def test_a_red_baseline_stops_before_any_upgrade(patched: pytest.MonkeyPatch) -> None:
    patch_suite(patched, [False])
    job = run()
    assert job.outcome is Outcome.BASELINE_RED
    assert job.repair_attempts == []


def test_an_upgrade_that_breaks_nothing_is_patched_clean(patched: pytest.MonkeyPatch) -> None:
    patch_suite(patched, [True, True])
    job = run()
    assert job.outcome is Outcome.PATCHED_CLEAN
    assert job.repair_attempts == [], "no model was called"
    assert job.pr_url


def test_a_break_the_agent_fixes_is_patched_repaired(patched: pytest.MonkeyPatch) -> None:
    patch_suite(patched, [True, False, True])
    patched.setattr(worker, "build_repair_agent", lambda settings=None: AlwaysRepairs())
    job = run()
    assert job.outcome is Outcome.PATCHED_REPAIRED
    assert len(job.repair_attempts) == 1


def test_a_break_the_agent_cannot_fix_is_repair_exhausted(patched: pytest.MonkeyPatch) -> None:
    patch_suite(patched, [True] + [False] * 10)
    patched.setattr(worker, "build_repair_agent", lambda settings=None: NeverRepairs())
    settings = Settings(
        fork_org="nightshift-fleet",
        workspace_root="/tmp/nightshift-test",
        ceilings=Ceilings(max_repair_attempts=2),
    )
    job = run(settings=settings)
    assert job.outcome is Outcome.REPAIR_EXHAUSTED
    assert len(job.repair_attempts) == 2


def test_an_advisory_with_no_fix_never_reaches_the_upgrade(
    patched: pytest.MonkeyPatch,
) -> None:
    patch_suite(patched, [True])
    job = RepoJob(
        job_id="run1:nightshift-fleet/example",
        repo="nightshift-fleet/example",
        vulnerabilities=[
            Vulnerability(
                osv_id="GHSA-a", package="jinja2", installed_version="2.11.3",
                fixed_version=None, severity=Severity.HIGH,
            )
        ],
    )
    finished = worker.handle(job, MemoryJobStore(), SETTINGS)
    assert finished.outcome is Outcome.NO_FIX_AVAILABLE


def test_a_blocked_pull_request_is_policy_blocked(patched: pytest.MonkeyPatch) -> None:
    from nightshift_core.policy import Decision, Effect

    def blocked(job: object, sandbox: object, policy: object, settings: object = None) -> str:
        raise PullRequestBlocked(Decision(Effect.DENY, "upstream-pr-denied", "not our fork"))

    patch_suite(patched, [True, True])
    patched.setattr(worker, "open_pull_request", blocked)
    job = run()
    assert job.outcome is Outcome.POLICY_BLOCKED
    assert "upstream-pr-denied" in job.notes


def test_every_phase_is_checkpointed(patched: pytest.MonkeyPatch) -> None:
    """A worker that dies resumes from its last phase, so each one must be stored."""
    patch_suite(patched, [True, True])
    store = MemoryJobStore()
    job = run(store)
    assert store.get(job.job_id) is not None
    assert store.get(job.job_id).outcome is Outcome.PATCHED_CLEAN  # type: ignore[union-attr]
