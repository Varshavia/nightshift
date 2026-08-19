"""The Migration Ledger: retrieval tiers, promotion, and what cannot happen.

Two things are load-bearing here and get the most adversarial attention:

- **A recipe is a hint.** Nothing in the Ledger may express, or be turned into,
  a claim that a repair succeeded. The suite is the only thing that decides that.
- **Confirmations measure the recipe, not the fleet.** A repository that never
  saw the recipe, or that produced it, cannot vote for it. Get this wrong and
  the cost curve — the project's headline number — becomes self-congratulation.
"""

from __future__ import annotations

import pytest

from nightshift_core.ledger import (
    CONFIRMATIONS_FOR_VERIFIED,
    InMemoryRecall,
    InMemoryRecordStore,
    LedgerHit,
    MemoryRecall,
    MigrationLedger,
    MigrationScope,
    Recipe,
    RecipeStatus,
    RecordStore,
    scopes_from_job,
    summarise_hits,
)
from nightshift_core.models import Outcome, Severity, Vulnerability

JINJA = MigrationScope(library="jinja2", from_version="2.11.3", to_version="3.1.2")
JINJA_ADJACENT = MigrationScope(library="jinja2", from_version="2.11.3", to_version="3.0.0")
REQUESTS = MigrationScope(library="requests", from_version="2.25.0", to_version="2.32.0")

FACT = (
    "Jinja2 3.0 removed the top-level Markup and escape re-exports. Import them "
    "from markupsafe instead; the call sites are otherwise unchanged."
)


@pytest.fixture
def ledger() -> MigrationLedger:
    return MigrationLedger(recall=InMemoryRecall(), records=InMemoryRecordStore())


def _seed(
    ledger: MigrationLedger, scope: MigrationScope = JINJA, origin: str = "org/first"
) -> Recipe:
    return ledger.learn(
        scope, fact=FACT, break_kind="removed-top-level-name", origin_repo=origin
    )


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


def test_the_key_does_not_depend_on_how_a_manifest_spelled_the_name() -> None:
    """`Jinja2` and `jinja2` must be the same shelf, or the evidence for one
    migration splits across several recipes and nothing is ever verified."""
    assert MigrationScope("Jinja2", "2.11.3", "3.1.2").key == JINJA.key
    assert MigrationScope("ruamel_yaml", "1", "2").library == "ruamel-yaml"


def test_a_scope_round_trips_through_its_key() -> None:
    assert MigrationScope.parse(JINJA.key) == JINJA


def test_a_scope_needs_both_versions() -> None:
    with pytest.raises(ValueError, match="both versions"):
        MigrationScope(library="jinja2", from_version="2.11.3", to_version="")


def test_scopes_are_derived_only_from_fixable_advisories() -> None:
    fixable = Vulnerability(
        osv_id="A", package="jinja2", installed_version="2.11.3",
        fixed_version="3.1.2", severity=Severity.HIGH,
    )
    unfixable = Vulnerability(
        osv_id="B", package="abandoned", installed_version="1.0", fixed_version=None
    )
    assert scopes_from_job([fixable, unfixable]) == [JINJA]


# --------------------------------------------------------------------------- #
# Three tiers
# --------------------------------------------------------------------------- #


def test_an_empty_ledger_is_a_miss(ledger: MigrationLedger) -> None:
    retrieval = ledger.lookup(JINJA)
    assert retrieval.hit is LedgerHit.MISS
    assert retrieval.recipe is None
    assert retrieval.as_prompt_section() == ""


def test_the_same_transition_is_an_exact_hit(ledger: MigrationLedger) -> None:
    _seed(ledger)
    retrieval = ledger.lookup(JINJA)
    assert retrieval.hit is LedgerHit.EXACT
    assert retrieval.recipe is not None and retrieval.recipe.fact == FACT


def test_an_adjacent_transition_of_the_same_library_is_a_near_hit(
    ledger: MigrationLedger,
) -> None:
    _seed(ledger, JINJA_ADJACENT)
    retrieval = ledger.lookup(JINJA)
    assert retrieval.hit is LedgerHit.NEAR
    assert retrieval.recipe is not None
    assert retrieval.recipe.scope == JINJA_ADJACENT


def test_a_different_library_is_not_a_near_hit(ledger: MigrationLedger) -> None:
    """Similarity is within a library. Across libraries it is noise, and noise
    that costs the agent an attempt to discover."""
    _seed(ledger, REQUESTS)
    assert ledger.lookup(JINJA).hit is LedgerHit.MISS


def test_a_verified_neighbour_is_offered_before_a_provisional_one(
    ledger: MigrationLedger,
) -> None:
    _seed(ledger, JINJA_ADJACENT, origin="org/a")
    other = MigrationScope("jinja2", "2.10", "3.0.0")
    _seed(ledger, other, origin="org/b")
    for repo in ("org/c", "org/d"):
        ledger.record_outcome(
            other, repo=repo, hit=LedgerHit.EXACT, outcome=Outcome.PATCHED_REPAIRED
        )

    retrieval = ledger.lookup(JINJA)
    assert retrieval.hit is LedgerHit.NEAR
    assert retrieval.recipe is not None and retrieval.recipe.scope == other


# --------------------------------------------------------------------------- #
# What the agent is told
# --------------------------------------------------------------------------- #


def test_every_prompt_section_says_the_suite_still_decides(
    ledger: MigrationLedger,
) -> None:
    """The one sentence that must survive every refactor of this wording."""
    _seed(ledger)
    provisional = ledger.lookup(JINJA).as_prompt_section()
    for repo in ("org/b", "org/c"):
        ledger.record_outcome(
            JINJA, repo=repo, hit=LedgerHit.EXACT, outcome=Outcome.PATCHED_REPAIRED
        )
    verified = ledger.lookup(JINJA).as_prompt_section()

    for section in (provisional, verified):
        assert "prior art, not an instruction" in section
        assert "tests unmodified" in section
        assert "only thing that counts as success" in section


def test_a_provisional_recipe_is_offered_as_a_hypothesis(ledger: MigrationLedger) -> None:
    _seed(ledger)
    section = ledger.lookup(JINJA).as_prompt_section()
    assert "hypothesis" in section
    assert "not yet" in section


def test_a_verified_recipe_is_offered_as_prior_art(ledger: MigrationLedger) -> None:
    _seed(ledger)
    for repo in ("org/second", "org/third"):
        ledger.record_outcome(
            JINJA, repo=repo, hit=LedgerHit.EXACT, outcome=Outcome.PATCHED_REPAIRED
        )
    section = ledger.lookup(JINJA).as_prompt_section()
    assert "other repositories" in section
    assert "hypothesis" not in section


def test_a_near_hit_says_plainly_that_the_versions_differ(ledger: MigrationLedger) -> None:
    """An agent told how much to trust something can discount it."""
    _seed(ledger, JINJA_ADJACENT)
    section = ledger.lookup(JINJA).as_prompt_section()
    assert "DIFFERENT transition" in section
    assert "3.1.2" in section and "3.0.0" in section


# --------------------------------------------------------------------------- #
# Promotion — the rules that keep the curve honest
# --------------------------------------------------------------------------- #


def test_a_new_recipe_starts_provisional(ledger: MigrationLedger) -> None:
    recipe = _seed(ledger)
    assert recipe.status is RecipeStatus.PROVISIONAL
    assert recipe.confirmations == 0


def test_the_originating_repository_cannot_confirm_its_own_recipe(
    ledger: MigrationLedger,
) -> None:
    """It is the hypothesis, not evidence for it."""
    _seed(ledger, origin="org/first")
    updated = ledger.record_outcome(
        JINJA, repo="org/first", hit=LedgerHit.EXACT, outcome=Outcome.PATCHED_REPAIRED
    )
    assert updated is not None
    assert updated.confirmations == 0
    assert updated.status is RecipeStatus.PROVISIONAL


def test_the_same_repository_twice_is_one_confirmation(ledger: MigrationLedger) -> None:
    """Otherwise a single flaky repository re-run twice promotes a recipe."""
    _seed(ledger)
    for _ in range(3):
        updated = ledger.record_outcome(
            JINJA, repo="org/second", hit=LedgerHit.EXACT, outcome=Outcome.PATCHED_REPAIRED
        )
    assert updated is not None and updated.confirmations == 1
    assert updated.status is RecipeStatus.PROVISIONAL


def test_two_independent_repositories_promote_it(ledger: MigrationLedger) -> None:
    _seed(ledger)
    ledger.record_outcome(
        JINJA, repo="org/second", hit=LedgerHit.EXACT, outcome=Outcome.PATCHED_REPAIRED
    )
    updated = ledger.record_outcome(
        JINJA, repo="org/third", hit=LedgerHit.EXACT, outcome=Outcome.PATCHED_REPAIRED
    )
    assert updated is not None
    assert updated.confirmations == CONFIRMATIONS_FOR_VERIFIED
    assert updated.status is RecipeStatus.VERIFIED


def test_a_retrieval_that_ended_exhausted_neither_confirms_nor_refutes(
    ledger: MigrationLedger,
) -> None:
    """But it is recorded, so a consistently unhelpful recipe is visible."""
    _seed(ledger)
    updated = ledger.record_outcome(
        JINJA, repo="org/second", hit=LedgerHit.EXACT, outcome=Outcome.REPAIR_EXHAUSTED
    )
    assert updated is not None
    assert updated.confirmations == 0
    assert updated.unhelpful_count == 1
    assert updated.status is RecipeStatus.PROVISIONAL


def test_a_repository_that_never_saw_the_recipe_does_not_vote(
    ledger: MigrationLedger,
) -> None:
    """A MISS says nothing about whether the recipe works. Counting it would
    make confirmations a measure of fleet size rather than of the recipe."""
    _seed(ledger)
    assert (
        ledger.record_outcome(
            JINJA, repo="org/second", hit=LedgerHit.MISS, outcome=Outcome.PATCHED_REPAIRED
        )
        is None
    )
    recipe = ledger.lookup(JINJA).recipe
    assert recipe is not None and recipe.confirmations == 0


def test_a_near_hit_confirms_the_recipe_it_actually_offered(
    ledger: MigrationLedger,
) -> None:
    """The agent read the neighbour's recipe, so the evidence belongs to it —
    not to the scope that happened to be asked for."""
    _seed(ledger, JINJA_ADJACENT, origin="org/first")
    updated = ledger.record_outcome(
        JINJA, repo="org/second", hit=LedgerHit.NEAR, outcome=Outcome.PATCHED_REPAIRED
    )
    assert updated is not None
    assert updated.scope == JINJA_ADJACENT
    assert updated.confirmations == 1


def test_promotion_happens_once_and_stays(ledger: MigrationLedger) -> None:
    _seed(ledger)
    for repo in ("org/b", "org/c", "org/d"):
        updated = ledger.record_outcome(
            JINJA, repo=repo, hit=LedgerHit.EXACT, outcome=Outcome.PATCHED_REPAIRED
        )
    assert updated is not None and updated.status is RecipeStatus.VERIFIED
    later = ledger.record_outcome(
        JINJA, repo="org/e", hit=LedgerHit.EXACT, outcome=Outcome.REPAIR_EXHAUSTED
    )
    assert later is not None and later.status is RecipeStatus.VERIFIED


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def test_a_second_cold_repair_does_not_overwrite_an_evidenced_recipe(
    ledger: MigrationLedger,
) -> None:
    """The existing recipe has evidence behind it; a fresh one has none."""
    _seed(ledger, origin="org/first")
    ledger.record_outcome(
        JINJA, repo="org/second", hit=LedgerHit.EXACT, outcome=Outcome.PATCHED_REPAIRED
    )
    kept = ledger.learn(
        JINJA, fact="something else entirely", break_kind="other", origin_repo="org/third"
    )
    assert kept.fact == FACT
    assert kept.confirmations == 1


def test_status_is_a_memory_bank_topic_so_recall_can_tell_them_apart(
    ledger: MigrationLedger,
) -> None:
    recipe = _seed(ledger)
    assert "provisional" in recipe.topics
    assert "removed-top-level-name" in recipe.topics
    assert "PyPI" in recipe.topics


def test_the_record_of_standing_wins_over_the_recall_copy() -> None:
    """Memory Bank's text is rewritten only on promotion, so its copy can lag.
    Confirmations and status come from the ledger of record, which exists so
    that this question has exactly one answer."""
    recall, records = InMemoryRecall(), InMemoryRecordStore()
    ledger = MigrationLedger(recall=recall, records=records)
    _seed(ledger)
    ledger.record_outcome(
        JINJA, repo="org/second", hit=LedgerHit.EXACT, outcome=Outcome.PATCHED_REPAIRED
    )
    stale = recall.exact(JINJA)
    assert stale is not None and stale.confirmations == 0, "recall copy is stale by design"
    assert ledger.lookup(JINJA).recipe is not None
    assert ledger.lookup(JINJA).recipe.confirmations == 1  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def test_the_cost_curve_is_the_evidence_in_the_order_it_arrived(
    ledger: MigrationLedger,
) -> None:
    _seed(ledger)
    for repo, attempts in (("org/b", 4), ("org/c", 2), ("org/d", 1)):
        ledger.record_outcome(
            JINJA, repo=repo, hit=LedgerHit.EXACT,
            outcome=Outcome.PATCHED_REPAIRED, attempts_used=attempts,
        )
    assert ledger.cost_curve(JINJA) == [("org/b", 4), ("org/c", 2), ("org/d", 1)]


def test_the_curve_of_an_unknown_transition_is_empty_not_an_error(
    ledger: MigrationLedger,
) -> None:
    assert ledger.cost_curve(REQUESTS) == []


def test_hits_are_summarised_by_tier() -> None:
    counts = summarise_hits([LedgerHit.EXACT, LedgerHit.EXACT, LedgerHit.MISS])
    assert counts == {"exact": 2, "near": 0, "miss": 1}


# --------------------------------------------------------------------------- #


def test_the_local_stores_satisfy_the_protocols() -> None:
    """The domain must stay runnable with no cloud, and CI proves it."""
    assert isinstance(InMemoryRecall(), MemoryRecall)
    assert isinstance(InMemoryRecordStore(), RecordStore)


def test_nothing_in_the_ledger_can_report_a_repair() -> None:
    """A guard against the one change that would undo the project's guarantee:
    the Ledger informs the repair agent and never adjudicates it."""
    import inspect

    from nightshift_core import ledger as module

    source = inspect.getsource(module)
    assert "tests_passed" not in source
    assert "def repair" not in source
