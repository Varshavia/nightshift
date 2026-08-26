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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from nightshift_core.models import Outcome, Phase, RepoJob

__all__ = [
    "Approval",
    "ApprovalStore",
    "FirestoreApprovalStore",
    "FirestoreJobStore",
    "JobStore",
    "MemoryApprovalStore",
    "MemoryJobStore",
    "document_id",
    "unfinished_state",
]

_COLLECTION = "nightshift_jobs"
_APPROVALS = "nightshift_approvals"


def document_id(identifier: str) -> str:
    """A Firestore document id built from something that may contain a slash.

    Firestore reads ``/`` as a path separator, so ``run1:Varshavia/throttled``
    is not a document id at all — it is three path elements, and the client
    refuses it with "a document must have an even number of path elements".

    This bit the job store on the first night anything actually wrote a job: the
    identifier had contained a repository name since the first commit, and until
    then every write had been to the in-memory store, where a slash is just a
    character. The approvals store was written later and keyed by repository, so
    the problem was obvious there and invisible here.

    The identifier itself is unchanged in the document body. Only the key is
    rewritten, so nothing downstream has to know this happened.
    """
    return identifier.replace("/", "__")


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
        self.client.collection(_COLLECTION).document(document_id(job.job_id)).set(job.to_dict())

    def get(self, job_id: str) -> RepoJob | None:
        snapshot = self.client.collection(_COLLECTION).document(document_id(job_id)).get()
        if not snapshot.exists:
            return None
        return RepoJob.from_dict(snapshot.to_dict())

    def list_jobs(self, *, run_id: str | None = None) -> list[RepoJob]:
        collection = self.client.collection(_COLLECTION)
        # Filtered on the job id, not on a `run_id` field, because there is no
        # such field: the run is the part of `job_id` before the colon, and the
        # memory store has always known that. This one asked Firestore for a
        # column nobody writes, so every filtered query came back empty and the
        # dashboard could only ever show every night at once — two
        # implementations of one protocol disagreeing about the data they share.
        #
        # A range over the prefix rather than reading the collection and
        # discarding most of it: `\uffff` sorts above any character the id can
        # contain, so the pair brackets exactly the ids that start `run_id:`.
        query = (
            collection
            if run_id is None
            else collection.where("job_id", ">=", f"{run_id}:").where(
                "job_id", "<", f"{run_id}:\uffff"
            )
        )
        return [RepoJob.from_dict(doc.to_dict()) for doc in query.stream()]


#: How long a job may sit unfinished before we stop calling it in flight.
#:
#: Tied to the worker's `--task-timeout` in infra/deploy.sh: Cloud Run kills a
#: task at thirty minutes, so a job whose record has not been touched since then
#: is not being worked on by anything — its container is gone.
#:
#: The margin over that is deliberate. Being early here would report live work
#: as abandoned, which is the more expensive mistake: it sends someone looking
#: for a bug in a worker that is simply still building a large project.
ABANDONED_AFTER = timedelta(minutes=45)


def unfinished_state(job: RepoJob, *, now: datetime | None = None) -> str | None:
    """What to call a job that has not finished. ``None`` if it has.

    Three states, because they are three different problems:

    ``IN_FLIGHT``  a worker is on it. The record was written recently enough
                   that a live container is the only explanation.

    ``WAITING``    published, and nothing has picked it up yet. Normal between
                   nights, and a backlog when it is forty of them: the fix is
                   more workers, not a debugger.

    ``ABANDONED``  a worker started and died. The record stopped partway — the
                   killed container's last checkpoint — and nothing will write
                   to it again. This is the one that means something is wrong.

    The distinction was learned twice over. First the dashboard called a
    stalled job in flight, so the fleet looked busy while it was stuck. Then it
    called every stalled job abandoned, which read as forty crashed workers when
    thirty-eight of them were a queue nobody had drained. Both are the same
    mistake: reporting a state the evidence does not support.

    Derived, never stored. The message is still on the queue, so a later worker
    moves the record on and the state corrects itself with nothing to clean up.
    A stored flag would have to be written back, and then the flag and the
    record could disagree about the same job.
    """
    if job.outcome is not None:
        return None
    if (now or datetime.now(UTC)) - job.updated_at <= ABANDONED_AFTER:
        return "IN_FLIGHT"
    return "WAITING" if job.phase is Phase.QUEUED else "ABANDONED"


def outcome_counts(store: JobStore, *, run_id: str | None = None) -> dict[str, int]:
    """Nightly tally, for the dashboard and for the final numbers.

    The three unfinished states sit alongside the outcomes without being any of
    them. Every member of ``Outcome`` describes a finished repair job (ADR 0003)
    and none of these finished anything — but folding them into ``IN_FLIGHT``
    says the fleet is busy when it is stalled, and folding them into an outcome
    claims a result nobody produced.
    """
    counts = {str(outcome): 0 for outcome in Outcome}
    counts["IN_FLIGHT"] = 0
    counts["WAITING"] = 0
    counts["ABANDONED"] = 0
    now = datetime.now(UTC)
    for job in store.list_jobs(run_id=run_id):
        counts[unfinished_state(job, now=now) or str(job.outcome)] += 1
    return counts


# --------------------------------------------------------------------------- #
# Upstream approvals
# --------------------------------------------------------------------------- #
#
# `ALLOW_UPSTREAM_PRS` is false and stays false. An approval is the narrow,
# per-repository exception to it: a named person deciding that one repository's
# pull request may go to its upstream rather than sitting on our fork.
#
# It is stored rather than configured because RESPONSIBLE_USE.md promises the
# decision is recorded and revocable, and a flag in an environment variable is
# neither. There is no bulk approve and there will not be one — the type below
# takes a single repository, which is the cheapest possible way to make that
# guarantee structural instead of cultural.


@dataclass(frozen=True, slots=True)
class Approval:
    """One person allowing one repository's pull request upstream."""

    repo: str
    approver: str
    approved_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    note: str = ""

    def __post_init__(self) -> None:
        if not self.repo.strip():
            raise ValueError("an approval must name a repository")
        if not self.approver.strip():
            # Unattributed approval is indistinguishable from no approval, and
            # the whole point of recording it is that somebody's name is on it.
            raise ValueError("an approval must name who gave it")

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "approver": self.approver,
            "approved_at": self.approved_at.isoformat(),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Approval:
        return cls(
            repo=data["repo"],
            approver=data["approver"],
            approved_at=datetime.fromisoformat(data["approved_at"])
            if data.get("approved_at")
            else datetime.now(UTC),
            note=data.get("note", ""),
        )


@runtime_checkable
class ApprovalStore(Protocol):
    def approve(self, approval: Approval) -> None: ...

    def revoke(self, repo: str) -> None: ...

    def approved(self, repo: str) -> Approval | None: ...

    def list_approvals(self) -> list[Approval]: ...


class MemoryApprovalStore:
    def __init__(self) -> None:
        self._by_repo: dict[str, Approval] = {}

    def approve(self, approval: Approval) -> None:
        self._by_repo[approval.repo] = approval

    def revoke(self, repo: str) -> None:
        self._by_repo.pop(repo, None)

    def approved(self, repo: str) -> Approval | None:
        return self._by_repo.get(repo)

    def list_approvals(self) -> list[Approval]:
        return sorted(self._by_repo.values(), key=lambda a: a.repo)


class FirestoreApprovalStore:
    """Approvals in Firestore, keyed by repository.

    The document id is the repository with its slash replaced, because Firestore
    document ids may not contain one. Keying by repository rather than appending
    events means approving twice is idempotent and revoking is a delete — both
    of which a reviewer can reason about without reading a log.
    """

    def __init__(self, *, project: str, database: str = "(default)") -> None:
        self._project = project
        self._database = database
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import firestore  # imported here, not at module load

            self._client = firestore.Client(project=self._project, database=self._database)
        return self._client

    def approve(self, approval: Approval) -> None:
        self.client.collection(_APPROVALS).document(document_id(approval.repo)).set(
            approval.to_dict()
        )

    def revoke(self, repo: str) -> None:
        self.client.collection(_APPROVALS).document(document_id(repo)).delete()

    def approved(self, repo: str) -> Approval | None:
        snapshot = self.client.collection(_APPROVALS).document(document_id(repo)).get()
        if not snapshot.exists:
            return None
        return Approval.from_dict(snapshot.to_dict())

    def list_approvals(self) -> list[Approval]:
        documents = self.client.collection(_APPROVALS).stream()
        return sorted(
            (Approval.from_dict(doc.to_dict()) for doc in documents),
            key=lambda a: a.repo,
        )
