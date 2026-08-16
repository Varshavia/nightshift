"""The probe's arithmetic and its output shape.

The probe itself shells out to git and pip, so what is tested here is everything
around that: the verdict taxonomy, the aggregation, and the case file the
benchmark runner will later consume. If the break rate is computed wrongly, the
project's central claim is wrong, so it gets its own tests.
"""

from __future__ import annotations

from scripts.probe_fleet import (
    ProbeResult,
    ProbeVerdict,
    benchmark_cases,
    summarise,
)


def _result(verdict: ProbeVerdict, repo: str = "a/b", **kwargs: object) -> ProbeResult:
    return ProbeResult(repo=repo, verdict=verdict, **kwargs)  # type: ignore[arg-type]


def test_break_rate_is_over_applied_upgrades_not_over_the_fleet() -> None:
    """Repositories that were never upgraded are not evidence either way.

    Including UNBUILDABLE and NOT_AFFECTED in the denominator would make
    upgrades look far safer than they are, which is the opposite of the mistake
    we are usually guarding against — and just as dishonest.
    """
    results = [
        _result(ProbeVerdict.BREAKING),
        _result(ProbeVerdict.BREAKING),
        _result(ProbeVerdict.CLEAN),
        _result(ProbeVerdict.CLEAN),
        _result(ProbeVerdict.UNBUILDABLE),
        _result(ProbeVerdict.NOT_AFFECTED),
        _result(ProbeVerdict.BASELINE_RED),
    ]
    summary = summarise(results)
    assert summary.probed == 7
    assert summary.upgrades_attempted == 4
    assert summary.break_rate == 0.5


def test_an_empty_run_does_not_read_as_upgrades_never_break() -> None:
    summary = summarise([_result(ProbeVerdict.UNBUILDABLE)])
    assert summary.break_rate is None


def test_counts_cover_every_verdict_and_total_the_run() -> None:
    results = [_result(v) for v in ProbeVerdict]
    summary = summarise(results)
    assert set(summary.counts) == {str(v) for v in ProbeVerdict}
    assert sum(summary.counts.values()) == summary.probed == len(ProbeVerdict)


def test_only_breaking_repositories_become_benchmark_cases() -> None:
    results = [
        _result(ProbeVerdict.BREAKING, repo="a/broken", failing_output="E TypeError"),
        _result(ProbeVerdict.CLEAN, repo="a/fine"),
        _result(ProbeVerdict.BASELINE_RED, repo="a/red"),
    ]
    cases = benchmark_cases(results)
    assert [case["repo"] for case in cases] == ["a/broken"]


def test_a_case_carries_what_the_repair_agent_will_need() -> None:
    """The failing output and the exact version transition are the case."""
    result = _result(
        ProbeVerdict.BREAKING,
        repo="a/broken",
        upgrades=("urllib3 1.24.1 -> 1.26.5",),
        advisories=("GHSA-xxxx",),
        failing_output="E   TypeError: unexpected keyword argument 'strict'",
    )
    case = benchmark_cases([result])[0]
    assert case["upgrades"] == ["urllib3 1.24.1 -> 1.26.5"]
    assert case["advisories"] == ["GHSA-xxxx"]
    assert "TypeError" in str(case["failing_output"])
    assert case["verdict"] == "BREAKING"


def test_the_probe_verdicts_are_not_the_job_outcomes() -> None:
    """The probe never repairs, so it must not borrow the repair job's enum.

    Keeping them separate is what preserves the guarantee in ADR 0003 that every
    member of Outcome describes a finished repair job.
    """
    from nightshift_core.models import Outcome

    assert "PATCHED_REPAIRED" not in {str(v) for v in ProbeVerdict}
    assert "BREAKING" not in {str(o) for o in Outcome}
