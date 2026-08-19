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
