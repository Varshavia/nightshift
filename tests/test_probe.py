"""The probe's arithmetic and its output shape.

The probe itself shells out to git and pip, so what is tested here is everything
around that: the verdict taxonomy, the aggregation, and the case file the
benchmark runner will later consume. If the break rate is computed wrongly, the
project's central claim is wrong, so it gets its own tests.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from scripts import probe_fleet
from scripts.probe_fleet import (
    ProbeResult,
    ProbeVerdict,
    benchmark_cases,
    summarise,
)

from nightshift_core.fleet import FleetEntry, FleetPool, save_pool


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


def test_pytests_own_failures_are_not_blamed_on_the_repository() -> None:
    """Exit 3 and 4 mean we invoked pytest wrongly, not that the suite is red.

    Folding them into BASELINE_RED would inflate the count of repositories that
    arrived broken and quietly excuse our own build bugs.
    """
    summary = summarise([_result(ProbeVerdict.PROBE_ERROR), _result(ProbeVerdict.BASELINE_RED)])
    assert summary.counts["PROBE_ERROR"] == 1
    assert summary.counts["BASELINE_RED"] == 1
    assert summary.upgrades_attempted == 0


def test_an_osv_outage_is_not_recorded_as_an_unbuildable_repository() -> None:
    """Our network failing says nothing about the repository we were probing.

    Filing it under UNBUILDABLE would understate how much of the fleet is
    usable, and the fleet-size estimate is what the cloud budget is sized from.
    """
    summary = summarise([_result(ProbeVerdict.PROBE_ERROR)])
    assert summary.counts["UNBUILDABLE"] == 0
    assert summary.counts["PROBE_ERROR"] == 1


def test_the_probe_reads_the_reviewed_pool_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two halves of the pipeline have to meet without a person retyping.

    ``build_fork_pool.py fork`` writes ``fleet/pool.json``; the probe used to
    demand a plain text file, so the list would have been copied out by hand —
    and a hand-copied list is a list nobody reviewed, which is exactly what ADR
    0002 says must not happen.
    """
    pool = FleetPool(entries=(FleetEntry(repo="me/service", upstream="org/service"),))
    save_pool(pool, tmp_path / "pool.json")

    seen: list[Sequence[str]] = []

    def record(repos: Sequence[str]) -> list[ProbeResult]:
        seen.append(repos)
        return []

    monkeypatch.setattr(probe_fleet, "probe_fleet", record)

    probe_fleet.main(
        ["--pool", str(tmp_path / "pool.json"), "--out", str(tmp_path / "cases.json")]
    )

    assert seen == [["me/service"]]


def test_a_missing_pool_says_how_to_build_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = probe_fleet.main(["--pool", str(tmp_path / "absent.json")])

    assert code == 2
    assert "build_fork_pool.py" in capsys.readouterr().err


def test_every_verdict_is_written_down_not_only_the_breaking_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first real run wrote 439 bytes and explained nothing.

    Six repositories failed before an upgrade was ever applied, each with a
    `notes` saying why, and the output file kept none of it because only
    BREAKING results were serialised. A run that finds no cases is the run most
    in need of diagnosis, so it is the run that must record the most.
    """
    monkeypatch.setattr(
        probe_fleet,
        "probe_fleet",
        lambda repos: [
            ProbeResult(repo="a/b", verdict=ProbeVerdict.UNBUILDABLE, notes="no such extra"),
        ],
    )
    out = tmp_path / "cases.json"

    probe_fleet.main(["--repos", str(_repo_file(tmp_path)), "--out", str(out)])

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["cases"] == []
    assert written["results"][0]["notes"] == "no such extra"


def test_an_unmeasured_break_rate_does_not_read_as_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        probe_fleet,
        "probe_fleet",
        lambda repos: [ProbeResult(repo="a/b", verdict=ProbeVerdict.BASELINE_RED)],
    )

    probe_fleet.main(
        ["--repos", str(_repo_file(tmp_path)), "--out", str(tmp_path / "cases.json")]
    )

    assert "unmeasured rather than zero" in capsys.readouterr().out


def _repo_file(tmp_path: Path) -> Path:
    target = tmp_path / "repos.txt"
    target.write_text("a/b\n", encoding="utf-8")
    return target
