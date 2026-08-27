"""The worker's phase machine, end to end, with a scripted agent.

Every member of ``Outcome`` this block can produce has a test here. That is what
makes the repair rate a number rather than a claim — the failures are named and
they are exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from services.worker import main as worker
from services.worker.interpreter import InterpreterChoice
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
        # A report thin enough to be a bool was fine while the gate was
        # `baseline.passed`. It is not fine now: the worker asks how much of the
        # suite is green and which tests are red, and a stub that answers "zero
        # collected, none failing" is a repository with no tests.
        passed = next(remaining)
        return TestReport(
            passed=passed,
            output="x",
            duration_seconds=0.1,
            tests_collected=10,
            failures=frozenset() if passed else frozenset({"tests/test_x.py::test_y"}),
        )

    monkeypatch.setattr(worker, "run_tests", fake)
    monkeypatch.setattr("services.worker.repair.run_tests", fake)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """Stub the toolchain so the phase machine is what is under test."""
    root = tmp_path / "repo"
    root.mkdir()
    sandbox = Sandbox(repo_path=root, python=Path("/usr/bin/python3"))
    monkeypatch.setattr(worker, "clone", lambda repo, workspace, token=None: root)
    # `**kwargs` because the worker now chooses the interpreter and hands it
    # down: a stub with the old signature would fail on the argument rather
    # than on anything this test is about.
    monkeypatch.setattr(worker, "build_environment", lambda path, **kw: sandbox)
    # No second rung by default. Asking for one downloads an interpreter, and a
    # unit test that reaches the network is a unit test that fails on a train.
    # The ladder has its own test below, with a stub in place of the download.
    monkeypatch.setattr(worker, "older_interpreter", lambda path: None)
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
    def boom(path: Path, **kwargs: object) -> Sandbox:
        raise EnvironmentBuildError("no recognised manifest")

    patched.setattr(worker, "build_environment", boom)
    job = run()
    assert job.outcome is Outcome.UNBUILDABLE


def test_a_repository_that_will_not_build_is_offered_the_world_it_was_written_for(
    patched: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The second rung, and why there is one.

    `requires-python` only became common around 2019, so a repository dormant
    since before then declares nothing and is handed whatever the worker runs.
    What it pins is the world of its last commit, built for an interpreter that
    still had `distutils` and still had wheels published for it. Six dormant
    repositories went through the fleet in one run: two would not install and
    three had nothing in the suite passing, every one of them on an interpreter
    we chose rather than one they asked for.
    """
    _patch_baseline(patched, collected=10, failing=0)
    offered: list[object] = []
    older = InterpreterChoice(python=tmp_path / "python3.9", version="3.9", source="stub")
    patched.setattr(worker, "older_interpreter", lambda path: older)

    def only_the_old_world(path: Path, **kwargs: object) -> Sandbox:
        offered.append(kwargs.get("choice"))
        if kwargs.get("choice") is None:
            raise EnvironmentBuildError("the project would not install")
        return Sandbox(repo_path=path, python=Path("/usr/bin/python3"))

    patched.setattr(worker, "build_environment", only_the_old_world)
    job = run()

    assert offered == [None, older], "the modern interpreter first, then the older one"
    assert job.outcome is Outcome.PATCHED_CLEAN, "the second rung is a real attempt"


def test_the_ladder_stops_at_two_rungs(patched: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Each rung is a full environment build inside a job with a wall clock. A
    ladder without a ceiling is how a job spends its whole half hour proving what
    the first rung already said."""
    attempts = 0
    older = InterpreterChoice(python=tmp_path / "python3.9", version="3.9", source="stub")
    patched.setattr(worker, "older_interpreter", lambda path: older)

    def never_builds(path: Path, **kwargs: object) -> Sandbox:
        nonlocal attempts
        attempts += 1
        raise EnvironmentBuildError("the project would not install")

    patched.setattr(worker, "build_environment", never_builds)
    job = run()

    assert attempts == 2
    assert job.outcome is Outcome.UNBUILDABLE
    assert "older interpreter" in job.notes, "both rungs are in the record, not just the last"


def test_a_repository_that_named_a_ceiling_is_not_second_guessed(tmp_path: Path) -> None:
    """It was given what it asked for. Reaching past a bound it published is not
    a second attempt, it is ignoring the answer."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.8,<3.10"\n', encoding="utf-8"
    )

    assert worker.older_interpreter(tmp_path) is None


def test_an_open_ended_requirement_does_not_close_the_door(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bonobo` publishes `>=3.5` from a setup.py written before 3.7 existed.

    Treated as a statement about 3.12 it bought forty-one collection errors and
    one passing test, and no second attempt, because the repository had
    technically "said something".
    """
    (tmp_path / "setup.py").write_text("setup(python_requires='>=3.5')\n", encoding="utf-8")
    # The fetch is a download; what is under test is whether one is asked for.
    monkeypatch.setattr(worker, "resolve", lambda version, **kw: tmp_path / "python3.9")

    choice = worker.older_interpreter(tmp_path)

    assert choice is not None and choice.version == "3.9"


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


def test_a_runner_that_will_not_start_is_not_returned_to_the_queue(
    patched: pytest.MonkeyPatch,
) -> None:
    """Exit 3 and exit 4 used to be INFRA_ERROR, which is nacked on purpose.

    Two repositories produced one on every delivery, for as long as the fleet
    had been running: a container, a clone and a full environment build each
    time, to reach the same conclusion, while everything behind them waited.
    Thirty-one of fifty-two finished jobs in a single night were this.

    A verdict a retry cannot change belongs to the repository, and the queue has
    to be allowed to let go of it.
    """
    patched.setattr(
        worker,
        "run_tests",
        lambda sandbox, **kw: TestReport(
            passed=False, output="", duration_seconds=0.1, exit_code=3
        ),
    )
    job = run()

    assert job.outcome is Outcome.UNBUILDABLE, "INFRA_ERROR would put it back on the queue"
    assert "exit 3" in job.notes, "the reason has to survive into the record"


def test_a_suite_where_nothing_passes_stops_before_any_upgrade(
    patched: pytest.MonkeyPatch,
) -> None:
    """BASELINE_RED now means what it says: no part of this suite works here.

    It used to mean "one test somewhere is red", which threw away a repository
    with a hundred passing tests over a single failure belonging to our
    container — and filed our limitation as the repository's condition.
    """
    _patch_baseline(patched, collected=8, failing=8)
    job = run()
    assert job.outcome is Outcome.BASELINE_RED
    assert job.repair_attempts == []


def test_a_mostly_red_suite_is_our_environment_not_their_code(
    patched: pytest.MonkeyPatch,
) -> None:
    """A maintained project does not ship a suite that is ninety percent red.

    When it looks that way from inside our container, the container is what is
    wrong — and saying so keeps the count of repositories that arrived broken
    from being padded with our own failures.
    """
    _patch_baseline(patched, collected=20, failing=18)
    job = run()
    assert job.outcome is Outcome.UNBUILDABLE
    assert "the environment is wrong, not the repository" in job.notes


def test_a_single_pre_existing_failure_does_not_disqualify_a_repository(
    patched: pytest.MonkeyPatch,
) -> None:
    """flask-jwt-extended: 106 passing, one failing on a crypto backend this
    image lacks. The old rule discarded it; the break is what the upgrade
    changed, and that test was red before we arrived."""
    _patch_baseline(patched, collected=107, failing=1)
    job = run()
    assert job.outcome is Outcome.PATCHED_CLEAN, "the upgrade broke nothing new"


def _patch_baseline(
    monkeypatch: pytest.MonkeyPatch, *, collected: int, failing: int
) -> None:
    """Every suite run returns the same report: `failing` of `collected` red.

    The upgrade changes nothing, so a repository that gets past the baseline
    gate reaches PATCHED_CLEAN — which is exactly what "the break is what
    changed" should produce when nothing changed.
    """
    red = frozenset(f"tests/test_{n}.py::test_{n}" for n in range(failing))

    def fake(sandbox: object, **kwargs: object) -> TestReport:
        return TestReport(
            passed=not red,
            output="x",
            duration_seconds=0.1,
            tests_collected=collected,
            failures=red,
        )

    monkeypatch.setattr(worker, "run_tests", fake)
    monkeypatch.setattr("services.worker.repair.run_tests", fake)


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


class _Message:
    """The two methods the consumer is allowed to call on a Pub/Sub message."""

    def __init__(self, job: RepoJob | None = None, raw: bytes | None = None) -> None:
        self.data = raw if raw is not None else json.dumps(job.to_dict()).encode()  # type: ignore[union-attr]
        self.acked = False
        self.nacked = False

    def ack(self) -> None:
        self.acked = True

    def nack(self) -> None:
        self.nacked = True


def _drain(
    monkeypatch: pytest.MonkeyPatch, message: _Message, finished: RepoJob | None
) -> None:
    """Run the queueing decision against one message.

    `on_message` is a module-level function precisely so this needs no live
    subscription: whether a message comes back is the only decision here worth
    getting wrong, and a test that could only reach it through a Pub/Sub client
    would not be run.
    """
    def fake_handle(job: RepoJob, store: object, settings: object, **kwargs: object) -> RepoJob:
        assert finished is not None
        return finished

    monkeypatch.setattr(worker, "handle", fake_handle)
    worker.on_message(message, MemoryJobStore(), Settings(gcp_project="p", fork_org="org"))



def test_a_terminal_outcome_is_acknowledged_even_when_it_is_a_bad_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNBUILDABLE is an answer. Redelivering it buys another fifteen minutes
    of the same, and the message would circulate until the retention window
    ended — which is a queue slowly filling with repositories we already know
    we cannot help."""
    message = _Message(RepoJob(job_id="r1:org/app", repo="org/app", vulnerabilities=[]))
    finished = RepoJob(job_id="r1:org/app", repo="org/app", vulnerabilities=[])
    finished.finish(Outcome.UNBUILDABLE)

    _drain(monkeypatch, message, finished)

    assert message.acked and not message.nacked


def test_our_own_failure_goes_back_on_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """INFRA_ERROR is the one member of Outcome that is about us.

    Acknowledging it would throw away a repository for a reason that had nothing
    to do with the repository.
    """
    message = _Message(RepoJob(job_id="r1:org/app", repo="org/app", vulnerabilities=[]))
    finished = RepoJob(job_id="r1:org/app", repo="org/app", vulnerabilities=[])
    finished.finish(Outcome.INFRA_ERROR)

    _drain(monkeypatch, message, finished)

    assert message.nacked and not message.acked


def test_an_unreadable_message_is_not_redelivered_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It cannot be delivered to anyone, so returning it only moves the problem."""
    message = _Message(None, raw=b"{not json")

    _drain(monkeypatch, message, None)

    assert message.acked


def test_a_model_nobody_could_reach_is_not_a_failed_repair(
    patched: pytest.MonkeyPatch,
) -> None:
    """The first benchmark run reported REPAIR_EXHAUSTED having called nothing.

    Application Default Credentials were absent, ADK logged the failure on a
    worker thread and handed back an empty event stream four times, and the loop
    charged four attempts for work nobody did. REPAIR_EXHAUSTED means the agent
    tried and could not fix it — it is the denominator of the number this
    project publishes — so awarding it here inflates the failures with jobs that
    were never attempted. The tell was in the same log line: zero tokens.
    """
    from services.worker.agent import ModelUnreachable

    patch_suite(patched, [True, False])

    def unreachable(settings: Settings) -> object:
        class Agent:
            def attempt(self, context: object, tools: object) -> RepairProposal:
                raise ModelUnreachable("DefaultCredentialsError: no ADC")

        return Agent()

    patched.setattr(worker, "build_repair_agent", unreachable)

    finished = run()

    assert finished.outcome is Outcome.INFRA_ERROR
    assert "model unreachable" in finished.notes
    assert finished.repair_attempts == [], "an attempt nobody made is not an attempt"


def test_a_real_failed_repair_is_still_repair_exhausted(
    patched: pytest.MonkeyPatch,
) -> None:
    """The other half: an agent that answers and gets it wrong must still count.

    Routing genuine failures to INFRA_ERROR would flatter the repair rate by
    quietly dropping every case the agent lost.
    """
    patch_suite(patched, [True, False, False, False, False, False, False, False])

    def wrong(settings: Settings) -> object:
        class Agent:
            def attempt(self, context: object, tools: object) -> RepairProposal:
                return RepairProposal(rationale="tried renaming the import", tokens_used=1200)

        return Agent()

    patched.setattr(worker, "build_repair_agent", wrong)

    finished = run()

    assert finished.outcome is Outcome.REPAIR_EXHAUSTED
    assert finished.tokens_used > 0, "a real exhaustion cannot cost nothing"


def test_a_worker_that_cannot_reach_a_librarian_still_repairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the write path costs the fleet tomorrow's shortcut. Losing the
    repair costs it tonight's pull request, and only one of those is worth
    failing a job over — so a Librarian that cannot be built is None, not a
    raise."""
    def refuse(settings: Settings) -> object:
        raise RuntimeError("no ADK installed")

    monkeypatch.setattr(worker, "build_librarian", refuse)

    assert worker._librarian(SETTINGS) is None
