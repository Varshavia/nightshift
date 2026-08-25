"""Triage: the cheap gate before an expensive model is woken."""

from __future__ import annotations

import json

import pytest
from services.scanner import main as scanner_main
from services.scanner.main import triage

from nightshift_core.config import Settings
from nightshift_core.models import RepoJob, Severity, Vulnerability


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


class _Future:
    """A publish future that records whether anyone waited for it."""

    def __init__(self, waited: list[float | None]) -> None:
        self._waited = waited

    def result(self, timeout: float | None = None) -> str:
        self._waited.append(timeout)
        return "message-1"


class _Publisher:
    def __init__(self) -> None:
        self.waited: list[float | None] = []
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic: str, data: bytes, **attributes: str) -> _Future:
        self.calls.append((topic, data, attributes))
        return _Future(self.waited)


def test_publishing_waits_for_the_message_to_be_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan that fires and exits reports work it may never have published.

    Pub/Sub's publish hands back a future and buffers inside the client. A Cloud
    Run Job that fanned out three hundred messages and returned would log "300
    jobs published" and could exit with most of them unsent — and a scan that
    published nothing looks exactly like a quiet night in the morning, which is
    the one failure this project refuses to have.
    """
    publisher = _Publisher()
    monkeypatch.setattr(scanner_main, "_publisher", lambda: publisher)

    job = RepoJob(job_id="run1:org/app", repo="org/app", vulnerabilities=[])
    message_id = scanner_main.publish(job, Settings(gcp_project="p", jobs_topic="t"))

    assert message_id == "message-1"
    assert publisher.waited == [30.0], "the publish must be waited on, not fired and forgotten"


def test_the_message_carries_the_job_and_names_it_in_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attributes are readable by a subscription filter and by the console.

    The body carries the same two fields, but reading them from there means
    parsing JSON — which is the difference between debugging a night's run and
    merely reading it.
    """
    publisher = _Publisher()
    monkeypatch.setattr(scanner_main, "_publisher", lambda: publisher)

    job = RepoJob(
        job_id="run1:org/app",
        repo="org/app",
        vulnerabilities=[make("jinja2", Severity.HIGH)],
    )
    scanner_main.publish(job, Settings(gcp_project="p", jobs_topic="nightshift-jobs"))

    topic, data, attributes = publisher.calls[0]
    assert topic == "projects/p/topics/nightshift-jobs"
    assert attributes == {"repo": "org/app", "job_id": "run1:org/app"}

    body = json.loads(data)
    assert body["repo"] == "org/app"
    assert body["vulnerabilities"][0]["package"] == "jinja2"


def test_one_message_per_repository_not_per_advisory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment build is the expensive part, so it happens once."""
    publisher = _Publisher()
    monkeypatch.setattr(scanner_main, "_publisher", lambda: publisher)

    job = RepoJob(
        job_id="run1:org/app",
        repo="org/app",
        vulnerabilities=[
            make("jinja2", Severity.HIGH),
            make("urllib3", Severity.CRITICAL),
            make("flask", Severity.MODERATE),
        ],
    )
    scanner_main.publish(job, Settings(gcp_project="p", jobs_topic="t"))

    assert len(publisher.calls) == 1
    assert len(json.loads(publisher.calls[0][1])["vulnerabilities"]) == 3
