"""The Migration Ledger — what the fleet knows, and how sure it is.

Every successful repair produces a **recipe**: a generalized, evidence-backed
rule about how one library transition breaks calling code and how it was fixed.
The fortieth repository to hit ``jinja2 2.11→3.1`` starts from the answer rather
than from the traceback, so cost per repository falls as the fleet works. That
curve is the headline number. See ADR 0004 and the design spec.

**The invariant that makes this safe.** A recipe is a *hint*, never an
instruction. The success criterion never changes: the repository's own suite
passes and the tests were not modified. A wrong recipe can waste attempts — it
cannot manufacture a false green. Nothing in this module is allowed to weaken
that, and there is no code path here that reports a repair.

**Two stores, deliberately.** Memory Bank is the agent's recall surface: text,
semantically searchable, scoped. Firestore is the ledger of record: counters,
provenance, audit trail. Incrementing a confirmation count by rewriting a
memory's text is the wrong shape, and Memory Bank is not a relational store.
This module composes both behind :class:`MigrationLedger` and neither is asked
to do the other's job.

Everything here is infrastructure-free: the protocols have in-memory
implementations that ``make run-local`` genuinely uses, so the whole read/write
path is exercised on a laptop with no credentials.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, Self, runtime_checkable

from packaging.utils import canonicalize_name

from nightshift_core.models import Outcome

__all__ = [
    "CONFIRMATIONS_FOR_VERIFIED",
    "Evidence",
    "InMemoryRecall",
    "InMemoryRecordStore",
    "LedgerHit",
    "MemoryRecall",
    "MigrationLedger",
    "MigrationScope",
    "Recipe",
    "RecipeStatus",
    "RecordStore",
    "Retrieval",
]

#: Independent confirmations required before a recipe is trusted as prior art.
#:
#: The spec says two in §4 (Promotion) and three in §9 (Risks); §4 is the
#: normative section, so two it is. Isolated here because the number is a
#: judgement call that the first real fleet run may well overturn: too low and a
#: coincidence gets promoted, too high and nothing is ever verified inside a
#: fleet of three hundred.
CONFIRMATIONS_FOR_VERIFIED = 2


def _now() -> datetime:
    return datetime.now(UTC)


class LedgerHit(StrEnum):
    """How the Ledger answered. The independent variable of the cost curve."""

    #: The exact transition has been solved before. Enters the prompt as prior art.
    EXACT = "exact"
    #: An adjacent transition of the same library. Enters labelled lower-confidence.
    NEAR = "near"
    #: Nothing known. Cold repair at full price; success writes a provisional recipe.
    MISS = "miss"


class RecipeStatus(StrEnum):
    PROVISIONAL = "provisional"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class MigrationScope:
    """The retrieval key: one library moving between two exact versions."""

    library: str
    from_version: str
    to_version: str

    def __post_init__(self) -> None:
        if not all((self.library, self.from_version, self.to_version)):
            raise ValueError("a migration scope needs a library and both versions")
        # Normalised on construction so that `Jinja2` and `jinja2` are the same
        # shelf. A key that depends on how a manifest happened to spell a name
        # would split the evidence for one migration across several recipes.
        object.__setattr__(self, "library", str(canonicalize_name(self.library)))

    @property
    def key(self) -> str:
        return f"{self.library}:{self.from_version}:{self.to_version}"

    @classmethod
    def parse(cls, key: str) -> Self:
        library, _, rest = key.partition(":")
        from_version, _, to_version = rest.partition(":")
        return cls(library=library, from_version=from_version, to_version=to_version)

    def as_dict(self) -> dict[str, str]:
        """Memory Bank scope filter."""
        return {
            "library": self.library,
            "from_version": self.from_version,
            "to_version": self.to_version,
        }

    def __str__(self) -> str:
        return f"{self.library} {self.from_version} → {self.to_version}"


@dataclass(frozen=True, slots=True)
class Evidence:
    """One repository's contribution to a recipe's standing."""

    repo: str
    osv_id: str = ""
    diff_sha: str = ""
    attempts_used: int = 0
    trace_id: str = ""
    #: The outcome that followed retrieval. Kept even when unhelpful, so a recipe
    #: that keeps being retrieved and keeps not working is visible rather than
    #: merely un-promoted.
    outcome: str = ""
    recorded_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "osv_id": self.osv_id,
            "diff_sha": self.diff_sha,
            "attempts_used": self.attempts_used,
            "trace_id": self.trace_id,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            repo=data["repo"],
            osv_id=data.get("osv_id", ""),
            diff_sha=data.get("diff_sha", ""),
            attempts_used=int(data.get("attempts_used", 0)),
            trace_id=data.get("trace_id", ""),
            outcome=data.get("outcome", ""),
            recorded_at=datetime.fromisoformat(data["recorded_at"])
            if data.get("recorded_at")
            else _now(),
        )


@dataclass(frozen=True, slots=True)
class Recipe:
    """What the fleet learned about one transition, and how sure it is."""

    scope: MigrationScope
    #: The generalized rule, one paragraph, written for another agent to read.
    fact: str
    #: How the upgrade breaks calling code, e.g. ``removed-top-level-name``.
    break_kind: str = ""
    status: RecipeStatus = RecipeStatus.PROVISIONAL
    #: The repository whose repair produced this. It is the hypothesis, so it
    #: never counts as evidence for itself.
    origin_repo: str = ""
    evidence: tuple[Evidence, ...] = ()
    first_seen: datetime = field(default_factory=_now)
    last_confirmed: datetime | None = None

    @property
    def confirmations(self) -> int:
        """Independent repositories where this was retrieved and then worked."""
        return len(self.confirming_repos)

    @property
    def confirming_repos(self) -> frozenset[str]:
        return frozenset(
            entry.repo
            for entry in self.evidence
            if entry.outcome == str(Outcome.PATCHED_REPAIRED) and entry.repo != self.origin_repo
        )

    @property
    def unhelpful_count(self) -> int:
        """Retrievals followed by exhaustion. Neither confirmation nor refutation."""
        return sum(1 for e in self.evidence if e.outcome == str(Outcome.REPAIR_EXHAUSTED))

    @property
    def verified(self) -> bool:
        return self.status is RecipeStatus.VERIFIED

    @property
    def topics(self) -> tuple[str, ...]:
        """Memory Bank topics. Status is one, so retrieval can tell them apart."""
        return tuple(t for t in (str(self.status), self.break_kind, "PyPI") if t)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.scope.key,
            "scope": self.scope.as_dict(),
            "fact": self.fact,
            "break_kind": self.break_kind,
            "status": str(self.status),
            "origin_repo": self.origin_repo,
            "confirmations": self.confirmations,
            "evidence": [e.to_dict() for e in self.evidence],
            "first_seen": self.first_seen.isoformat(),
            "last_confirmed": self.last_confirmed.isoformat() if self.last_confirmed else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        scope = data.get("scope")
        return cls(
            scope=MigrationScope(**scope) if scope else MigrationScope.parse(data["key"]),
            fact=data.get("fact", ""),
            break_kind=data.get("break_kind", ""),
            status=RecipeStatus(data.get("status", "provisional")),
            origin_repo=data.get("origin_repo", ""),
            evidence=tuple(Evidence.from_dict(e) for e in data.get("evidence", [])),
            first_seen=datetime.fromisoformat(data["first_seen"])
            if data.get("first_seen")
            else _now(),
            last_confirmed=datetime.fromisoformat(data["last_confirmed"])
            if data.get("last_confirmed")
            else None,
        )


@dataclass(frozen=True, slots=True)
class Retrieval:
    """The Ledger's answer to one lookup, in the form the repair agent gets it."""

    hit: LedgerHit
    requested: MigrationScope
    recipe: Recipe | None = None

    @property
    def useful(self) -> bool:
        return self.recipe is not None

    def as_prompt_section(self) -> str:
        """How a recipe enters the repair prompt — as a hint, never as an order.

        The wording carries the confidence rather than hiding it: prior art for a
        verified exact match, a hypothesis for a provisional one, and an explicit
        warning that the versions differ for a near hit. An agent that is told
        how much to trust something can discount it; one that is handed a bare
        instruction cannot.
        """
        if self.recipe is None:
            return ""
        recipe = self.recipe
        if self.hit is LedgerHit.NEAR:
            header = (
                f"A DIFFERENT transition of the same library has been solved before: "
                f"{recipe.scope}. You are attempting {self.requested}. The versions are "
                "not the same, so treat this as a lead and verify it against the "
                "installed library before relying on it."
            )
        elif recipe.verified:
            header = (
                f"This exact transition has been repaired successfully in "
                f"{recipe.confirmations} other repositories:"
            )
        else:
            header = (
                "A single earlier repair suggests the following, but it has not yet "
                "been confirmed anywhere else. Treat it as a hypothesis:"
            )
        return (
            f"{header}\n\n{recipe.fact}\n\n"
            "This is prior art, not an instruction. The suite passing with the new "
            "version installed, and the tests unmodified, remains the only thing "
            "that counts as success."
        )


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #


@runtime_checkable
class MemoryRecall(Protocol):
    """Vertex AI Memory Bank, reduced to what the fleet actually needs."""

    def write(self, recipe: Recipe) -> None: ...

    def exact(self, scope: MigrationScope) -> Recipe | None: ...

    def near(self, scope: MigrationScope) -> Recipe | None: ...


@runtime_checkable
class RecordStore(Protocol):
    """Firestore ledger of record: counters, provenance, audit trail."""

    def get(self, key: str) -> Recipe | None: ...

    def put(self, recipe: Recipe) -> None: ...

    def all(self) -> list[Recipe]: ...


class InMemoryRecall:
    """Recall without a cloud. Not a mock — ``make run-local`` uses it.

    ``near`` is a deliberately crude stand-in for Memory Bank's similarity
    search: same library, different versions, most recently confirmed first.
    Good enough to exercise the three-tier path honestly, and replaced rather
    than extended when the real API is wired in.
    """

    def __init__(self) -> None:
        self._recipes: dict[str, Recipe] = {}

    def write(self, recipe: Recipe) -> None:
        self._recipes[recipe.scope.key] = recipe

    def exact(self, scope: MigrationScope) -> Recipe | None:
        return self._recipes.get(scope.key)

    def near(self, scope: MigrationScope) -> Recipe | None:
        candidates = [
            recipe
            for key, recipe in self._recipes.items()
            if recipe.scope.library == scope.library and key != scope.key
        ]
        if not candidates:
            return None
        # Verified before provisional, then most recently confirmed. A near hit
        # is already a guess; offering the least-evidenced one would compound it.
        candidates.sort(
            key=lambda r: (r.verified, r.confirmations, r.last_confirmed or r.first_seen),
            reverse=True,
        )
        return candidates[0]

    def __len__(self) -> int:
        return len(self._recipes)


class InMemoryRecordStore:
    def __init__(self) -> None:
        self._records: dict[str, Recipe] = {}

    def get(self, key: str) -> Recipe | None:
        return self._records.get(key)

    def put(self, recipe: Recipe) -> None:
        self._records[recipe.scope.key] = recipe

    def all(self) -> list[Recipe]:
        return sorted(self._records.values(), key=lambda r: r.first_seen)


# --------------------------------------------------------------------------- #
# The Ledger
# --------------------------------------------------------------------------- #


class MigrationLedger:
    """Composes recall and record. The only thing the worker talks to.

    Writes go through :meth:`learn` and :meth:`record_outcome`; nothing else
    mutates a recipe. In production only the Librarian's service account can
    reach the write path at all — that is enforced by IAM rather than by this
    class, because an agent that cannot write to the Ledger cannot poison it
    whatever it has been persuaded of.
    """

    def __init__(self, *, recall: MemoryRecall, records: RecordStore) -> None:
        self._recall = recall
        self._records = records

    # -- read --------------------------------------------------------------- #

    def lookup(self, scope: MigrationScope) -> Retrieval:
        """Three tiers: exact, near, miss. Never raises — a Ledger outage
        degrades the fleet to cold repair and must never stop a run."""
        exact = self._recall.exact(scope)
        if exact is not None:
            return Retrieval(hit=LedgerHit.EXACT, requested=scope, recipe=self._enrich(exact))
        near = self._recall.near(scope)
        if near is not None:
            return Retrieval(hit=LedgerHit.NEAR, requested=scope, recipe=self._enrich(near))
        return Retrieval(hit=LedgerHit.MISS, requested=scope)

    def _enrich(self, recipe: Recipe) -> Recipe:
        """Recall holds the text; the record holds the standing. Prefer the record.

        Memory Bank's copy can lag — its text is rewritten only on promotion —
        so confirmations and status are read from Firestore, which is the ledger
        of record precisely so this question has one answer.
        """
        stored = self._records.get(recipe.scope.key)
        if stored is None:
            return recipe
        return replace(stored, fact=stored.fact or recipe.fact)

    # -- write -------------------------------------------------------------- #

    def learn(
        self, scope: MigrationScope, *, fact: str, break_kind: str, origin_repo: str
    ) -> Recipe:
        """Write a new provisional recipe, or leave an existing one alone.

        The Librarian's first job. A second repository solving the same
        transition from scratch does not overwrite what is already there — the
        existing recipe has evidence behind it and this one has none.
        """
        existing = self._records.get(scope.key)
        if existing is not None:
            return existing
        recipe = Recipe(
            scope=scope,
            fact=fact.strip(),
            break_kind=break_kind,
            status=RecipeStatus.PROVISIONAL,
            origin_repo=origin_repo,
        )
        self._records.put(recipe)
        self._recall.write(recipe)
        return recipe

    def record_outcome(
        self,
        scope: MigrationScope,
        *,
        repo: str,
        hit: LedgerHit,
        outcome: Outcome,
        osv_id: str = "",
        diff_sha: str = "",
        attempts_used: int = 0,
        trace_id: str = "",
    ) -> Recipe | None:
        """Record what happened after a recipe was retrieved, and promote if due.

        Only retrievals count. A repository that never saw the recipe says
        nothing about whether the recipe works, so a ``MISS`` is not recorded
        against it — that is what keeps the confirmation count a measure of the
        recipe rather than a measure of the fleet's size.
        """
        if hit is LedgerHit.MISS:
            return None
        recipe = self._records.get(scope.key) if hit is LedgerHit.EXACT else None
        if recipe is None:
            # A near hit confirms the recipe it actually offered, not the scope
            # that was asked for. Resolving it through recall keeps the evidence
            # attached to the recipe the agent really read.
            offered = self._recall.near(scope) if hit is LedgerHit.NEAR else None
            recipe = self._records.get(offered.scope.key) if offered else None
        if recipe is None:
            return None

        entry = Evidence(
            repo=repo,
            osv_id=osv_id,
            diff_sha=diff_sha,
            attempts_used=attempts_used,
            trace_id=trace_id,
            outcome=str(outcome),
        )
        updated = replace(recipe, evidence=(*recipe.evidence, entry))
        if outcome is Outcome.PATCHED_REPAIRED and repo != recipe.origin_repo:
            updated = replace(updated, last_confirmed=_now())
        updated = self._promote_if_due(updated)
        self._records.put(updated)
        if updated.status is not recipe.status:
            # Only promotion rewrites the memory: the topics changed, and the
            # repair agent reads confidence off the topic.
            self._recall.write(updated)
        return updated

    def _promote_if_due(self, recipe: Recipe) -> Recipe:
        if recipe.verified or recipe.confirmations < CONFIRMATIONS_FOR_VERIFIED:
            return recipe
        return replace(recipe, status=RecipeStatus.VERIFIED)

    # -- reporting ---------------------------------------------------------- #

    def recipes(self) -> list[Recipe]:
        return self._records.all()

    def cost_curve(self, scope: MigrationScope) -> list[tuple[str, int]]:
        """``(repo, attempts_used)`` in the order the fleet met this transition.

        The demo's shape, straight out of the evidence: the first repository
        pays full price and the later ones do not. Rendered from Cloud Trace in
        production; this is the same series, computed from the ledger of record
        so the number can be checked without a trace viewer.
        """
        recipe = self._records.get(scope.key)
        if recipe is None:
            return []
        return [(e.repo, e.attempts_used) for e in recipe.evidence]


def summarise_hits(hits: Iterable[LedgerHit]) -> dict[str, int]:
    """Counts by tier, for the dashboard and for the write-up."""
    counts = {str(hit): 0 for hit in LedgerHit}
    for hit in hits:
        counts[str(hit)] += 1
    return counts


def scopes_from_job(vulnerabilities: Sequence[Any]) -> list[MigrationScope]:
    """The transitions a job is about to attempt, in a stable order."""
    return [
        MigrationScope(
            library=v.package, from_version=v.installed_version, to_version=v.fixed_version
        )
        for v in vulnerabilities
        if getattr(v, "fixed_version", None)
    ]
