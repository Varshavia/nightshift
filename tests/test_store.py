"""Stores. The Firestore one is checked for the property that matters most in
CI: importing it must not require credentials."""

from __future__ import annotations

from nightshift_core.models import Outcome, RepoJob
from nightshift_core.store import FirestoreJobStore, JobStore, MemoryJobStore, outcome_counts


def test_memory_store_satisfies_the_protocol() -> None:
    assert isinstance(MemoryJobStore(), JobStore)


def test_put_then_get_round_trips() -> None:
    store = MemoryJobStore()
    job = RepoJob(job_id="run-1:a/b", repo="a/b")
    store.put(job)
    fetched = store.get("run-1:a/b")
    assert fetched is not None and fetched.repo == "a/b"
    assert store.get("missing") is None


def test_outcome_counts_add_up_to_the_size_of_the_fleet() -> None:
    store = MemoryJobStore()
    for index, outcome in enumerate([Outcome.BASELINE_RED, Outcome.UNBUILDABLE, None]):
        job = RepoJob(job_id=f"run-1:a/b{index}", repo=f"a/b{index}")
        if outcome is not None:
            job.finish(outcome)
        store.put(job)

    counts = outcome_counts(store)
    assert counts["BASELINE_RED"] == 1
    assert counts["UNBUILDABLE"] == 1
    assert counts["IN_FLIGHT"] == 1
    assert sum(counts.values()) == 3


def test_firestore_store_constructs_without_credentials() -> None:
    """The client is lazy on purpose: the domain stays runnable on a laptop."""
    store = FirestoreJobStore(project="nightshift-test")
    assert store._client is None
