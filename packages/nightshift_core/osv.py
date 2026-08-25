"""OSV.dev client.

Chosen over a commercial vulnerability feed for three reasons that survived
review: it needs no API key, it imposes no rate limit worth designing around,
and it exposes a *batch* endpoint. That last one is what makes a nightly scan of
several hundred repositories one request rather than several thousand.

The batch endpoint returns advisory ids only. Details are fetched once per
distinct id and cached for the run, because at fleet scale the same handful of
advisories appear in a great many repositories.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from nightshift_core.models import Dependency, Severity, Vulnerability

__all__ = ["OSV_API", "OSVClient", "pick_fixed_version"]

OSV_API = "https://api.osv.dev"

#: OSV caps a batch at 1000 queries. Chunk below it rather than at it.
log = logging.getLogger("nightshift.osv")

#: Three attempts and no more. A fleet scan that keeps retrying a service which
#: is genuinely down turns one bad night into a very slow bad night, and the
#: verdict it would eventually record — PROBE_ERROR — is the same either way.
_RETRY_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 1.0

_BATCH_SIZE = 900

_SEVERITY_BY_SCORE: tuple[tuple[float, Severity], ...] = (
    (9.0, Severity.CRITICAL),
    (7.0, Severity.HIGH),
    (4.0, Severity.MODERATE),
    (0.1, Severity.LOW),
)


def _parse(version: str) -> Version | None:
    try:
        return Version(version)
    except InvalidVersion:
        return None


def pick_fixed_version(affected: Iterable[dict[str, Any]], installed: str) -> str | None:
    """The lowest published version that fixes the advisory and is an upgrade.

    Lowest rather than latest on purpose: the smallest version jump is the one
    least likely to break the calling code, and when it does break it, the
    repair is the smallest. Aiming at the newest release would inflate the
    repair rate by manufacturing breakage the advisory did not require.
    """
    current = _parse(installed)
    candidates: list[Version] = []
    for entry in affected:
        for range_ in entry.get("ranges", []):
            if range_.get("type") not in {"ECOSYSTEM", "SEMVER"}:
                continue
            for event in range_.get("events", []):
                fixed = event.get("fixed")
                if not fixed:
                    continue
                parsed = _parse(str(fixed))
                if parsed is not None and (current is None or parsed > current):
                    candidates.append(parsed)
    if not candidates:
        return None
    return str(min(candidates))


def _severity_of(vuln: dict[str, Any]) -> Severity:
    """GHSA's own label if present, otherwise bucketed from the CVSS score."""
    labelled = str(vuln.get("database_specific", {}).get("severity", "")).upper()
    if labelled in Severity.__members__:
        return Severity[labelled]
    for entry in vuln.get("severity", []):
        score = entry.get("score")
        try:
            numeric = float(score)
        except (TypeError, ValueError):
            continue
        for threshold, severity in _SEVERITY_BY_SCORE:
            if numeric >= threshold:
                return severity
    return Severity.UNKNOWN


class OSVClient:
    """Thin, synchronous, and deliberately dumb about our domain."""

    def __init__(self, client: httpx.Client | None = None, *, timeout: float = 30.0) -> None:
        self._client = client or httpx.Client(base_url=OSV_API, timeout=timeout)
        self._owns_client = client is None
        self._details: dict[str, dict[str, Any]] = {}

    def __enter__(self) -> OSVClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- plumbing ----------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One OSV call, retried while the failure is plausibly temporary.

        OSV is a free public service and a fleet scan asks it a few thousand
        questions in a few minutes. It answers 503 sometimes; that is not a
        statement about the repository being scanned, and it must not end up
        recorded as one.

        This is not a hypothetical either. A single 503 on one advisory ended
        the probe of ``flask-jwt-extended`` — a repository with 107 usable tests
        and a cryptography upgrade seven majors wide, which is to say the most
        promising candidate in the pool — and filed it as PROBE_ERROR.

        Only 5xx and transport errors are retried. A 400 means we asked a
        malformed question and asking it again more slowly will not improve it.
        """
        delay = _RETRY_BASE_SECONDS
        last: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = self._client.request(method, path, **kwargs)
                if response.status_code < 500:
                    response.raise_for_status()
                    return response
                last = httpx.HTTPStatusError(
                    f"OSV answered {response.status_code}", request=response.request,
                    response=response,
                )
            except httpx.TransportError as exc:
                last = exc
            if attempt < _RETRY_ATTEMPTS - 1:
                log.info("OSV %s %s failed (%s); retrying in %.1fs", method, path, last, delay)
                time.sleep(delay)
                delay *= 2
        assert last is not None
        raise last

    # -- raw endpoints ------------------------------------------------------ #

    def query_batch(self, dependencies: Sequence[Dependency]) -> list[list[str]]:
        """Advisory ids per dependency, in the order given.

        One request per chunk. The result is positional — OSV guarantees the
        results array lines up with the queries array — so callers zip it back
        against their own list rather than matching on package name.
        """
        ids: list[list[str]] = []
        for start in range(0, len(dependencies), _BATCH_SIZE):
            chunk = dependencies[start : start + _BATCH_SIZE]
            payload = {
                "queries": [
                    {
                        "package": {"name": dep.name, "ecosystem": dep.ecosystem},
                        "version": dep.version,
                    }
                    for dep in chunk
                ]
            }
            response = self._request("POST", "/v1/querybatch", json=payload)
            results = response.json().get("results", [])
            for index in range(len(chunk)):
                entry = results[index] if index < len(results) else {}
                ids.append([v["id"] for v in entry.get("vulns", []) if "id" in v])
        return ids

    def get_vulnerability(self, osv_id: str) -> dict[str, Any]:
        """Full advisory, cached for the lifetime of the client."""
        cached = self._details.get(osv_id)
        if cached is not None:
            return cached
        response = self._request("GET", f"/v1/vulns/{osv_id}")
        detail: dict[str, Any] = response.json()
        self._details[osv_id] = detail
        return detail

    # -- domain view -------------------------------------------------------- #

    def find_vulnerabilities(self, dependencies: Sequence[Dependency]) -> list[Vulnerability]:
        """Everything affecting the given pins, including the unfixable ones.

        Advisories with no published fix are returned rather than filtered out.
        They become ``NO_FIX_AVAILABLE`` further down, which is a result the
        fleet reports — a repository we could not help is still information.
        """
        found: list[Vulnerability] = []
        for dependency, osv_ids in zip(dependencies, self.query_batch(dependencies), strict=True):
            for osv_id in osv_ids:
                detail = self.get_vulnerability(osv_id)
                affected = [
                    entry
                    for entry in detail.get("affected", [])
                    if entry.get("package", {}).get("name", "").lower() == dependency.name.lower()
                ]
                found.append(
                    Vulnerability(
                        osv_id=osv_id,
                        package=dependency.name,
                        installed_version=dependency.version,
                        fixed_version=pick_fixed_version(affected, dependency.version),
                        severity=_severity_of(detail),
                        summary=detail.get("summary", "") or detail.get("details", "")[:280],
                        aliases=tuple(detail.get("aliases", [])),
                    )
                )
        return found
