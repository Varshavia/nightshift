"""Job state, checkpointed.

Two implementations behind one protocol. ``MemoryJobStore`` is what the tests
and ``make run-local`` use; ``FirestoreJobStore`` is what runs at night. The
protocol exists so that no service imports a Google Cloud client directly — the
domain stays runnable on a laptop with no credentials, which is the difference
between a test suite that runs and one that is skipped.

Checkpointing is per phase transition rather than per tool call. A worker
killed mid-repair resumes from the last completed phase and re-does at most one
attempt; checkpointing finer would triple the writes to buy back seconds.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable

from nightshift_core.models import Outcome, RepoJob

__all__ = ["FirestoreJobStore", "JobStore", "MemoryJobStore"]

_COLLECTION = "nightshift_jobs"


@runtime_checkable
class JobStore(Protocol):
    """What a worker needs from persistence, and nothing more."""

    def put(self, job: RepoJob) -> None: ...

    def get(self, job_id: str) -> RepoJob | None: ...

    def list_jobs(self, *, run_id: str | None = None) -> list[RepoJob]: ...


class MemoryJobStore:
    """In-process store. Not a mock — the local runner genuinely uses it."""

    def __init__(self) -> None:
        self._jobs: dict[str, RepoJob] = {}

    def put(self, job: RepoJob) -> None:
        self._jobs[job.job_id] = job

    def get(self, job_id: str) -> RepoJob | None:
        return self._jobs.get(job_id)

    def list_jobs(self, *, run_id: str | None = None) -> list[RepoJob]:
        jobs = self._jobs.values()
        if run_id is not None:
            jobs = [job for job in jobs if job.job_id.startswith(f"{run_id}:")]  # type: ignore[assignment]
        return sorted(jobs, key=lambda job: job.created_at)

    def __len__(self) -> int:
        return len(self._jobs)

    def __iter__(self) -> Iterator[RepoJob]:
        return iter(self._jobs.values())


class FirestoreJobStore:
    """Firestore-backed store.

    The client is constructed lazily so that importing this module on a machine
    with no Google credentials is harmless — a property the test suite depends
    on and CI proves on every push.
    """

    def __init__(self, *, project: str, database: str = "(default)") -> None:
        self._project = project
        self._database = database
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import firestore  # imported here, not at module load

            self._client = firestore.Client(project=self._project, database=self._database)
        return self._client

    def put(self, job: RepoJob) -> None:
        self.client.collection(_COLLECTION).document(job.job_id).set(job.to_dict())

    def get(self, job_id: str) -> RepoJob | None:
        snapshot = self.client.collection(_COLLECTION).document(job_id).get()
        if not snapshot.exists:
            return None
        return RepoJob.from_dict(snapshot.to_dict())

    def list_jobs(self, *, run_id: str | None = None) -> list[RepoJob]:
        collection = self.client.collection(_COLLECTION)
        query = collection if run_id is None else collection.where("run_id", "==", run_id)
        return [RepoJob.from_dict(doc.to_dict()) for doc in query.stream()]


def outcome_counts(store: JobStore, *, run_id: str | None = None) -> dict[str, int]:
    """Nightly tally, for the dashboard and for the final numbers."""
    counts = {str(outcome): 0 for outcome in Outcome}
    counts["IN_FLIGHT"] = 0
    for job in store.list_jobs(run_id=run_id):
        counts[str(job.outcome) if job.outcome else "IN_FLIGHT"] += 1
    return counts
