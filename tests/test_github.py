"""The GitHub client and the assessment built on it. The network is mocked.

Two things here are worth more than the coverage. First, the token must not be
reachable from a URL, a log line or an error message — the header is the only
place it lives. Second, absence must be an ordinary answer: most repositories
have no ``requirements/test.txt``, and a client that raised on that would make
a normal repository look like a broken one.
"""

from __future__ import annotations

import base64

import httpx
import pytest
from scripts.build_fork_pool import assess

from nightshift_core.github import GitHubClient, GitHubError, RateLimited, RepoMetadata

REPO_JSON = {
    "full_name": "org/service",
    "stargazers_count": 412,
    "license": {"spdx_id": "MIT"},
    "archived": False,
    "fork": False,
    "default_branch": "main",
    "pushed_at": "2026-08-01T00:00:00Z",
}

REQUIREMENTS = "django==3.2.1\nrequests==2.19.0\nurllib3==1.24.1\npyyaml>=5.1\n"


def _client(handler: object) -> GitHubClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return GitHubClient(
        "secret-token",
        client=httpx.Client(base_url="https://api.github.com", transport=transport),
        pause=0,
    )


def _contents(text: str) -> dict[str, str]:
    return {
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
    }


# --------------------------------------------------------------------------- #
# The token
# --------------------------------------------------------------------------- #


def test_the_token_travels_in_a_header_and_nowhere_else() -> None:
    """Not in the URL, where it would end up in a shell history or a log."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REPO_JSON)

    _client(handler).get_repo("org/service")
    assert seen[0].headers["authorization"] == "Bearer secret-token"
    assert "secret-token" not in str(seen[0].url)


def test_no_token_still_reads() -> None:
    """Search works unauthenticated, just slowly. Building a pool is a one-off."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json=REPO_JSON)

    transport = httpx.MockTransport(handler)
    client = GitHubClient(
        client=httpx.Client(base_url="https://api.github.com", transport=transport), pause=0
    )
    assert client.get_repo("org/service") is not None


# --------------------------------------------------------------------------- #
# Absence is an answer
# --------------------------------------------------------------------------- #


def test_a_missing_file_is_none_not_an_error() -> None:
    """Most repositories have no requirements/test.txt."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    assert _client(handler).get_file("org/service", "requirements/test.txt") is None


def test_a_missing_repository_is_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    assert _client(handler).get_repo("org/gone") is None


def test_a_directory_is_not_a_file() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"name": "base.txt"}])

    assert _client(handler).get_file("org/service", "requirements") is None


def test_a_file_that_is_not_utf8_is_skipped_rather_than_crashing_the_scan() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"encoding": "base64", "content": base64.b64encode(b"\xff\xfe").decode()}
        )

    assert _client(handler).get_file("org/service", "requirements.txt") is None


def test_an_unreadable_tree_is_an_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "Git Repository is empty."})

    assert _client(handler).list_paths("org/empty") == []


# --------------------------------------------------------------------------- #
# Rate limits say wait, not retry harder
# --------------------------------------------------------------------------- #


def test_a_rate_limit_is_reported_as_something_to_wait_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "API rate limit exceeded for user"})

    with pytest.raises(RateLimited, match="waiting is the correct response"):
        _client(handler).get_repo("org/service")


def test_search_failure_is_loud() -> None:
    """A search that silently returned nothing would look like an empty world."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Validation Failed"})

    with pytest.raises(GitHubError, match="search failed"):
        _client(handler).search_repositories("language:python")


def test_search_stops_at_the_limit_it_was_given() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        pages.append(page)
        start = (page - 1) * 100
        return httpx.Response(
            200,
            json={
                "items": [
                    {**REPO_JSON, "full_name": f"org/repo{n}"}
                    for n in range(start, start + 100)
                ]
            },
        )

    found = _client(handler).search_repositories("language:python", limit=150)
    assert len(found) == 150
    assert pages == [1, 2]


def test_search_stops_when_the_results_run_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    assert _client(handler).search_repositories("language:python", limit=50) == []


# --------------------------------------------------------------------------- #
# Assessment
# --------------------------------------------------------------------------- #


def _assessment_handler(paths: list[str], files: dict[str, str]) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/git/trees/" in path:
            return httpx.Response(200, json={"tree": [{"path": p} for p in paths]})
        for name, text in files.items():
            if path.endswith(f"/contents/{name}"):
                return httpx.Response(200, json=_contents(text))
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


def test_an_application_is_assessed_from_two_small_requests() -> None:
    client = _client(
        _assessment_handler(
            ["requirements.txt", "tests/test_views.py", "app/__init__.py"],
            {"requirements.txt": REQUIREMENTS},
        )
    )
    candidate = assess(client, RepoMetadata.from_api(REPO_JSON))

    assert candidate.repo == "org/service"
    assert candidate.has_tests
    # Three exact pins; `pyyaml>=5.1` is a range and is not counted.
    assert candidate.pinned_dependencies == 3
    assert candidate.manifests == ("requirements.txt",)


def test_a_library_assesses_as_having_nothing_to_scan() -> None:
    """The lesson from the first probe run, encoded where selection can see it."""
    client = _client(
        _assessment_handler(
            ["pyproject.toml", "tests/test_it.py"],
            {"pyproject.toml": '[project]\ndependencies = ["requests>=2.0", "click~=8.0"]\n'},
        )
    )
    candidate = assess(client, RepoMetadata.from_api(REPO_JSON))
    assert candidate.pinned_dependencies == 0
    assert candidate.manifests == ()


def test_a_repository_with_no_test_directory_is_noticed() -> None:
    client = _client(
        _assessment_handler(["requirements.txt"], {"requirements.txt": REQUIREMENTS})
    )
    assert not assess(client, RepoMetadata.from_api(REPO_JSON)).has_tests


def test_a_nested_test_directory_still_counts() -> None:
    client = _client(
        _assessment_handler(
            ["src/pkg/tests/test_a.py", "requirements.txt"], {"requirements.txt": REQUIREMENTS}
        )
    )
    assert assess(client, RepoMetadata.from_api(REPO_JSON)).has_tests


def test_the_licence_and_archive_state_come_from_the_metadata() -> None:
    meta = RepoMetadata.from_api({**REPO_JSON, "archived": True, "license": {"spdx_id": "GPL-3.0"}})
    client = _client(_assessment_handler([], {}))
    candidate = assess(client, meta)
    assert candidate.archived
    assert candidate.license_id == "GPL-3.0"


def test_a_repository_with_no_licence_field_does_not_crash() -> None:
    meta = RepoMetadata.from_api({**REPO_JSON, "license": None})
    assert meta.license_id == ""


# --------------------------------------------------------------------------- #
# Pagination is not stable, so search must deduplicate
# --------------------------------------------------------------------------- #


def test_repeated_results_across_pages_are_returned_once() -> None:
    """GitHub's search pagination is not stable when the sort key has ties, and
    star counts tie constantly. Measured on a real run: of 150 results about a
    third were repeats, and each cost two requests to assess a second time."""
    pages = {
        1: [{**REPO_JSON, "full_name": f"org/{n}"} for n in ("a", "b", "c")],
        2: [{**REPO_JSON, "full_name": f"org/{n}"} for n in ("b", "c", "d")],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json={"items": pages.get(page, [])})

    found = _client(handler).search_repositories("language:python", limit=10)
    assert [m.full_name for m in found] == ["org/a", "org/b", "org/c", "org/d"]


def test_a_page_of_nothing_but_repeats_ends_the_search() -> None:
    """Otherwise paging loops over the same repositories until the limit."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(int(request.url.params.get("page", 1)))
        return httpx.Response(200, json={"items": [REPO_JSON]})

    found = _client(handler).search_repositories("language:python", limit=50)
    assert len(found) == 1
    assert calls == [1, 2]


# --------------------------------------------------------------------------- #
# Rate limits, all three ways GitHub says it
# --------------------------------------------------------------------------- #


def test_a_secondary_limit_answers_429_and_must_not_be_missed() -> None:
    """The dangerous one. Earlier this was simply "not 200", so `list_paths`
    returned an empty list and the repository was assessed as having no tests
    and no dependencies — rejected for a reason nobody ever checked."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "You have exceeded a secondary rate limit"})

    with pytest.raises(RateLimited):
        _client(handler).list_paths("org/service")


def test_a_refused_tree_is_never_reported_as_a_repository_without_tests() -> None:
    """Silent bad data is worse than no data."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "secondary rate limit"})

    with pytest.raises(RateLimited):
        assess(_client(handler), RepoMetadata.from_api(REPO_JSON))


def test_retry_after_is_carried_so_the_caller_knows_how_long() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, headers={"retry-after": "60"}, json={"message": "API rate limit exceeded"}
        )

    with pytest.raises(RateLimited) as excinfo:
        _client(handler).get_repo("org/service")
    assert excinfo.value.retry_after_seconds == 60
    assert "retry after 60s" in str(excinfo.value)


def test_the_message_names_the_missing_token() -> None:
    """The commonest cause by far, and the one with a one-line fix."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "API rate limit exceeded"})

    with pytest.raises(RateLimited, match="GITHUB_TOKEN"):
        _client(handler).get_repo("org/service")


def test_an_ordinary_403_is_not_a_rate_limit() -> None:
    """A private repository or a token without scope is a per-repository
    problem; treating it as a quota exhaustion would abandon the whole run."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    assert _client(handler).get_file("org/private", "requirements.txt") is None


def test_a_rate_limit_stops_the_run_rather_than_skipping_a_repository() -> None:
    """The quota is gone for everything that follows. Continuing spends a
    hundred more requests learning nothing."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if len(seen) > 2:
            return httpx.Response(429, json={"message": "secondary rate limit"})
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"tree": [{"path": "requirements.txt"}]})
        return httpx.Response(200, json=_contents(REQUIREMENTS))

    client = _client(handler)
    metas = [RepoMetadata.from_api({**REPO_JSON, "full_name": f"org/r{n}"}) for n in range(10)]
    assessed = []
    with pytest.raises(RateLimited):
        for meta in metas:
            assessed.append(assess(client, meta))

    assert len(assessed) == 1, "the run must stop, not carry on through the whole list"


def test_forking_without_an_organisation_omits_the_field() -> None:
    """GitHub expresses "my own account" by leaving the field out, not by
    naming a user. Sending an empty organisation would be rejected."""
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return httpx.Response(202, json={"full_name": "me/service"})

    assert _client(handler).fork("org/service") == "me/service"
    assert bodies[0] in (b"{}", b"null", b"")


def test_forking_into_an_organisation_names_it() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return httpx.Response(202, json={"full_name": "ns-fleet/service"})

    _client(handler).fork("org/service", organization="ns-fleet")
    assert b"ns-fleet" in bodies[0]


def test_whoami_names_the_account_the_token_belongs_to() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"login": "varshavia"})

    assert _client(handler).whoami() == "varshavia"


def test_whoami_says_what_is_missing_when_it_cannot_ask() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Requires authentication"})

    with pytest.raises(GitHubError, match="token"):
        _client(handler).whoami()
