"""The domain vocabulary, especially the parts that keep the numbers honest."""

from __future__ import annotations

import pytest

from nightshift_core.models import (
    Dependency,
    Outcome,
    Phase,
    RepairAttempt,
    RepoJob,
    Severity,
    Vulnerability,
    summarise,
)


def _vuln(**overrides: object) -> Vulnerability:
    base: dict[str, object] = {
        "osv_id": "GHSA-xxxx",
        "package": "requests",
        "installed_version": "2.19.0",
        "fixed_version": "2.20.0",
        "severity": Severity.HIGH,
    }
    base.update(overrides)
    return Vulnerability(**base)  # type: ignore[arg-type]


def _job(**overrides: object) -> RepoJob:
    base: dict[str, object] = {"job_id": "run-1:pallets/flask", "repo": "pallets/flask"}
    base.update(overrides)
    return RepoJob(**base)  # type: ignore[arg-type]


def test_repo_must_look_like_owner_name() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        RepoJob(job_id="j", repo="not-a-repo")


def test_dependency_must_be_pinned() -> None:
    with pytest.raises(ValueError, match="pinned"):
        Dependency(name="requests", version="")


def test_an_advisory_without_a_fix_is_not_actionable() -> None:
    assert _vuln().actionable
    assert not _vuln(fixed_version=None).actionable


def test_cve_is_read_out_of_the_aliases() -> None:
    assert _vuln(aliases=("CVE-2018-18074", "PYSEC-2018-28")).cve == "CVE-2018-18074"
    assert _vuln(aliases=("PYSEC-2018-28",)).cve is None


def test_highest_severity_drives_triage_order() -> None:
    job = _job(vulnerabilities=[_vuln(severity=Severity.LOW), _vuln(severity=Severity.CRITICAL)])
    assert job.highest_severity is Severity.CRITICAL


def test_a_finished_job_cannot_be_advanced() -> None:
    job = _job()
    job.finish(Outcome.BASELINE_RED)
    with pytest.raises(ValueError, match="already finished"):
        job.advance(Phase.REPAIR)


def test_a_job_cannot_change_its_mind_about_how_it_ended() -> None:
    """Two workers racing on one job must not quietly overwrite the result."""
    job = _job()
    job.finish(Outcome.UNBUILDABLE)
    with pytest.raises(ValueError, match="refusing"):
        job.finish(Outcome.PATCHED_CLEAN, pr_url="https://github.com/x/y/pull/1")


def test_success_requires_a_pull_request_to_point_at() -> None:
    """A claimed patch with no PR is exactly the claim this project distrusts."""
    with pytest.raises(ValueError, match="pull request"):
        _job().finish(Outcome.PATCHED_REPAIRED)


def test_repair_attempts_accumulate_tokens() -> None:
    job = _job()
    job.record_attempt(RepairAttempt(attempt=1, failing_output="E   TypeError", tokens_used=1200))
    job.record_attempt(RepairAttempt(attempt=2, failing_output="E   TypeError", tokens_used=800))
    assert job.tokens_used == 2000
    assert job.required_repair


def test_round_trip_through_a_dict_preserves_everything() -> None:
    job = _job(vulnerabilities=[_vuln(aliases=("CVE-2018-18074",))])
    job.advance(Phase.REPAIR)
    job.record_attempt(RepairAttempt(attempt=1, failing_output="boom", diff="--- a", tokens_used=5))
    job.finish(Outcome.PATCHED_REPAIRED, pr_url="https://github.com/x/y/pull/1")

    restored = RepoJob.from_dict(job.to_dict())

    assert restored.to_dict() == job.to_dict()
    assert restored.outcome is Outcome.PATCHED_REPAIRED
    assert restored.vulnerabilities[0].cve == "CVE-2018-18074"
    assert restored.repair_attempts[0].diff == "--- a"


def test_summarise_counts_unfinished_jobs_rather_than_dropping_them() -> None:
    done = _job()
    done.finish(Outcome.BASELINE_RED)
    counts = summarise([done, _job(job_id="run-1:a/b", repo="a/b")])
    assert counts["BASELINE_RED"] == 1
    assert counts["IN_FLIGHT"] == 1
    assert sum(counts.values()) == 2


def test_every_outcome_is_terminal_and_none_of_them_is_an_exception() -> None:
    """The closed enum is what makes the repair rate a number, not a claim."""
    assert {"UNBUILDABLE", "BASELINE_RED"} <= {str(o) for o in Outcome}
    for outcome in Outcome:
        job = _job(pr_url="https://github.com/x/y/pull/1")
        job.finish(outcome)
        assert job.finished and job.phase is Phase.DONE
