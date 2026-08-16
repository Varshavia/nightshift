"""Structural checks on the Tier A case directories.

Cheap and fast — nothing is installed here. The point is that a malformed case
fails in CI rather than three weeks later during the run that produces the
number we publish.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nightshift_core.manifests import parse_requirements, rewrite_pin

CASES_ROOT = Path(__file__).resolve().parent.parent / "benchmark" / "cases"
CASE_DIRS = (
    sorted(path for path in CASES_ROOT.iterdir() if path.is_dir())
    if CASES_ROOT.is_dir()
    else []
)

REQUIRED_FIELDS = {
    "id",
    "tier",
    "package",
    "from_version",
    "to_version",
    "break_kind",
    "expected_failure",
}


def test_there_is_at_least_one_case() -> None:
    assert CASE_DIRS, "Tier A is empty; the regression suite measures nothing"


@pytest.mark.parametrize("case_dir", CASE_DIRS, ids=lambda p: p.name)
def test_case_metadata_is_complete(case_dir: Path) -> None:
    meta = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    assert set(meta) >= REQUIRED_FIELDS
    assert meta["id"] == case_dir.name
    assert meta["tier"] == "A"


@pytest.mark.parametrize("case_dir", CASE_DIRS, ids=lambda p: p.name)
def test_case_has_a_suite_of_its_own(case_dir: Path) -> None:
    """A case without tests cannot demonstrate a repair."""
    assert list(case_dir.rglob("test_*.py")), f"{case_dir.name} has no tests"


@pytest.mark.parametrize("case_dir", CASE_DIRS, ids=lambda p: p.name)
def test_the_declared_package_is_actually_pinned_at_the_declared_version(case_dir: Path) -> None:
    """The case must start where its metadata says it starts."""
    meta = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    text = (case_dir / "requirements.txt").read_text(encoding="utf-8")
    pinned = {d.name: d.version for d in parse_requirements(text)}
    assert pinned.get(meta["package"]) == meta["from_version"]


@pytest.mark.parametrize("case_dir", CASE_DIRS, ids=lambda p: p.name)
def test_the_upgrade_the_case_describes_can_actually_be_applied(case_dir: Path) -> None:
    """Exercises the same rewrite the worker will perform, without installing."""
    meta = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    text = (case_dir / "requirements.txt").read_text(encoding="utf-8")
    upgraded = rewrite_pin(text, meta["package"], meta["to_version"], "requirements.txt")
    pinned = {d.name: d.version for d in parse_requirements(upgraded)}
    assert pinned[meta["package"]] == meta["to_version"]
