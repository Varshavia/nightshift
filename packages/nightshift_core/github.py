"""A GitHub client, narrowed to the five things the fleet actually does.

Search for candidates, read a repository's metadata, read a file, list a tree,
fork. Nothing else, because every method here is a capability someone could
later reach for, and a client that can do more than the fleet needs is a way for
the fleet to end up doing more than it should.

Reading a repository is deliberately done through the contents API rather than
by cloning. The scan touches hundreds of repositories and only wants two or
three small files from each; the worker is the only part that needs a working
tree, and it is the only part that clones.

The token is never logged, never included in an error message, and never
formatted into a URL — it goes in the ``Authorization`` header, which is the one
place it cannot leak into a shell history or a traceback.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = ["GITHUB_API", "GitHubClient", "GitHubError", "RepoMetadata"]

log = logging.getLogger("nightshift.github")

GITHUB_API = "https://api.github.com"

#: GitHub's search endpoints are far more restricted than the rest of the API
#: and answer 403 rather than 429 when they are unhappy. Slower than necessary
#: beats getting the account rate-limited during a demo.
_SEARCH_PAUSE_SECONDS = 2.0

_PAGE_SIZE = 100


class GitHubError(RuntimeError):
    """A GitHub request failed in a way the caller has to know about."""


@dataclass(frozen=True, slots=True)
class RepoMetadata:
    full_name: str
    stars: int = 0
    license_id: str = ""
    archived: bool = False
    fork: bool = False
    default_branch: str = "main"
    pushed_at: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> RepoMetadata:
        licence = data.get("license") or {}
        return cls(
            full_name=data.get("full_name", ""),
            stars=int(data.get("stargazers_count", 0)),
            license_id=str(licence.get("spdx_id") or ""),
            archived=bool(data.get("archived", False)),
            fork=bool(data.get("fork", False)),
            default_branch=data.get("default_branch") or "main",
            pushed_at=data.get("pushed_at") or "",
        )


class GitHubClient:
    """Synchronous, minimal, and injectable for tests."""

    def __init__(
        self,
        token: str = "",
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        pause: float = _SEARCH_PAUSE_SECONDS,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(base_url=GITHUB_API, timeout=timeout)
        # Applied to an injected client too. The credential belongs to this
        # object, not to whatever transport it was handed: attaching it only to
        # the client we construct ourselves would mean a test — or a caller
        # sharing a connection pool — silently made unauthenticated requests and
        # found out through a rate limit rather than an error.
        self._client.headers.update(headers)
        self._owns_client = client is None
        self._pause = pause

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- plumbing ----------------------------------------------------------- #

    def _get(self, path: str, **params: Any) -> httpx.Response:
        response = self._client.get(path, params=params or None)
        if response.status_code == 403 and "rate limit" in response.text.lower():
            raise GitHubError(
                "GitHub rate limit reached. The pool is built once and reviewed by hand, "
                "so waiting is the correct response rather than retrying harder."
            )
        return response

    # -- the five things ---------------------------------------------------- #

    def search_repositories(self, query: str, *, limit: int = 100) -> list[RepoMetadata]:
        """Repository search, paginated, stopping at ``limit``.

        Search is the one place the fleet looks at repositories it has not been
        told about, and its output is a *proposal* — never a target. Nothing is
        forked from here without a human reading the list first. See ADR 0002.
        """
        found: list[RepoMetadata] = []
        page = 1
        while len(found) < limit:
            response = self._get(
                "/search/repositories",
                q=query,
                per_page=min(_PAGE_SIZE, limit - len(found)),
                page=page,
                sort="stars",
                order="desc",
            )
            if response.status_code != 200:
                raise GitHubError(f"search failed: {response.status_code} {response.text[:200]}")
            items = response.json().get("items", [])
            if not items:
                break
            found.extend(RepoMetadata.from_api(item) for item in items)
            page += 1
            if self._pause:
                time.sleep(self._pause)
        return found[:limit]

    def get_repo(self, repo: str) -> RepoMetadata | None:
        response = self._get(f"/repos/{repo}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GitHubError(f"could not read {repo}: {response.status_code}")
        return RepoMetadata.from_api(response.json())

    def get_file(self, repo: str, path: str, *, ref: str = "") -> str | None:
        """One file's text, or None when it is not there.

        Absence is a normal answer — most repositories have no
        ``requirements/test.txt`` — so it is not an error. A file too large for
        the contents API is also None: those are lockfiles and vendored blobs,
        never the small manifests we came for.
        """
        params = {"ref": ref} if ref else {}
        response = self._get(f"/repos/{repo}/contents/{path}", **params)
        if response.status_code in {403, 404}:
            return None
        if response.status_code != 200:
            raise GitHubError(f"could not read {repo}:{path}: {response.status_code}")
        payload = response.json()
        if isinstance(payload, list) or payload.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(payload.get("content", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    def list_paths(self, repo: str, *, ref: str = "HEAD") -> list[str]:
        """Every path in the tree. One request, so it can be asked casually.

        A truncated response is returned as-is: it means a very large repository,
        and everything we look for — manifests, a tests directory — is near the
        root and comes back in the first pages anyway.
        """
        response = self._get(f"/repos/{repo}/git/trees/{ref}", recursive="1")
        if response.status_code != 200:
            return []
        payload = response.json()
        if payload.get("truncated"):
            log.info("%s tree truncated; judging from what came back", repo)
        return [item["path"] for item in payload.get("tree", []) if "path" in item]

    def fork(self, repo: str, *, organization: str = "") -> str:
        """Fork into our organisation. Returns the new ``owner/name``.

        The only method here that changes anything on GitHub, and the only one a
        human has to have approved a list for.
        """
        body: dict[str, Any] = {"organization": organization} if organization else {}
        response = self._client.post(f"/repos/{repo}/forks", json=body)
        if response.status_code not in {200, 202}:
            raise GitHubError(
                f"could not fork {repo}: {response.status_code} {response.text[:200]}"
            )
        return str(response.json().get("full_name", ""))


def iter_batched(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
