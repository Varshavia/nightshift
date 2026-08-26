"""The control tower: what it serves, and what it refuses.

The dashboard is the hosted face of the project, so two things get tested here
above all — that a health probe never depends on a database, and that text
originating in somebody else's repository cannot become markup in our page.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from services.api import main as api

from nightshift_core.config import Settings, get_settings
from nightshift_core.store import Approval, MemoryApprovalStore


@pytest.fixture(autouse=True)
def _no_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in these tests may reach Firestore."""
    monkeypatch.setattr(api, "get_store", lambda: _RaisingStore())
    monkeypatch.setattr(api, "get_approvals", lambda: MemoryApprovalStore())
    get_settings.cache_clear()


class _RaisingStore:
    def list_jobs(self, *, run_id: str | None = None) -> list[Any]:
        raise AssertionError("the store must not be touched in this test")


def test_the_health_probe_never_touches_the_database() -> None:
    """Cloud Run probes the container before a revision goes live.

    A probe that reached Firestore would turn a slow database into a failed
    deployment, which is the sort of coupling that only shows itself at the
    worst possible moment.
    """
    response = TestClient(api.create_app()).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_test_output_from_another_repository_cannot_become_markup() -> None:
    """The path an injected script would actually take.

    Repository names come from a reviewed pool and phases from our own enum, but
    a job's notes carry failing test output, and that output originates inside
    somebody else's repository — which the agent clones and runs.
    """
    job = {
        "repo": "<script>alert('xss')</script>",
        "outcome": "PATCHED_CLEAN",
        "phase": "DONE",
        "tokens_used": 10,
        "pr_url": "",
    }

    page = api.render_dashboard({"counts": {}, "attempted": 0, "repair_rate": None}, [job])

    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page


def test_only_a_github_url_is_ever_rendered_as_a_link() -> None:
    """A URL out of the store going straight into an href is a redirect waiting
    to happen, so anything that is not one of our pull requests is not a link."""
    assert "href" not in api._pr_cell({"pr_url": "javascript:alert(1)"})
    assert "href" not in api._pr_cell({"pr_url": "https://evil.example/pr/1"})
    assert "href" in api._pr_cell({"pr_url": "https://github.com/org/repo/pull/1"})


def test_an_unmeasured_repair_rate_is_not_shown_as_zero() -> None:
    """Zero percent repaired and nothing to repair yet are different nights."""
    unmeasured = api.render_dashboard({"counts": {}, "attempted": 0, "repair_rate": None}, [])
    measured = api.render_dashboard({"counts": {}, "attempted": 4, "repair_rate": 0.0}, [])

    assert "not yet measured" in unmeasured
    # The headline reads the rate out of the same slot either way, so comparing
    # the two renderings is what actually shows they differ.
    assert ">not yet measured<" in unmeasured
    assert ">0%<" in measured


def test_approving_upstream_records_who_did_it() -> None:
    store = MemoryApprovalStore()
    store.approve(Approval(repo="org/app", approver="suat", note="reviewed the diff"))

    assert store.approved("org/app") is not None
    assert store.approved("org/app").approver == "suat"  # type: ignore[union-attr]


def test_an_approval_without_a_name_is_refused() -> None:
    """An unattributed approval is indistinguishable from no approval."""
    with pytest.raises(ValueError):
        Approval(repo="org/app", approver="   ")


def test_an_approval_is_revocable() -> None:
    """RESPONSIBLE_USE promises recorded *and* revocable."""
    store = MemoryApprovalStore()
    store.approve(Approval(repo="org/app", approver="suat"))
    store.revoke("org/app")

    assert store.approved("org/app") is None


def test_the_write_endpoint_refuses_without_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads are public so a reviewer needs no credentials; the one write is not.

    Sending a pull request upstream puts our output in front of somebody else's
    project, which is a decision that needs a key behind it.
    """
    monkeypatch.setattr(api, "get_settings", lambda: Settings(approval_key="s3cret"))
    client = TestClient(api.create_app())

    refused = client.post("/api/approvals", params={"repo": "org/app", "approver": "suat"})
    assert refused.status_code == 403

    allowed = client.post(
        "/api/approvals",
        params={"repo": "org/app", "approver": "suat"},
        headers={"x-nightshift-approval-key": "s3cret"},
    )
    assert allowed.status_code == 200


def test_a_deployment_with_no_key_configured_refuses_every_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty key must mean "nobody", not "everybody" — the failure mode of a
    check that compares two empty strings and finds them equal."""
    monkeypatch.setattr(api, "get_settings", lambda: Settings(approval_key=""))
    client = TestClient(api.create_app())

    response = client.post(
        "/api/approvals",
        params={"repo": "org/app", "approver": "suat"},
        headers={"x-nightshift-approval-key": ""},
    )
    assert response.status_code == 403


def test_a_stalled_job_is_not_rendered_as_work_in_progress() -> None:
    """The page said CLONING about seventeen repositories nothing was cloning.

    The phase is still true — it is where the job stopped — so it stays. What
    was false was the badge beside it, which claimed the fleet was busy with
    work its containers had already been killed for.
    """
    row = api._row({"repo": "a/b", "phase": "CLONING", "state": "ABANDONED"})

    assert "ABANDONED" in row
    assert "IN_FLIGHT" not in row
    assert "CLONING" in row, "where it stopped is worth keeping"


def test_an_unfinished_job_within_the_ceiling_still_reads_as_in_flight() -> None:
    row = api._row({"repo": "a/b", "phase": "CLONING", "state": "IN_FLIGHT"})
    assert "IN_FLIGHT" in row


def test_a_job_nobody_has_started_reads_as_waiting_not_as_a_dropped_one() -> None:
    """Forty queued jobs badged ABANDONED read as forty crashed workers."""
    row = api._row({"repo": "a/b", "phase": "QUEUED", "state": "WAITING"})
    assert "WAITING" in row
    assert "ABANDONED" not in row


def test_a_stalled_job_is_not_counted_as_a_repository_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline number is how many repositories the fleet reached a verdict
    on. A job whose worker was killed reached none, and counting it would be
    taking credit for a container that ran out of memory."""
    from datetime import UTC, datetime, timedelta

    from nightshift_core.models import Outcome, Phase, RepoJob
    from nightshift_core.store import ABANDONED_AFTER, MemoryJobStore

    store = MemoryJobStore()
    store.put(RepoJob(job_id="r:a/b", repo="a/b", outcome=Outcome.BASELINE_RED))
    store.put(RepoJob(job_id="r:c/d", repo="c/d"))
    store.put(
        RepoJob(
            job_id="r:e/f",
            repo="e/f",
            updated_at=datetime.now(UTC) - ABANDONED_AFTER - timedelta(minutes=1),
            phase=Phase.CLONING,
        )
    )
    monkeypatch.setattr(api, "get_store", lambda: store)

    summary = api.fleet_summary()

    assert summary["counts"]["ABANDONED"] == 1
    assert summary["attempted"] == 1, "one verdict reached, not three"


def test_the_stalled_tile_is_not_a_quiet_one() -> None:
    """Muted says "nothing to see". Seventeen dropped jobs is something to see,
    and it is usually a symptom of the infrastructure rather than the fleet. A
    queue waiting for its next worker is the opposite: entirely ordinary."""
    assert api._OUTCOME_TONE["ABANDONED"] == "bad"
    assert api._OUTCOME_TONE["WAITING"] == "muted"
