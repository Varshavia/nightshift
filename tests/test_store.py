"""Stores. The Firestore one is checked for the property that matters most in
CI: importing it must not require credentials."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nightshift_core.models import Outcome, Phase, RepoJob
from nightshift_core.store import (
    ABANDONED_AFTER,
    FirestoreJobStore,
    JobStore,
    MemoryJobStore,
    document_id,
    is_abandoned,
    outcome_counts,
)


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


def test_a_job_id_containing_a_repository_is_a_usable_document_id() -> None:
    """Firestore reads `/` as a path separator, and a job id contains one.

    `run1:Varshavia/throttled` is not a document id — it is three path elements,
    and the client refuses it outright. The identifier has carried a repository
    name since the first commit and this only surfaced the first night anything
    wrote a job to Firestore, because every earlier write went to the in-memory
    store, where a slash is just a character.
    """
    assert "/" not in document_id("run1:Varshavia/throttled")
    assert document_id("run1:Varshavia/throttled") == "run1:Varshavia__throttled"


def test_two_different_repositories_never_collide() -> None:
    """The rewrite has to stay injective or one repository overwrites another."""
    assert document_id("run1:org/a") != document_id("run1:org/b")
    assert document_id("run1:org-a/x") != document_id("run1:org/a-x")


def test_an_identifier_with_no_slash_is_left_alone() -> None:
    assert document_id("run1") == "run1"


def _stopped_at(age: timedelta, **kwargs: object) -> RepoJob:
    return RepoJob(
        job_id="run-1:a/b",
        repo="a/b",
        updated_at=datetime.now(UTC) - age,
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_job_nothing_is_working_on_stops_counting_as_in_flight() -> None:
    """Seventeen repositories showed CLONING with no worker anywhere near them.

    Each was a record left behind by a container Cloud Run killed for using too
    much memory. Nothing writes to such a record again, so "in flight" is a
    claim about the present that stopped being true the moment the task died.
    """
    assert is_abandoned(_stopped_at(ABANDONED_AFTER + timedelta(minutes=1), phase=Phase.CLONING))


def test_a_job_still_within_the_ceiling_is_left_alone() -> None:
    """Building a large project legitimately takes a long time, and calling that
    abandoned sends someone hunting a bug that is not there."""
    assert not is_abandoned(_stopped_at(timedelta(minutes=5), phase=Phase.CLONING))


def test_a_finished_job_is_never_abandoned_however_old() -> None:
    """The record stops being written to precisely because it is done."""
    job = _stopped_at(timedelta(days=30), outcome=Outcome.PATCHED_CLEAN)
    assert not is_abandoned(job)


def test_a_stalled_job_is_neither_in_flight_nor_an_outcome() -> None:
    """Folding it into either number tells a different lie: busy, or finished."""
    store = MemoryJobStore()
    store.put(_stopped_at(ABANDONED_AFTER + timedelta(minutes=1)))
    store.put(RepoJob(job_id="run-1:c/d", repo="c/d"))
    store.put(RepoJob(job_id="run-1:e/f", repo="e/f", outcome=Outcome.BASELINE_RED))

    counts = outcome_counts(store)

    assert counts["ABANDONED"] == 1
    assert counts["IN_FLIGHT"] == 1
    assert counts["BASELINE_RED"] == 1


def test_abandoned_is_not_smuggled_into_the_outcome_enum() -> None:
    """ADR 0003: every member of Outcome describes a finished repair job, and an
    abandoned job finished nothing. It is a fact about the clock, not a verdict."""
    assert "ABANDONED" not in {str(outcome) for outcome in Outcome}
