"""The pull-request body. The only artefact a maintainer actually reads."""

from __future__ import annotations

from services.worker.pull_request import render_pr_body

from nightshift_core.models import RepairAttempt, RepoJob, Severity, Vulnerability


def make_job() -> RepoJob:
    job = RepoJob(
        job_id="run1:nightshift-fleet/example",
        repo="nightshift-fleet/example",
        vulnerabilities=[
            Vulnerability(
                osv_id="GHSA-abcd-1234",
                package="jinja2",
                installed_version="2.11.3",
                fixed_version="3.1.2",
                severity=Severity.HIGH,
                summary="Sandbox escape in Jinja2",
                aliases=("CVE-2024-22195",),
            )
        ],
    )
    job.record_attempt(
        RepairAttempt(
            attempt=1,
            failing_output="ImportError: cannot import name 'Markup' from 'jinja2'",
            diff=(
                "diff --git a/app.py b/app.py\n"
                "-from jinja2 import Markup\n"
                "+from markupsafe import Markup\n"
            ),
            rationale="Jinja2 3.0 removed the top-level Markup re-export.",
            tests_passed=True,
            tokens_used=4200,
        )
    )
    return job


def render(job: RepoJob | None = None) -> str:
    return render_pr_body(
        job or make_job(),
        baseline_green=True,
        test_command="pytest -q",
        model="gemini-3.5-flash",
    )


def test_the_body_names_the_package_and_both_versions() -> None:
    assert "jinja2 2.11.3 → 3.1.2" in render()


def test_the_body_carries_the_advisory_and_its_cve() -> None:
    body = render()
    assert "GHSA-abcd-1234" in body
    assert "CVE-2024-22195" in body
    assert "HIGH" in body


def test_the_body_contains_the_diff_and_the_explanation() -> None:
    body = render()
    assert "+from markupsafe import Markup" in body
    assert "Jinja2 3.0 removed the top-level Markup re-export." in body


def test_the_ai_authorship_disclosure_is_always_present() -> None:
    """Non-negotiable: every pull request discloses that an agent wrote it."""
    assert "written by an AI agent" in render()


def test_the_body_states_the_tests_were_not_modified() -> None:
    assert "not modified" in render()


def test_no_placeholder_survives_rendering() -> None:
    assert "{" not in render(), "an unfilled template field reached the body"


def test_a_vulnerability_without_a_cve_renders_cleanly() -> None:
    job = make_job()
    job.vulnerabilities = [
        Vulnerability(
            osv_id="PYSEC-2021-1",
            package="pyyaml",
            installed_version="5.3",
            fixed_version="6.0",
            severity=Severity.MODERATE,
            summary="Arbitrary code execution",
        )
    ]
    body = render(job)
    assert "PYSEC-2021-1" in body
    assert "CVE-" not in body


def test_an_advisory_with_no_summary_does_not_render_an_empty_quote() -> None:
    job = make_job()
    job.vulnerabilities = [
        Vulnerability(
            osv_id="GHSA-x", package="jinja2", installed_version="2.11.3",
            fixed_version="3.1.2", severity=Severity.HIGH,
        )
    ]
    assert "No summary published." in render(job)
