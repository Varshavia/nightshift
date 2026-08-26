"""Can the agent make the suite green without fixing anything?

Every test here is an attempt to produce a **false green**: a passing suite, a
``PATCHED_REPAIRED`` outcome and an open pull request, with the advisory still
unfixed. That is the single most damaging thing this project could do, because
it is indistinguishable from success from the outside.

Five routes were open when this file was written, found by probing the engine
rather than by reading it:

    ALLOW  pip install jinja2==2.11.3           downgrade the library
    ALLOW  git checkout -- .                    revert the working tree
    ALLOW  git stash                            stash the upgrade
    ALLOW  pytest --deselect tests/x.py::test   deselect the failing test
    ALLOW  pytest --ignore=tests/x.py           skip the failing file

Three of them were harmless, but only by accident: the loop decides from its own
fixed-argv test run, so whatever the agent passes to pytest never reaches the
verdict. That accident is pinned down here so it cannot quietly stop being true.

The first was real. The instruction prompt tells the agent not to pin the
dependency back down — and this project's whole argument is that an instruction
is not a guarantee.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services.worker import repair as repair_module
from services.worker.repair import DRIFT_PREAMBLE, RepairContext, RepairProposal, run_repair_loop
from services.worker.toolchain import Sandbox, TestReport, UpgradeDrift, upgrade_drift
from services.worker.tools import SandboxTools

from nightshift_core.config import Ceilings, Settings
from nightshift_core.models import RepoJob, Severity, Vulnerability
from nightshift_core.policy import Budget, Effect, PolicyEngine, ToolCall

VULNERABILITY = Vulnerability(
    osv_id="GHSA-test",
    package="jinja2",
    installed_version="2.11.3",
    fixed_version="3.1.2",
    severity=Severity.HIGH,
)


def _still_red() -> TestReport:
    """A suite whose failure did not go away. Named because the loop now asks
    which tests are red rather than whether any are."""
    return TestReport(
        False, "boom", 0.1, tests_collected=10, failures=frozenset({"tests/test_x.py::test_y"})
    )


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(
        settings=Settings(fork_org="nightshift-fleet"),
        workspace="/workspace/repo",
        protected_packages=["jinja2"],
    )


# --------------------------------------------------------------------------- #
# Route 1 — reinstall the library at the version that was vulnerable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        ["pip", "install", "jinja2==2.11.3"],
        ["pip", "install", "-U", "jinja2==2.11.3"],
        ["pip3", "install", "Jinja2==2.11.3"],
        ["pip", "install", "jinja2[i18n]==2.11.3"],
        ["pip", "install", "jinja2<3"],
        ["pip", "uninstall", "-y", "jinja2"],
        ["uv", "pip", "install", "jinja2==2.11.3"],
        ["poetry", "add", "jinja2@2.11.3"],
    ],
)
def test_the_agent_cannot_reinstall_the_package_it_came_to_upgrade(
    engine: PolicyEngine, command: list[str]
) -> None:
    decision = engine.check(ToolCall("run_command", {"command": command}))
    assert decision.effect is Effect.DENY, command
    assert decision.rule == "no-downgrade"


def test_reinstalling_from_the_manifest_stays_allowed(engine: PolicyEngine) -> None:
    """The manifest already carries the new pin, so this re-applies the upgrade.

    Denying it would break the ordinary case — an agent that adds a missing test
    dependency and reinstalls — to defend against nothing.
    """
    for command in (["pip", "install", "-r", "requirements.txt"], ["pip", "install", "-e", "."]):
        assert engine.check(ToolCall("run_command", {"command": command})).allowed


def test_unrelated_packages_are_not_protected(engine: PolicyEngine) -> None:
    """Only the packages this job came to upgrade are off limits."""
    call = ToolCall("run_command", {"command": ["pip", "install", "freezegun"]})
    assert engine.check(call).allowed


def test_an_engine_with_nothing_to_protect_behaves_as_before() -> None:
    plain = PolicyEngine(settings=Settings(fork_org="x"), workspace="/workspace/repo")
    call = ToolCall("run_command", {"command": ["pip", "install", "jinja2==2.11.3"]})
    assert plain.check(call).allowed


# --------------------------------------------------------------------------- #
# Routes 4 and 5 — the agent cannot influence the verdict, and that is by design
# --------------------------------------------------------------------------- #


class DeselectingAgent:
    """Tries to win by telling pytest to skip the test that fails."""

    def __init__(self) -> None:
        self.contexts: list[RepairContext] = []

    def attempt(self, context: RepairContext, tools: SandboxTools) -> RepairProposal:
        self.contexts.append(context)
        tools.run_command(["pytest", "--deselect", "tests/test_a.py::test_broken"])
        tools.run_command(["pytest", "-k", "not broken"])
        return RepairProposal(rationale="deselected the failure", tokens_used=10)


def test_what_the_agent_runs_never_becomes_the_verdict(tmp_path: Path) -> None:
    """The loop re-runs the suite itself, with an argv the agent cannot reach.

    So ``--deselect``, ``--ignore`` and ``-k`` are inert. They are allowed by the
    executable allowlist and it does not matter — which is the correct shape, but
    only as long as the loop keeps deciding for itself. This test is what stops
    that from silently changing.
    """
    job, sandbox, tools, policy, budget = _fixtures(tmp_path)
    agent = DeselectingAgent()

    repaired = run_repair_loop(
        job,
        sandbox,
        TestReport(passed=False, output="boom", duration_seconds=0.1),
        policy,
        budget,
        agent,
        tools=tools,
        run_suite=lambda sandbox, **kwargs: TestReport(
            passed=False,
            output="still boom",
            duration_seconds=0.1,
            tests_collected=10,
            failures=frozenset({"tests/test_x.py::test_y"}),
        ),
        capture=lambda sandbox: "",
        check_drift=lambda sandbox, vulns: [],
    )

    assert repaired is False, "the agent's own pytest invocation must not decide anything"


# --------------------------------------------------------------------------- #
# The check that catches every route, including ones nobody has thought of
# --------------------------------------------------------------------------- #


class DowngradingAgent:
    """Makes the suite green by putting the vulnerable version back."""

    def __init__(self) -> None:
        self.contexts: list[RepairContext] = []

    def attempt(self, context: RepairContext, tools: SandboxTools) -> RepairProposal:
        self.contexts.append(context)
        return RepairProposal(rationale="downgraded", tokens_used=10)


def test_a_green_suite_with_the_upgrade_undone_is_not_a_repair(tmp_path: Path) -> None:
    job, sandbox, tools, policy, budget = _fixtures(tmp_path)
    agent = DowngradingAgent()
    drift = [UpgradeDrift(package="jinja2", expected="3.1.2", installed="2.11.3")]

    repaired = run_repair_loop(
        job,
        sandbox,
        TestReport(passed=False, output="boom", duration_seconds=0.1),
        policy,
        budget,
        agent,
        tools=tools,
        run_suite=lambda sandbox, **kwargs: TestReport(
            passed=True, output="all green", duration_seconds=0.1
        ),
        capture=lambda sandbox: "",
        check_drift=lambda sandbox, vulns: drift,
    )

    assert repaired is False, "a green suite proves nothing if the upgrade is gone"
    assert all(not attempt.tests_passed for attempt in job.repair_attempts)
    assert job.outcome is None, "the loop never decides the outcome"


def test_the_agent_is_told_why_its_green_did_not_count(tmp_path: Path) -> None:
    """A silent rejection would burn every remaining attempt the same way."""
    job, sandbox, tools, policy, budget = _fixtures(tmp_path)
    agent = DowngradingAgent()
    drift = [UpgradeDrift(package="jinja2", expected="3.1.2", installed="2.11.3")]

    run_repair_loop(
        job, sandbox,
        TestReport(passed=False, output="boom", duration_seconds=0.1),
        policy, budget, agent,
        tools=tools,
        run_suite=lambda sandbox, **kwargs: TestReport(True, "green", 0.1),
        capture=lambda sandbox: "",
        check_drift=lambda sandbox, vulns: drift,
    )

    second = agent.contexts[1].failing_output
    assert DRIFT_PREAMBLE in second
    assert "expected 3.1.2, found 2.11.3" in second


def test_drift_is_only_checked_when_there_is_a_green_to_doubt(tmp_path: Path) -> None:
    """Reading installed versions costs a subprocess; a red suite has not earned one."""
    job, sandbox, tools, policy, budget = _fixtures(tmp_path)
    calls: list[object] = []

    def record(sandbox: Sandbox, vulnerabilities: object) -> list[UpgradeDrift]:
        calls.append(vulnerabilities)
        return []

    run_repair_loop(
        job, sandbox,
        TestReport(passed=False, output="boom", duration_seconds=0.1),
        policy, budget, DowngradingAgent(),
        tools=tools,
        run_suite=lambda sandbox, **kwargs: _still_red(),
        capture=lambda sandbox: "",
        check_drift=record,
    )

    assert calls == []


def test_an_intact_upgrade_with_a_green_suite_is_a_repair(tmp_path: Path) -> None:
    job, sandbox, tools, policy, budget = _fixtures(tmp_path)
    repaired = run_repair_loop(
        job, sandbox,
        TestReport(passed=False, output="boom", duration_seconds=0.1),
        policy, budget, DowngradingAgent(),
        tools=tools,
        run_suite=lambda sandbox, **kwargs: TestReport(True, "green", 0.1),
        capture=lambda sandbox: "",
        check_drift=lambda sandbox, vulns: [],
    )
    assert repaired is True


# --------------------------------------------------------------------------- #
# Reading the environment rather than the manifest
# --------------------------------------------------------------------------- #


def _stub_versions(monkeypatch: pytest.MonkeyPatch, found: dict[str, str | None]) -> None:
    monkeypatch.setattr(
        "services.worker.toolchain.installed_versions",
        lambda sandbox, packages: {name: found.get(name) for name in packages},
    )


def test_the_upgrade_is_intact_when_the_installed_version_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_versions(monkeypatch, {"jinja2": "3.1.2"})
    sandbox = Sandbox(repo_path=tmp_path, python=Path("/usr/bin/python3"))
    assert upgrade_drift(sandbox, [VULNERABILITY]) == []


def test_equivalent_version_spellings_are_not_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``3.1.2`` and ``3.1.2.0`` are the same release. Reporting drift here would
    fail honest repairs for a formatting difference."""
    _stub_versions(monkeypatch, {"jinja2": "3.1.2.0"})
    sandbox = Sandbox(repo_path=tmp_path, python=Path("/usr/bin/python3"))
    assert upgrade_drift(sandbox, [VULNERABILITY]) == []


@pytest.mark.parametrize("installed", ["2.11.3", "3.1.1", "3.1.2.post1", None])
def test_anything_other_than_the_fixed_version_is_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, installed: str | None
) -> None:
    """``3.1.2.post1`` counts too: it is a different release, and we did not
    choose it. Whatever put it there is something we did not verify."""
    _stub_versions(monkeypatch, {"jinja2": installed})
    sandbox = Sandbox(repo_path=tmp_path, python=Path("/usr/bin/python3"))
    drift = upgrade_drift(sandbox, [VULNERABILITY])
    assert [entry.package for entry in drift] == ["jinja2"]
    assert drift[0].installed == installed


def test_an_advisory_with_no_fix_cannot_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_versions(monkeypatch, {})
    unfixable = Vulnerability(
        osv_id="GHSA-b", package="abandoned", installed_version="1.0", fixed_version=None
    )
    sandbox = Sandbox(repo_path=tmp_path, python=Path("/usr/bin/python3"))
    assert upgrade_drift(sandbox, [unfixable]) == []


# --------------------------------------------------------------------------- #
# The wall clock covers the whole job, not just the loop
# --------------------------------------------------------------------------- #


def test_the_wall_clock_counts_from_the_start_of_the_job() -> None:
    """Clone, build and two suite runs are the slowest phases of a job.

    Accumulating only per-attempt durations excluded all of them, so a repository
    that took twenty-five minutes to install entered the repair loop with a
    wall-clock ceiling that had not started counting.
    """
    budget = Budget()
    budget.start(1_000.0)
    budget.tick(1_000.0 + 1_500.0)
    assert budget.elapsed_seconds == 1_500.0


def test_the_clock_is_inert_until_it_is_started() -> None:
    budget = Budget()
    budget.tick(9_999.0)
    assert budget.elapsed_seconds == 0.0


def test_time_spent_before_the_loop_still_counts_against_the_ceiling() -> None:
    engine = PolicyEngine(
        settings=Settings(ceilings=Ceilings(max_job_seconds=600)), workspace="/workspace/repo"
    )
    budget = Budget()
    budget.start(0.0)
    budget.tick(601.0)  # spent entirely on cloning and installing
    decision = engine.check(ToolCall("read_file", {"path": "app.py"}), budget)
    assert decision.effect is Effect.DENY
    assert decision.rule == "ceiling-wallclock"


# --------------------------------------------------------------------------- #


def _fixtures(
    tmp_path: Path,
) -> tuple[RepoJob, Sandbox, SandboxTools, PolicyEngine, Budget]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    settings = Settings(
        fork_org="nightshift-fleet",
        ceilings=Ceilings(max_repair_attempts=2, max_job_seconds=600, max_job_tokens=100_000),
    )
    policy = PolicyEngine(
        settings=settings, workspace=root.as_posix(), protected_packages=["jinja2"]
    )
    budget = Budget()
    sandbox = Sandbox(repo_path=root, python=Path("/usr/bin/python3"))
    tools = SandboxTools(sandbox=sandbox, policy=policy, budget=budget)
    job = RepoJob(
        job_id="run:owner/name", repo="owner/name", vulnerabilities=[VULNERABILITY]
    )
    return job, sandbox, tools, policy, budget


def test_the_loop_resolves_the_real_check_by_default() -> None:
    """A stub reaching production would disable every guarantee in this file."""
    assert getattr(repair_module, "upgrade_drift") is upgrade_drift  # noqa: B009
