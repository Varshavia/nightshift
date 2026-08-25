"""OSV client. The network is mocked; the version arithmetic is not."""

from __future__ import annotations

import httpx
import pytest

from nightshift_core.models import Dependency, Severity
from nightshift_core.osv import OSV_API, OSVClient, pick_fixed_version

AFFECTED = [
    {
        "package": {"name": "requests", "ecosystem": "PyPI"},
        "ranges": [
            {
                "type": "ECOSYSTEM",
                "events": [{"introduced": "0"}, {"fixed": "2.20.0"}],
            },
            {
                "type": "ECOSYSTEM",
                "events": [{"introduced": "2.21.0"}, {"fixed": "2.31.0"}],
            },
        ],
    }
]


def test_smallest_upgrade_is_preferred_over_the_newest_release() -> None:
    """The smallest jump breaks the least — and inflating breakage would
    inflate our own repair rate."""
    assert pick_fixed_version(AFFECTED, "2.19.0") == "2.20.0"


def test_a_fix_below_the_installed_version_is_not_an_upgrade() -> None:
    assert pick_fixed_version(AFFECTED, "2.25.0") == "2.31.0"


def test_no_published_fix_reads_as_none() -> None:
    unfixed = [{"ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}]}]
    assert pick_fixed_version(unfixed, "1.0.0") is None


def test_unparsable_versions_do_not_crash_the_scan() -> None:
    weird = [{"ranges": [{"type": "ECOSYSTEM", "events": [{"fixed": "not-a-version"}]}]}]
    assert pick_fixed_version(weird, "1.0.0") is None


def _transport(handler: object) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.osv.dev",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def test_find_vulnerabilities_joins_the_batch_to_the_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/querybatch":
            return httpx.Response(200, json={"results": [{"vulns": [{"id": "GHSA-1"}]}, {}]})
        return httpx.Response(
            200,
            json={
                "id": "GHSA-1",
                "summary": "Header injection",
                "aliases": ["CVE-2018-18074"],
                "database_specific": {"severity": "HIGH"},
                "affected": AFFECTED,
            },
        )

    deps = [
        Dependency(name="requests", version="2.19.0"),
        Dependency(name="flask", version="3.0.0"),
    ]
    with OSVClient(_transport(handler)) as client:
        found = client.find_vulnerabilities(deps)

    assert len(found) == 1
    assert found[0].package == "requests"
    assert found[0].fixed_version == "2.20.0"
    assert found[0].severity is Severity.HIGH
    assert found[0].cve == "CVE-2018-18074"


def test_advisory_details_are_fetched_once_per_id() -> None:
    """At fleet scale the same advisory turns up in hundreds of repositories."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/querybatch":
            return httpx.Response(
                200, json={"results": [{"vulns": [{"id": "GHSA-1"}]}] * 2}
            )
        return httpx.Response(200, json={"id": "GHSA-1", "affected": AFFECTED})

    deps = [Dependency(name="requests", version="2.19.0")] * 2
    with OSVClient(_transport(handler)) as client:
        client.find_vulnerabilities(deps)

    assert calls.count("/v1/vulns/GHSA-1") == 1


def test_a_batch_larger_than_the_api_limit_is_chunked() -> None:
    batches: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode()
        batches.append(payload.count('"version"'))
        count = payload.count('"version"')
        return httpx.Response(200, json={"results": [{} for _ in range(count)]})

    deps = [Dependency(name=f"pkg{i}", version="1.0.0") for i in range(1000)]
    with OSVClient(_transport(handler)) as client:
        assert client.query_batch(deps) == [[] for _ in range(1000)]

    assert batches == [900, 100]


def test_an_osv_outage_is_not_swallowed() -> None:
    """A failed scan must look like a failure, not like a clean night."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    with OSVClient(_transport(handler)) as client, pytest.raises(httpx.HTTPStatusError):
        client.query_batch([Dependency(name="requests", version="2.19.0")])


def test_a_transient_server_error_is_retried_not_recorded_as_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One 503 cost us the best candidate in the pool.

    OSV is a free public service and a fleet scan asks it thousands of questions
    in minutes; it answers 503 sometimes. That says nothing about the repository
    being scanned, and `flask-jwt-extended` — 107 usable tests, a cryptography
    upgrade seven majors wide — was filed as PROBE_ERROR because of one.
    """
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"id": "GHSA-x", "affected": []})

    client = OSVClient(httpx.Client(base_url=OSV_API, transport=httpx.MockTransport(handler)))

    assert client.get_vulnerability("GHSA-x")["id"] == "GHSA-x"
    assert attempts["n"] == 3


def test_a_bad_request_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asking a malformed question again, more slowly, does not improve it."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, text="malformed")

    client = OSVClient(httpx.Client(base_url=OSV_API, transport=httpx.MockTransport(handler)))

    with pytest.raises(httpx.HTTPStatusError):
        client.get_vulnerability("GHSA-x")
    assert attempts["n"] == 1


def test_a_service_that_stays_down_eventually_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying forever turns one bad night into a very slow bad night."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, text="unavailable")

    client = OSVClient(httpx.Client(base_url=OSV_API, transport=httpx.MockTransport(handler)))

    with pytest.raises(httpx.HTTPStatusError):
        client.get_vulnerability("GHSA-x")
    assert attempts["n"] == 3
