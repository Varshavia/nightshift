"""The fork pool's format and its selection rule.

The rule encodes something measured rather than assumed. Probing four real
public repositories showed that libraries declare ranges — `requests>=2.0`,
`click~=8.0` — and a range has no installed version to ask OSV about. Two of the
four came back NOT_AFFECTED for that reason alone. So the pool must select
applications, and the test below is what stops that lesson being quietly undone
by someone lowering a threshold.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nightshift_core.fleet import (
    BUILD_TOOLING,
    MAX_REPO_SIZE_KB,
    MIN_PINNED_DEPENDENCIES,
    POOL_SCHEMA,
    Candidate,
    FleetEntry,
    FleetPool,
    eligibility,
    load_pool,
    propose,
    save_pool,
)


def application(**overrides: object) -> Candidate:
    """A pinned application: what the pool is for."""
    base: dict[str, object] = {
        "repo": "org/service",
        "stars": 400,
        "license_id": "MIT",
        "has_tests": True,
        "pinned_dependencies": 12,
        "size_kb": 4_000,
        "manifests": ("requirements.txt",),
    }
    base.update(overrides)
    return Candidate(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def test_a_pinned_application_is_accepted() -> None:
    ok, reason = eligibility(application())
    assert ok
    assert "12 pins" in reason


def test_a_library_is_rejected_because_it_declares_ranges() -> None:
    """Measured, not assumed: itsdangerous and tenacity both came back
    NOT_AFFECTED in the first probe run for exactly this reason."""
    ok, reason = eligibility(application(repo="org/library", pinned_dependencies=0))
    assert not ok
    assert "range has no version" in reason


def test_the_pin_threshold_is_a_threshold_not_a_formality() -> None:
    assert eligibility(application(pinned_dependencies=MIN_PINNED_DEPENDENCIES))[0]
    assert not eligibility(application(pinned_dependencies=MIN_PINNED_DEPENDENCIES - 1))[0]


@pytest.mark.parametrize("licence", ["GPL-3.0", "AGPL-3.0", "", "NOASSERTION", "Proprietary"])
def test_only_permissive_licences_are_eligible(licence: str) -> None:
    """We copy and modify this code. Complying with copyleft properly is a
    conversation, and a script should not be having conversations."""
    ok, reason = eligibility(application(license_id=licence))
    assert not ok
    assert "licence" in reason


@pytest.mark.parametrize("licence", ["MIT", "mit", "Apache-2.0", "BSD-3-Clause", "ISC"])
def test_permissive_licences_are_accepted_however_they_are_spelled(licence: str) -> None:
    assert eligibility(application(license_id=licence))[0]


def test_an_archived_repository_is_rejected() -> None:
    ok, reason = eligibility(application(archived=True))
    assert not ok
    assert "go nowhere" in reason


def test_a_repository_without_tests_cannot_be_evidence_of_anything() -> None:
    ok, reason = eligibility(application(has_tests=False))
    assert not ok
    assert "evidence" in reason


def test_a_fork_is_rejected_in_favour_of_its_upstream() -> None:
    assert not eligibility(application(fork=True))[0]


def test_the_licence_is_the_first_thing_said_about_a_repository() -> None:
    """A hard no should not be reported as a pin-count problem."""
    _, reason = eligibility(
        application(license_id="GPL-3.0", pinned_dependencies=0, has_tests=False)
    )
    assert "licence" in reason


def test_rejections_come_back_with_their_reasons() -> None:
    """A reviewer asking "why is this project not in here" gets an answer
    without rerunning anything."""
    accepted, rejected = propose(
        [
            application(repo="org/good"),
            application(repo="org/library", pinned_dependencies=1),
            application(repo="org/gpl", license_id="GPL-3.0"),
        ]
    )
    assert [c.repo for c in accepted] == ["org/good"]
    assert dict(rejected).keys() == {"org/library", "org/gpl"}
    assert "range" in dict(rejected)["org/library"]


# --------------------------------------------------------------------------- #
# The pool file
# --------------------------------------------------------------------------- #


def test_an_entry_carries_the_evidence_for_its_own_selection() -> None:
    entry = FleetEntry.from_candidate(
        application(), repo="nightshift-fleet/service", upstream="org/service"
    )
    assert entry.repo == "nightshift-fleet/service"
    assert entry.upstream == "org/service"
    assert entry.pinned_dependencies == 12
    assert entry.license_id == "MIT"


def test_a_pool_round_trips_through_a_file(tmp_path: Path) -> None:
    pool = FleetPool(
        entries=(
            FleetEntry(repo="ns/one", upstream="org/one", license_id="MIT", stars=10),
            FleetEntry(repo="ns/two", upstream="org/two", license_id="Apache-2.0"),
        )
    )
    path = tmp_path / "pool" / "fleet.json"
    save_pool(pool, path)
    restored = load_pool(path)
    assert restored.repos == ["ns/one", "ns/two"]
    assert restored.to_dict()["entries"] == pool.to_dict()["entries"]


def test_a_missing_pool_is_an_error_not_an_empty_fleet(tmp_path: Path) -> None:
    """A scanner that read a missing pool as "no repositories" would report a
    quiet night — the failure mode this project is least willing to have."""
    with pytest.raises(FileNotFoundError, match="build one"):
        load_pool(tmp_path / "nope.json")


def test_an_unknown_schema_is_refused_rather_than_guessed_at(tmp_path: Path) -> None:
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps({"schema": 99, "entries": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="rebuild it"):
        load_pool(path)


def test_the_current_schema_is_written_out(tmp_path: Path) -> None:
    path = tmp_path / "fleet.json"
    save_pool(FleetPool(), path)
    assert json.loads(path.read_text())["schema"] == POOL_SCHEMA


def test_merging_keeps_the_entry_that_has_already_been_reviewed() -> None:
    """The one in the pool may have been edited by hand; a fresh proposal has
    neither review nor edits behind it."""
    existing = FleetPool(
        entries=(FleetEntry(repo="ns/one", upstream="org/one", notes="checked by hand"),)
    )
    merged = existing.merged_with(
        [FleetEntry(repo="ns/one", upstream="org/one"), FleetEntry(repo="ns/two")]
    )
    assert merged.repos == ["ns/one", "ns/two"]
    assert merged.entries[0].notes == "checked by hand"


# --------------------------------------------------------------------------- #
# Size — the ceiling that the first real run made necessary
# --------------------------------------------------------------------------- #


def test_a_repository_too_large_for_a_job_is_rejected() -> None:
    """Measured, not guessed. The first real proposal returned seven usable
    repositories and every one of them — home-assistant/core, apache/superset —
    was larger than a fifteen-minute install and a fifteen-minute test run.
    Forking them fills the pool with jobs that can only end UNBUILDABLE."""
    ok, reason = eligibility(application(size_kb=MAX_REPO_SIZE_KB + 1))
    assert not ok
    assert "UNBUILDABLE" in reason


def test_a_repository_at_the_ceiling_is_still_accepted() -> None:
    assert eligibility(application(size_kb=MAX_REPO_SIZE_KB))[0]


def test_the_size_is_reported_in_units_a_person_reads() -> None:
    _, reason = eligibility(application(size_kb=512_000))
    assert "512 MB" in reason


def test_size_is_carried_into_the_pool_entry() -> None:
    entry = FleetEntry.from_candidate(
        application(size_kb=8_000), repo="ns/service", upstream="org/service"
    )
    assert entry.size_kb == 8_000
    assert FleetEntry.from_dict(entry.to_dict()).size_kb == 8_000


def test_a_repository_proposed_twice_is_accepted_once() -> None:
    """A duplicate would be forked twice and counted twice, inflating every
    number computed over the pool."""
    accepted, rejected = propose([application(repo="org/same"), application(repo="org/same")])
    assert [c.repo for c in accepted] == ["org/same"]
    assert rejected == []


def test_a_repository_rejected_twice_is_reported_once() -> None:
    _, rejected = propose(
        [application(repo="org/lib", pinned_dependencies=0)] * 3
    )
    assert len(rejected) == 1


def test_a_repository_with_nothing_to_fix_is_not_proposed() -> None:
    """The loop the first six probes closed.

    Every other check asks whether we *could* work on a repository. Two passed
    all of them, were forked, cloned, built and tested — and came back
    NOT_AFFECTED, because there was nothing wrong with them. That is the query
    arguing with itself: `pushed:>...` selects maintained projects, maintained
    projects keep their dependencies current, and current dependencies have no
    advisories. No GitHub qualifier expresses "green suite, stale pins", so the
    question is asked of OSV directly instead.
    """
    ok, reason = eligibility(application(advisories_checked=True, actionable_advisories=0))
    assert not ok
    assert "nothing for the fleet to upgrade" in reason


def test_not_having_looked_is_not_the_same_as_having_found_nothing() -> None:
    """`--no-advisories`, or an OSV outage, must not reject the whole world.

    Collapsing "nobody asked" into "the answer was zero" is the same error as
    reading a rate-limited tree request as "this repository has no tests": a
    failure on our side rewritten as a fact about somebody else's code.
    """
    ok, _ = eligibility(application(advisories_checked=False, actionable_advisories=0))
    assert ok


def test_an_affected_repository_says_what_is_wrong_with_it() -> None:
    ok, reason = eligibility(
        application(
            advisories_checked=True,
            actionable_advisories=3,
            advisory_packages=("jinja2", "urllib3"),
        )
    )
    assert ok
    assert "jinja2" in reason


def test_the_advisory_check_comes_after_the_cheap_refusals() -> None:
    """A repository refused for its licence should not cost a network call.

    Asserted through the reason rather than by counting calls: whichever check
    speaks first is the one that ran first.
    """
    _, reason = eligibility(
        application(license_id="CC-BY-4.0", advisories_checked=True, actionable_advisories=0)
    )
    assert "licence" in reason


def test_no_search_query_uses_a_logical_operator_between_qualifiers() -> None:
    """GitHub rejects it, and the rejection arrives as a 422 mid-run.

    `topic:flask OR topic:django` looks reasonable and is not: OR applies to
    free text only, and between qualifiers GitHub answers "The search contains
    only logical operators without any search terms". Qualifiers in one query
    are always ANDed, so the union has to be several searches merged on our
    side. This test is what stops the tidier-looking version coming back.
    """
    from scripts.build_fork_pool import APPLICATION_QUERIES, DEFAULT_QUERY

    for query in (DEFAULT_QUERY, *APPLICATION_QUERIES):
        assert " OR " not in query, query
        assert " NOT " not in query, query


def test_the_application_queries_ask_for_one_framework_each() -> None:
    from scripts.build_fork_pool import APPLICATION_QUERIES

    topics = [q.rsplit("topic:", 1)[1] for q in APPLICATION_QUERIES]
    assert sorted(topics) == ["django", "fastapi", "flask"]
    for query in APPLICATION_QUERIES:
        assert query.count("topic:") == 1, "two topics in one query means neither matches"


def test_an_advisory_against_build_tooling_is_not_evidence_of_a_break() -> None:
    """The count misleads, and one survey showed exactly how.

    `apiflask` was proposed with three advisories — filelock, virtualenv and
    wheel. Nothing imports those at runtime, so upgrading them cannot break
    calling code, and this project is about upgrades that do. Ranked on the raw
    count it sorted above repositories whose advisories were against Flask and
    PyJWT, which is precisely backwards.
    """
    tooling_only = application(advisory_packages=("filelock", "virtualenv", "wheel"))
    on_the_path = application(advisory_packages=("pyjwt", "cryptography", "black"))

    assert tooling_only.call_path_advisories == 0
    assert on_the_path.call_path_advisories == 2


def test_the_tooling_list_leaves_out_anything_genuinely_ambiguous() -> None:
    """A CLI really does break when click changes, and templates when jinja2 does.

    The list earns its keep by being short. Every name added to it silently
    removes a class of real break from consideration, so the ambiguous cases
    belong outside it.
    """
    for package in ("click", "requests", "jinja2", "urllib3", "flask", "django"):
        assert package not in BUILD_TOOLING
