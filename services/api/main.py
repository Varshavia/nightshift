"""Read model and approval queue for the control tower.

Deliberately thin and read-mostly. The dashboard is a fleet control tower, not
a chat UI: it answers "what happened last night, and what needs a human" and
nothing else. The one write it exposes is an approval — a human deciding that a
particular repository's pull request may go upstream — because that decision
must be a person's, recorded, and revocable.
"""

from __future__ import annotations

from typing import Any

from nightshift_core.config import get_settings
from nightshift_core.store import FirestoreJobStore, JobStore, outcome_counts


def get_store() -> JobStore:
    settings = get_settings()
    settings.require_cloud()
    return FirestoreJobStore(
        project=settings.gcp_project, database=settings.firestore_database
    )


def fleet_summary(run_id: str | None = None) -> dict[str, Any]:
    """Outcome counts for a run. The number the whole project reports."""
    store = get_store()
    counts = outcome_counts(store, run_id=run_id)
    attempted = sum(v for k, v in counts.items() if k != "IN_FLIGHT")
    repaired = counts["PATCHED_REPAIRED"]
    broke = repaired + counts["REPAIR_EXHAUSTED"]
    return {
        "run_id": run_id,
        "counts": counts,
        "attempted": attempted,
        # The honest denominator: of the upgrades that actually broke something,
        # how many did the agent fix? Dividing by the whole fleet would flatter us.
        "repair_rate": (repaired / broke) if broke else None,
    }


def list_jobs(run_id: str | None = None) -> list[dict[str, Any]]:
    return [job.to_dict() for job in get_store().list_jobs(run_id=run_id)]


def approve_upstream(repo: str, approver: str) -> dict[str, Any]:
    """Record a human's decision to allow one repository's PR upstream.

    Per-repository, human-reviewed and disclosed — see RESPONSIBLE_USE.md. There
    is no bulk approve and there will not be one.
    """
    raise NotImplementedError("api: approve_upstream")


def create_app() -> Any:
    """FastAPI application. Wired in Block 1 alongside the dashboard skeleton."""
    raise NotImplementedError("api: create_app")
