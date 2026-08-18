"""Triage: the cheap gate before an expensive model is woken."""

from __future__ import annotations

from services.scanner.main import triage

from nightshift_core.models import Severity, Vulnerability


def make(package: str, severity: Severity, fixed: str | None = "2.0") -> Vulnerability:
    return Vulnerability(
        osv_id=f"GHSA-{package}",
        package=package,
        installed_version="1.0",
        fixed_version=fixed,
        severity=severity,
    )


def test_low_severity_is_dropped() -> None:
    kept = triage([make("a", Severity.LOW), make("b", Severity.HIGH)])
    assert [v.package for v in kept] == ["b"]


def test_the_floor_is_inclusive_of_moderate() -> None:
    assert [v.package for v in triage([make("a", Severity.MODERATE)])] == ["a"]


def test_an_advisory_with_no_fix_is_dropped() -> None:
    """There is nothing to schedule: NO_FIX_AVAILABLE is decided per job."""
    assert list(triage([make("a", Severity.CRITICAL, fixed=None)])) == []


def test_unknown_severity_is_dropped_but_critical_is_kept() -> None:
    kept = triage([make("a", Severity.UNKNOWN), make("b", Severity.CRITICAL)])
    assert [v.package for v in kept] == ["b"]


def test_an_empty_input_gives_an_empty_result() -> None:
    assert list(triage([])) == []


def test_order_is_preserved() -> None:
    """The scanner pairs results back to dependencies positionally-ish; do not shuffle."""
    kept = triage([make("z", Severity.HIGH), make("a", Severity.CRITICAL)])
    assert [v.package for v in kept] == ["z", "a"]
