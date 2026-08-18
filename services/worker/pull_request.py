"""Rendering and opening the pull request.

The body is rendered by a pure function so that a formatting mistake is caught
by a unit test rather than by a maintainer reading a broken pull request. The
AI-authorship disclosure lives in the template and is asserted in the tests — it
is not something a future refactor gets to drop quietly.
"""

from __future__ import annotations

from pathlib import Path

from nightshift_core.models import RepoJob
from services.worker.toolchain import diff_stats

__all__ = ["PR_TEMPLATE_PATH", "render_pr_body"]

PR_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "pr_body.md"

#: How much of the failing output goes into the body. The tail carries the
#: traceback; the head is collection noise nobody needs in a pull request.
EXCERPT_CHARS = 2000


def render_pr_body(
    job: RepoJob,
    *,
    baseline_green: bool,
    test_command: str,
    model: str,
    max_attempts: int | None = None,
    template: str | None = None,
) -> str:
    """Fill ``templates/pr_body.md`` from a finished job."""
    text = template if template is not None else PR_TEMPLATE_PATH.read_text(encoding="utf-8")

    vulnerability = job.vulnerabilities[0]
    attempts = job.repair_attempts
    last = attempts[-1] if attempts else None
    diff = last.diff if last else ""
    stats = diff_stats(diff)
    excerpt = (last.failing_output if last else "")[-EXCERPT_CHARS:]
    cve = vulnerability.cve
    run_id, _, _ = job.job_id.partition(":")

    return text.format(
        package=vulnerability.package,
        from_version=vulnerability.installed_version,
        to_version=vulnerability.fixed_version or "unknown",
        advisory_id=vulnerability.osv_id,
        cve_suffix=f" ({cve})" if cve else "",
        severity=str(vulnerability.severity),
        advisory_summary=vulnerability.summary or "No summary published.",
        failing_test_count=stats.files,
        failing_excerpt=excerpt,
        repair_explanation=last.rationale if last else "",
        changed_file_count=stats.files,
        added_lines=stats.added,
        removed_lines=stats.removed,
        repair_diff=diff,
        baseline_status="passing" if baseline_green else "failing",
        final_status="passing",
        attempts=len(attempts),
        max_attempts=max_attempts if max_attempts is not None else len(attempts),
        test_command=test_command,
        run_id=run_id,
        job_id=job.job_id,
        model=model,
    )
