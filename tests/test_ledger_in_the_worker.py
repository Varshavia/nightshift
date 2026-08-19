"""The Ledger, where it actually earns its keep: inside a job.

The unit tests in ``test_ledger.py`` prove the promotion arithmetic. These prove
the wiring, and the wiring is where the cost curve can quietly become a lie:

- A job that never consulted the Ledger must not appear on the curve as a miss
  it never paid for, nor as a hit it never got.
- A recipe must reach the agent, or the whole mechanism is a very well tested
  filing cabinet nobody opens.
- A Ledger outage must cost full price and nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services.worker import main as worker
from services.worker.agent import render_attempt_prompt
from services.worker.repair import RepairContext, RepairProposal
from services.worker.toolchain import Sandbox, TestReport

from nightshift_core import telemetry
from nightshift_core.config import Ceilings, Settings
from nightshift_core.ledger import (
    InMemoryRecordStore,
    MigrationLedger,
    MigrationScope,
    RecipeStatus,
    RecordBackedRecall,
)
from nightshift_core.models import Outcome, RepoJob, Severity, Vulnerability
from nightshift_core.store import MemoryJobStore
from nightshift_core.telemetry import LEDGER_HIT, OUTCOME, SpanRecorder

SETTINGS = Settings(fork_org="nightshift-fleet", workspace_root="/tmp/nightshift-test")
JINJA = MigrationScope(library="jinja2", from_version="2.11.3", to_version="3.1.2")
FACT = "Jinja2 3.0 removed the top-level Markup re-export. Import it from markupsafe."

VULNERABILITY = Vulnerability(
    osv_id="GHSA-test",
    package="jinja2",
    installed_version="2.11.3",
    fixed_version="3.1.2",
    severity=Severity.HIGH,
)


class RecordingAgent:
    """Repairs, and keeps what it was handed so the prompt can be inspected."""

    def __init__(self, *, repairs: bool = True) -> None:
        self.contexts: list[RepairContext] = []
        self._repairs = repairs

    def attempt(self, context: RepairContext, tools: object) -> RepairProposal:
        self.contexts.append(context)
        return RepairProposal(rationale="fixed the import", tokens_used=100)


def make_job(repo: str = "nightshift-fleet/example") -> RepoJob:
    return RepoJob(job_id=f"run1:{repo}", repo=repo, vulnerabilities=[VULNERABILITY])


def fresh_ledger() -> MigrationLedger:
    records = InMemoryRecordStore()
    return MigrationLedger(recall=RecordBackedRecall(records), records=records)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """Stub the toolchain. The wiring is what is under test, not the sandbox."""
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
    monkeypatch.setattr("services.worker.repair.capture_diff", lambda sandbox: "")
    monkeypatch.setattr("services.worker.repair.upgrade_drift", lambda sandbox, vulns: [])
    return monkeypatch


def _suite(results: list[bool], monkeypatch: pytest.MonkeyPatch) -> None:
    remaining = iter(results)

    def fake(sandbox: object, **kwargs: object) -> TestReport:
        return TestReport(passed=next(remaining), output="ImportError", duration_seconds=0.1)

    monkeypatch.setattr(worker, "run_tests", fake)
    monkeypatch.setattr("services.worker.repair.run_tests", fake)


def _agent(monkeypatch: pytest.MonkeyPatch, agent: RecordingAgent) -> None:
    monkeypatch.setattr(worker, "build_repair_agent", lambda settings: agent)


# --------------------------------------------------------------------------- #
# Which tier answered, and when the question is even asked
# --------------------------------------------------------------------------- #


def test_a_cold_job_is_a_miss_and_gets_no_recipe(patched: pytest.MonkeyPatch) -> None:
    _suite([True, False, True], patched)
    agent = RecordingAgent()
    _agent(patched, agent)

    job = worker.handle(make_job(), MemoryJobStore(), SETTINGS, ledger=fresh_ledger())

    assert job.outcome is Outcome.PATCHED_REPAIRED
    assert job.ledger_hit == "miss"
    assert agent.contexts[0].recipe == ""


def test_a_known_transition_is_an_exact_hit_and_the_recipe_reaches_the_agent(
    patched: pytest.MonkeyPatch,
) -> None:
    ledger = fresh_ledger()
    ledger.learn(JINJA, fact=FACT, break_kind="removed-top-level-name", origin_repo="org/first")
    _suite([True, False, True], patched)
    agent = RecordingAgent()
    _agent(patched, agent)

    job = worker.handle(make_job(), MemoryJobStore(), SETTINGS, ledger=ledger)

    assert job.ledger_hit == "exact"
    assert FACT in agent.contexts[0].recipe


def test_a_clean_upgrade_never_consults_the_ledger(patched: pytest.MonkeyPatch) -> None:
    """It has nothing to look up. A hit recorded here would put a repository on
    the curve that never needed the Ledger at all, flattering every tier."""
    ledger = fresh_ledger()
    ledger.learn(JINJA, fact=FACT, break_kind="k", origin_repo="org/first")
    _suite([True, True], patched)

    job = worker.handle(make_job(), MemoryJobStore(), SETTINGS, ledger=ledger)

    assert job.outcome is Outcome.PATCHED_CLEAN
    assert job.ledger_hit == "miss"
    assert ledger.lookup(JINJA).recipe is not None
    assert ledger.lookup(JINJA).recipe.confirmations == 0  # type: ignore[union-attr]


def test_a_ledger_that_raises_costs_full_price_and_nothing_else(
    patched: pytest.MonkeyPatch,
) -> None:
    """A preview API outage degrades the fleet to cold repair. It must never
    cost a repository its run."""

    class BrokenLedger:
        def lookup(self, scope: MigrationScope) -> object:
            raise RuntimeError("Memory Bank is unavailable")

        def record_outcome(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("still unavailable")

    _suite([True, False, True], patched)
    _agent(patched, RecordingAgent())

    job = worker.handle(make_job(), MemoryJobStore(), SETTINGS, ledger=BrokenLedger())  # type: ignore[arg-type]

    assert job.outcome is Outcome.PATCHED_REPAIRED
    assert job.ledger_hit == "miss"


# --------------------------------------------------------------------------- #
# What gets written back
# --------------------------------------------------------------------------- #


def test_a_repair_after_a_hit_is_recorded_as_evidence(patched: pytest.MonkeyPatch) -> None:
    ledger = fresh_ledger()
    ledger.learn(JINJA, fact=FACT, break_kind="k", origin_repo="org/first")
    _suite([True, False, True], patched)
    _agent(patched, RecordingAgent())

    worker.handle(make_job("org/second"), MemoryJobStore(), SETTINGS, ledger=ledger)

    recipe = ledger.lookup(JINJA).recipe
    assert recipe is not None
    assert recipe.confirmations == 1
    assert recipe.evidence[0].repo == "org/second"
    assert recipe.evidence[0].attempts_used == 1


def test_two_independent_repositories_promote_the_recipe_through_real_jobs(
    patched: pytest.MonkeyPatch,
) -> None:
    """The demo, end to end: cold, then confirmed, then verified prior art."""
    ledger = fresh_ledger()
    ledger.learn(JINJA, fact=FACT, break_kind="k", origin_repo="org/first")

    for repo in ("org/second", "org/third"):
        _suite([True, False, True], patched)
        _agent(patched, RecordingAgent())
        worker.handle(make_job(repo), MemoryJobStore(), SETTINGS, ledger=ledger)

    recipe = ledger.lookup(JINJA).recipe
    assert recipe is not None
    assert recipe.status is RecipeStatus.VERIFIED

    agent = RecordingAgent()
    _suite([True, False, True], patched)
    _agent(patched, agent)
    worker.handle(make_job("org/fourth"), MemoryJobStore(), SETTINGS, ledger=ledger)
    assert "other repositories" in agent.contexts[0].recipe


def test_exhaustion_after_a_hit_is_recorded_but_confirms_nothing(
    patched: pytest.MonkeyPatch,
) -> None:
    ledger = fresh_ledger()
    ledger.learn(JINJA, fact=FACT, break_kind="k", origin_repo="org/first")
    settings = Settings(
        fork_org="nightshift-fleet",
        workspace_root="/tmp/nightshift-test",
        ceilings=Ceilings(max_repair_attempts=1),
    )
    _suite([True, False, False, False], patched)
    _agent(patched, RecordingAgent())

    job = worker.handle(make_job("org/second"), MemoryJobStore(), settings, ledger=ledger)

    assert job.outcome is Outcome.REPAIR_EXHAUSTED
    recipe = ledger.lookup(JINJA).recipe
    assert recipe is not None
    assert recipe.confirmations == 0
    assert recipe.unhelpful_count == 1


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #


def test_the_recipe_comes_after_the_traceback_in_the_prompt() -> None:
    """The agent should read the failure before it is handed a conclusion.

    A recipe placed first anchors it on somebody else's fix, which may not be
    this repository's problem — and a wrong anchor costs an attempt we do not
    get back.
    """
    prompt = render_attempt_prompt(
        RepairContext(
            repo="org/x",
            vulnerabilities=(VULNERABILITY,),
            failing_output="ImportError: cannot import name 'Markup'",
            attempt=1,
            recipe=FACT,
        )
    )
    assert prompt.index("ImportError") < prompt.index(FACT)
    assert "What the fleet already knows" in prompt


def test_a_prompt_with_no_recipe_says_nothing_about_the_fleet() -> None:
    prompt = render_attempt_prompt(
        RepairContext(
            repo="org/x", vulnerabilities=(VULNERABILITY,), failing_output="boom", attempt=1
        )
    )
    assert "What the fleet already knows" not in prompt


# --------------------------------------------------------------------------- #
# The curve
# --------------------------------------------------------------------------- #


def test_the_job_span_carries_what_the_curve_is_computed_from(
    patched: pytest.MonkeyPatch,
) -> None:
    recorder = telemetry.configure(recorder=SpanRecorder())
    assert recorder is not None
    ledger = fresh_ledger()
    ledger.learn(JINJA, fact=FACT, break_kind="k", origin_repo="org/first")
    _suite([True, False, True], patched)
    _agent(patched, RecordingAgent())

    worker.handle(make_job("org/second"), MemoryJobStore(), SETTINGS, ledger=ledger)

    job_spans = recorder.named("job")
    assert job_spans, "every job must produce a job span or it drops out of the curve"
    attributes = job_spans[-1].attributes
    assert attributes[LEDGER_HIT] == "exact"
    assert attributes[OUTCOME] == "PATCHED_REPAIRED"
    assert attributes[telemetry.TOKENS] == 100
