"""The Tier A runner: what it scores, and what it refuses to score.

The runner calls the production pipeline, so nothing here exercises a repair —
that needs a model and a network. What is tested is the arithmetic around it,
which is where a benchmark goes wrong quietly: a denominator that includes cases
the agent never saw turns a run of failures into a respectable percentage.

The case directories themselves are checked in test_benchmark_cases.py.
"""

from __future__ import annotations

import pytest
from scripts.run_benchmark import CaseResult, job_for, summarise

from nightshift_core.models import Outcome


def _result(outcome: Outcome | str, case_id: str = "c", **kwargs: object) -> CaseResult:
    defaults: dict[str, object] = {
        "case_id": case_id,
        "repo": f"org/nightshift-case-{case_id}",
        "outcome": str(outcome),
        "attempts": 0,
        "tokens": 0,
        "cost_usd": 0.0,
        "pr_url": None,
        "notes": "",
    }
    return CaseResult(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_the_rate_is_over_the_cases_that_actually_broke() -> None:
    """A case that would not build says nothing about the agent either way.

    Putting it in the denominator would let a bad container drag the score down,
    and putting it in the numerator would let one prop the score up. It belongs
    in neither, which is the same rule the fleet-wide probe follows.
    """
    summary = summarise(
        [
            _result(Outcome.PATCHED_REPAIRED, "a"),
            _result(Outcome.REPAIR_EXHAUSTED, "b"),
            _result(Outcome.UNBUILDABLE, "c"),
            _result(Outcome.BASELINE_RED, "d"),
        ]
    )
    assert summary["cases"] == 4
    assert summary["scored"] == 2
    assert summary["repair_rate"] == 0.5


def test_an_upgrade_that_broke_nothing_is_not_a_repair() -> None:
    """PATCHED_CLEAN is a fine result and it is not evidence of repair.

    A Tier A case that comes back clean is a broken fixture — the whole point of
    the case is that the upgrade breaks the suite — so counting it as a success
    would hide exactly the failure worth knowing about.
    """
    summary = summarise([_result(Outcome.PATCHED_CLEAN, "a")])
    assert summary["scored"] == 0
    assert summary["repair_rate"] is None


def test_a_run_with_nothing_scorable_does_not_read_as_zero_percent() -> None:
    """Zero percent is a measurement. This is the absence of one."""
    assert summarise([_result(Outcome.UNBUILDABLE)])["repair_rate"] is None
    assert summarise([])["repair_rate"] is None


def test_the_job_carries_the_transition_the_case_file_names() -> None:
    """The case is the version transition, not whatever OSV says this week.

    Asking OSV at run time would mean a fixture silently stops being a test the
    day an advisory is withdrawn or its fixed version moves.
    """
    case = {"id": "x", "package": "jinja2", "from_version": "2.11.3", "to_version": "3.1.2"}
    job = job_for(case, "org/nightshift-case-x")

    vulnerability = job.vulnerabilities[0]
    assert (vulnerability.installed_version, vulnerability.fixed_version) == ("2.11.3", "3.1.2")
    assert vulnerability.actionable


def test_a_case_missing_its_transition_fails_loudly() -> None:
    """Silently defaulting a version would run the pipeline against nothing and
    report the result as though a measurement had been taken."""
    with pytest.raises(ValueError, match="to_version"):
        job_for({"id": "x", "package": "jinja2", "from_version": "2.11.3"}, "org/x")
