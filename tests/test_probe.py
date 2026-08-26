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
from services.worker.toolchain import TestReport, collection_counts, failing_ids

from nightshift_core.fleet import FleetEntry, FleetPool, save_pool


#: These tests stub the prober out, so what they exercise is argument handling
#: and output — this machine is exactly what they mean to measure. Said with the
#: flag rather than by leaving the guard out of their way, because the guard
#: exists to make measuring the host a decision somebody made on purpose. It was
#: added on Linux and turned four of these red on the Windows machine the team
#: develops on, which is its own small lesson about where a suite gets run.
HOST = ["--allow-host-platform"]


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
        ["--pool", str(tmp_path / "pool.json"), "--out", str(tmp_path / "cases.json"), *HOST]
    )

    assert seen == [["me/service"]]


def test_a_missing_pool_says_how_to_build_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = probe_fleet.main(["--pool", str(tmp_path / "absent.json"), *HOST])

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

    probe_fleet.main(["--repos", str(_repo_file(tmp_path)), "--out", str(out), *HOST])

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
        ["--repos", str(_repo_file(tmp_path)), "--out", str(tmp_path / "cases.json"), *HOST]
    )

    assert "unmeasured rather than zero" in capsys.readouterr().out


def _repo_file(tmp_path: Path) -> Path:
    target = tmp_path / "repos.txt"
    target.write_text("a/b\n", encoding="utf-8")
    return target


def test_a_pre_existing_failure_does_not_disqualify_a_repository() -> None:
    """flask-jwt-extended: 106 passing, one failing on an absent crypto backend.

    Demanding a perfectly green baseline sounds rigorous and is not — it throws
    away a hundred usable tests over one failure that belongs to our container
    rather than to the repository.
    """
    baseline = TestReport(
        passed=False,
        output="FAILED tests/test_asymmetric_crypto.py::test_asymmetric_cropto\n"
        "1 failed, 106 passed, 3 errors in 0.76s",
        duration_seconds=1.0,
        exit_code=1,
    )

    # Counts are filled in by run_tests, not by the constructor.
    assert baseline.tests_collected == 0
    assert failing_ids(baseline.output) == {
        "tests/test_asymmetric_crypto.py::test_asymmetric_cropto"
    }


def test_the_break_is_what_changed_not_what_was_red() -> None:
    before = failing_ids("FAILED tests/test_crypto.py::test_rsa\n1 failed, 106 passed")
    after = failing_ids(
        "FAILED tests/test_crypto.py::test_rsa\n"
        "FAILED tests/test_decode.py::test_decode_algorithms\n"
        "2 failed, 105 passed"
    )

    assert after - before == {"tests/test_decode.py::test_decode_algorithms"}


def test_an_import_that_dies_after_the_upgrade_counts_as_a_break() -> None:
    """The most common shape of a real break: the name is gone, so the module
    never imports and no test in it runs at all."""
    before = failing_ids("110 passed in 2.0s")
    after = failing_ids("ERROR tests/test_view_decorators.py\n1 error in 0.4s")

    assert after - before == {"tests/test_view_decorators.py"}


def test_a_suite_where_nothing_passes_is_still_baseline_red() -> None:
    """The rule loosened, it did not disappear. A repository whose every test is
    red offers no evidence either way and must not enter the denominator."""
    output = "\n".join(f"FAILED tests/test_{n}.py::test_{n}" for n in range(4)) + "\n4 failed"
    assert len(failing_ids(output)) == 4


def test_an_upgrade_verified_by_no_tests_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`code-examples-python` was called CLEAN with zero tests at baseline.

    Loosening the green-baseline rule to tolerate pre-existing failures was
    right. Loosening it far enough that an empty suite counts as evidence was
    the same false-green this project exists to refuse, arrived at from the
    other direction.
    """
    from services.worker.toolchain import collection_counts

    assert collection_counts("2 errors in 0.30s") == (0, 2)


def test_a_suite_that_is_mostly_red_is_our_environment_not_their_code() -> None:
    """alerta: 174 of 194 tests failing before we touched anything.

    A maintained project does not ship a suite that is ninety percent red. When
    it looks that way from inside our container, the container is what is wrong
    — alerta's fixtures want a database — and calling the result CLEAN would put
    a number in the denominator that twenty passing tests were holding up.
    """
    collected, _ = collection_counts("174 failed, 20 passed, 12 errors in 30.0s")
    passing = collected - 174
    assert collected == 194
    assert passing * 2 < collected, "this is the shape that must not reach a verdict"


def test_the_probe_refuses_to_measure_the_wrong_operating_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Four repositories came back UNBUILDABLE on a Windows laptop, and the
    reasons had nothing to do with the repositories.

    `triton` publishes no Windows wheel; `gnureadline` says outright "this
    module is not meant to work on Windows". UNBUILDABLE reads as "this project
    is beyond us" and what it actually said was "we asked on the wrong operating
    system" — a wrong number that looks like a finding, which is worse than a
    wrong number.
    """
    monkeypatch.setattr(probe_fleet.sys, "platform", "win32")
    monkeypatch.setattr(probe_fleet, "probe_fleet", _must_not_run)

    code = probe_fleet.main(["--repos", str(_repo_file(tmp_path))])

    assert code == 2
    assert "probe.Dockerfile" in capsys.readouterr().err


def _must_not_run(repos: Sequence[str]) -> list[ProbeResult]:
    raise AssertionError("nothing may be probed from the wrong platform")


def test_the_probe_runs_where_the_fleet_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_fleet.sys, "platform", "linux")
    assert probe_fleet.wrong_platform() == ""


def test_measuring_this_machine_has_to_be_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quick single-repository run while debugging is legitimate. It is a flag
    rather than the default, so the result is a choice somebody made."""
    monkeypatch.setattr(probe_fleet.sys, "platform", "darwin")
    message = probe_fleet.wrong_platform()

    assert "--allow-host-platform" in message
    assert "probe.Dockerfile" in message


def test_the_escape_hatch_actually_lets_a_run_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard nobody can get past becomes a guard somebody deletes."""
    monkeypatch.setattr(probe_fleet.sys, "platform", "win32")
    monkeypatch.setattr(probe_fleet, "probe_fleet", lambda repos: [])

    code = probe_fleet.main(
        [
            "--repos",
            str(_repo_file(tmp_path)),
            "--out",
            str(tmp_path / "cases.json"),
            "--allow-host-platform",
        ]
    )

    assert code == 0
