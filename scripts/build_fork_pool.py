"""Build the fork pool, in two steps with a human in the middle.

    python scripts/build_fork_pool.py propose --out fleet/candidates.json
    #  ... a person reads it, sets keep=false on anything they object to ...
    python scripts/build_fork_pool.py fork --from fleet/candidates.json --org my-org

The split is the point. ``propose`` only reads: it searches, assesses, and writes
a file with a reason attached to every acceptance and every rejection. ``fork``
only acts, and only on a file that already exists — it never searches. So the set
of repositories this fleet touches is always something a person has read, which
is what ADR 0002 and RESPONSIBLE_USE.md commit us to.

The selection rule lives in :mod:`nightshift_core.fleet` and is tested there.
What lives here is the part that talks to GitHub.

**We are looking for applications, not libraries.** Libraries declare ranges —
correctly — and a range has no installed version to ask OSV about. The first
probe run measured this: two of four well-maintained libraries came back
NOT_AFFECTED because they had no exact pins at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

# See the note in probe_fleet.py. This script happens to work without it when
# the package is pip-installed, which is precisely why it is here: the three
# scripts should not each behave differently depending on how the checkout was
# set up.
if __package__ in {None, ""}:  # pragma: no cover - depends on how it was invoked
    _root = Path(__file__).resolve().parent.parent
    sys.path[:0] = [str(_root), str(_root / "packages")]

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from nightshift_core.config import load_env_file
from nightshift_core.fleet import (
    BUILD_TOOLING,
    Candidate,
    FleetEntry,
    FleetPool,
    load_pool,
    propose,
    save_pool,
)
from nightshift_core.github import (
    GitHubClient,
    GitHubError,
    RateLimited,
    RepoMetadata,
    WrongTokenType,
)
from nightshift_core.manifests import RECOGNISED_MANIFESTS, parse_manifest
from nightshift_core.models import Dependency, Vulnerability, consolidate_upgrades
from nightshift_core.osv import OSVClient

log = logging.getLogger("nightshift.forkpool")

#: Retuned after the first real run, which assessed 60 repositories and produced
#: seven usable ones — and every one of those seven was too big to run.
#:
#: Sorting the whole of GitHub by stars returns the internet's most-starred
#: Python repositories, and those are awesome-lists, books and tutorials (22 of
#: 53 rejections were licences like CC-BY: content, not code) or enormous
#: frameworks. What the fleet needs is the unglamorous middle: real applications
#: with pinned requirements and a suite that finishes.
#:
#: So the star count is bounded at both ends rather than only below, and size is
#: capped. `pushed` is a proxy for the suite having been run recently enough to
#: be green.
#:
#: Retuned a second time, after probing the six repositories the previous query
#: produced. Not one reached the measurement. The list it returned was almost
#: entirely machine learning, because sorting `language:python` by stars returns
#: the ecosystem where the stars are, and those repositories are the worst
#: possible fit: multi-gigabyte installs, suites that download model weights,
#: some that will not import without a GPU.
#:
#: Excluding the topics is cheap and does most of the work. It is a blunt
#: instrument — a web application tagged `machine-learning` is lost with them —
#: and that is an acceptable trade when the pool only needs a few dozen members
#: out of the several hundred thousand repositories that match the rest.
DEFAULT_QUERY = (
    "language:python stars:100..3000 size:<50000 pushed:>2025-06-01 archived:false "
    "-topic:machine-learning -topic:deep-learning -topic:pytorch -topic:tensorflow "
    "-topic:llm -topic:awesome -topic:tutorial"
)

#: An alternative worth running alongside the default rather than instead of it.
#:
#: The default excludes what we do not want; this one asks for what we do. A
#: repository that depends on a web framework is an application almost by
#: definition — it pins, it has a suite that runs on CPU in seconds, and its
#: dependencies are exactly the ones advisories are published against.
#: Three searches rather than one, because GitHub cannot express this as one.
#: ``OR`` applies to free text only; between qualifiers it is a validation error
#: — "The search contains only logical operators without any search terms" —
#: and the qualifiers in a single query are always ANDed, so `topic:flask
#: topic:django` asks for repositories that are somehow both. The union has to
#: be assembled on our side.
_APPLICATION_BASE = "language:python stars:50..3000 size:<30000 pushed:>2025-06-01 archived:false"
APPLICATION_QUERIES: tuple[str, ...] = tuple(
    f"{_APPLICATION_BASE} topic:{topic}" for topic in ("flask", "django", "fastapi")
)

#: The band the other two queries exclude by construction.
#:
#: Both of them ask for `pushed:>2025-06-01`, and a repository that has been
#: touched in the last three months keeps its dependencies current. A dependency
#: that is current has no advisory whose only fix is a major version away, and an
#: upgrade that moves a patch release does not break a suite. Eleven repositories
#: were upgraded in one night and not one of them broke — which is not the fleet
#: working, it is the fleet never being asked the question it exists to answer.
#:
#: What breaks has the opposite shape: untouched long enough for the ecosystem to
#: move underneath it, and small and pure enough to still build. `jinja2 2.11`,
#: the only repair this project has ever performed, is exactly that — one import
#: that moved to another package between two majors.
#:
#: Never archived: an archived repository cannot receive a pull request, so a
#: repair against one is a repair nobody can accept. Smaller than either other
#: band, because dormant and large is the combination that does not install —
#: the pinned world it was written for is no longer on PyPI in that shape.
#:
#: Two windows rather than one date range, because GitHub sorts by stars within
#: a query and a single wide window returns the same few hundred famous
#: abandoned projects for both halves of it.
_DORMANT_EXCLUSIONS = (
    "-topic:machine-learning -topic:deep-learning -topic:awesome -topic:tutorial"
)
DORMANT_QUERIES: tuple[str, ...] = tuple(
    f"language:python stars:30..2000 size:<8000 pushed:{window} archived:false "
    f"{_DORMANT_EXCLUSIONS}"
    for window in ("2022-06-01..2023-09-01", "2023-09-01..2024-12-01")
)

#: Path shapes that mean a machine can find the suite.
_TEST_MARKERS = ("tests/", "test/", "conftest.py")


def assess(
    client: GitHubClient, meta: RepoMetadata, *, osv: OSVClient | None = None
) -> Candidate:
    """Read enough of a repository to decide, without cloning it.

    Two or three small requests: the tree once, then each manifest the tree says
    exists. Cloning several hundred repositories to count ``==`` signs would be
    the same answer for a great deal more bandwidth.

    When ``osv`` is supplied the pins are also checked against the advisory
    database — one batched request per repository, no clone, no model. This is
    the difference between a pool of repositories we *could* work on and a pool
    of repositories there is work to do on.
    """
    paths = client.list_paths(meta.full_name, ref=meta.default_branch or "HEAD")
    has_tests = any(
        path.startswith(_TEST_MARKERS) or "/tests/" in path or path.endswith("/conftest.py")
        for path in paths
    )

    present = [name for name in RECOGNISED_MANIFESTS if name in paths]
    dependencies: list[Dependency] = []
    with_pins: list[str] = []
    for name in present:
        text = client.get_file(meta.full_name, name, ref=meta.default_branch)
        if not text:
            continue
        parsed = parse_manifest(text, name)
        if parsed:
            dependencies.extend(parsed)
            with_pins.append(name)

    checked = False
    advisories: list[Vulnerability] = []
    if osv is not None and dependencies:
        try:
            advisories = consolidate_upgrades(osv.find_vulnerabilities(dependencies))
            checked = True
        except Exception as exc:
            # Deliberately not fatal and deliberately not counted as zero. An
            # outage on our side must never become "this repository has nothing
            # wrong with it" — that is the same mistake as reading a refused
            # tree request as "no tests".
            log.warning("could not check advisories for %s: %s", meta.full_name, exc)

    jumps = tuple(
        f"{v.package} {v.installed_version} -> {v.fixed_version}" for v in advisories
    )
    return Candidate(
        repo=meta.full_name,
        stars=meta.stars,
        license_id=meta.license_id,
        archived=meta.archived,
        fork=meta.fork,
        has_tests=has_tests,
        size_kb=meta.size_kb,
        pinned_dependencies=len(dependencies),
        manifests=tuple(with_pins),
        advisories_checked=checked,
        actionable_advisories=len(advisories),
        advisory_packages=tuple(dict.fromkeys(v.package for v in advisories)),
        advisory_jumps=jumps,
        major_jump_advisories=sum(
            1
            for v in advisories
            if str(canonicalize_name(v.package)) not in BUILD_TOOLING
            and _crosses_a_major(v.installed_version, v.fixed_version)
        ),
    )


def _crosses_a_major(installed: str, fixed: str | None) -> bool:
    """Whether the only published fix is a major version away.

    Unparseable versions answer False rather than raising. A survey is not the
    place to discover that one advisory in four hundred carries a date where a
    version should be, and guessing "probably breaking" about a version nobody
    can compare would put noise at the top of the list — which is the one place
    noise costs the most.
    """
    if not fixed:
        return False
    try:
        return Version(fixed).major > Version(installed).major
    except InvalidVersion:
        return False


# --------------------------------------------------------------------------- #
# propose — reads only
# --------------------------------------------------------------------------- #


def run_propose(args: argparse.Namespace, token: str) -> int:
    osv = None if args.no_advisories else OSVClient()
    queries: list[str] = args.query if isinstance(args.query, list) else [args.query]
    with GitHubClient(token) as client:
        # Several searches merged into one survey. Deduplicated across queries as
        # well as within them: a Django project tagged `fastapi` too would
        # otherwise be assessed twice, forked twice, and counted twice in every
        # number computed over the pool.
        found: list[RepoMetadata] = []
        seen: set[str] = set()
        for query in queries:
            log.info("searching: %s", query)
            for meta in client.search_repositories(query, limit=args.search):
                if meta.full_name not in seen:
                    seen.add(meta.full_name)
                    found.append(meta)
            if len(found) >= args.search:
                break
        found = found[: args.search]
        log.info("assessing %d repositories", len(found))
        candidates: list[Candidate] = []
        truncated = False
        for index, meta in enumerate(found, start=1):
            try:
                candidates.append(assess(client, meta, osv=osv))
            except RateLimited as exc:
                # Stop the whole run rather than skipping this one. The quota is
                # gone for everything that follows, and continuing would spend a
                # hundred more requests learning nothing — while recording every
                # remaining repository as having no tests and no pins.
                log.error("%s", exc)
                truncated = True
                break
            except GitHubError as exc:
                log.warning("skipping %s: %s", meta.full_name, exc)
            if index % 10 == 0:
                log.info("  %d/%d", index, len(found))

    if osv is not None:
        osv.close()

    accepted, rejected = propose(candidates)
    # Most advisories first, so that `--limit` keeps the repositories with the
    # most for the fleet to do rather than whichever GitHub happened to sort
    # highest by stars.
    # Ranked on advisories against something the repository calls, not on the
    # raw count: apiflask advertised three and every one was build tooling.
    # Ordered by what we are actually hunting: advisories whose only fix is a
    # major version away. Two repositories reached the measurement on the raw
    # count and both came back CLEAN, because OSV answers with the lowest
    # fixed version and that is usually a patch release.
    accepted.sort(
        key=lambda c: (-c.likely_to_break, -c.call_path_advisories, -c.actionable_advisories)
    )
    accepted = accepted[: args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "query": queries,
                "assessed": len(candidates),
                "requested": len(found),
                # A run cut short by a rate limit surveyed part of the world.
                # Recorded so that a short list is not mistaken for a thorough
                # search that found little.
                "truncated": truncated,
                "accepted": [
                    {
                        "repo": c.repo,
                        "stars": c.stars,
                        "license_id": c.license_id,
                        "pinned_dependencies": c.pinned_dependencies,
                        "size_kb": c.size_kb,
                        "manifests": list(c.manifests),
                        "actionable_advisories": c.actionable_advisories,
                        "call_path_advisories": c.call_path_advisories,
                        "major_jump_advisories": c.major_jump_advisories,
                        "advisory_jumps": list(c.advisory_jumps),
                        "advisory_packages": list(c.advisory_packages),
                        "keep": True,
                    }
                    for c in accepted
                ],
                # Rejections stay in the same file on purpose: a reviewer asking
                # "why is this project not in here" should not have to rerun
                # anything to find out.
                "rejected": [{"repo": repo, "reason": reason} for repo, reason in rejected],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if truncated:
        print(
            f"\nSTOPPED EARLY: assessed {len(candidates)} of {len(found)} before the rate "
            "limit. This proposal is a partial survey, not a thorough one.",
            file=sys.stderr,
        )
    print(f"\nassessed {len(candidates)}, proposing {len(accepted)}")
    for candidate in accepted[:20]:
        print(
            f"  {candidate.repo:<40} {candidate.likely_to_break:>2} major jumps"
            f"  {candidate.call_path_advisories:>3} on the call path"
            f"  ({candidate.actionable_advisories:>2} total)"
        )
    if len(accepted) > 20:
        print(f"  ... and {len(accepted) - 20} more")
    print(f"\nwritten to {out}")
    print('Read it. Set "keep": false on anything you would not want us to touch, then:')
    print(f"  build_fork_pool.py fork --from {out} --org <your-org>")
    return 0


# --------------------------------------------------------------------------- #
# fork — acts only, and only on what was read
# --------------------------------------------------------------------------- #


def run_fork(args: argparse.Namespace, token: str) -> int:
    source = Path(args.source)
    if not source.is_file():
        print(f"no proposal at {source}; run `propose` first", file=sys.stderr)
        return 2

    proposal = json.loads(source.read_text(encoding="utf-8"))
    keeping = [entry for entry in proposal.get("accepted", []) if entry.get("keep", False)]
    if not keeping:
        print("nothing marked keep=true; nothing to fork", file=sys.stderr)
        return 1

    existing = load_pool(args.pool) if Path(args.pool).is_file() else FleetPool()
    already = set(existing.repos)

    entries: list[FleetEntry] = []
    with GitHubClient(token) as client:
        # Resolved once. Without an organisation the forks land in the token
        # holder's account, and we need its name to recognise repositories the
        # pool already has.
        owner = args.org or client.whoami()
        if not owner:
            print("could not determine where to fork to", file=sys.stderr)
            return 2
        log.info("forking into %s", owner)

        for entry in keeping:
            upstream = str(entry["repo"])
            target = f"{owner}/{upstream.split('/')[1]}"
            if target in already:
                log.info("%s is already in the pool", target)
                continue
            if args.dry_run:
                print(f"would fork {upstream} -> {target}")
                continue
            try:
                forked = client.fork(upstream, organization=args.org)
            except WrongTokenType as exc:
                # Stop rather than carry on. This is a property of the token, so
                # every remaining repository produces the identical refusal, and
                # six copies of it bury the one sentence that says what to do.
                print(f"\n{exc}", file=sys.stderr)
                break
            except GitHubError as exc:
                log.warning("could not fork %s: %s", upstream, exc)
                continue
            print(f"forked {upstream} -> {forked}")
            entries.append(
                FleetEntry(
                    repo=forked or target,
                    upstream=upstream,
                    license_id=str(entry.get("license_id", "")),
                    stars=int(entry.get("stars", 0)),
                    pinned_dependencies=int(entry.get("pinned_dependencies", 0)),
                    manifests=tuple(entry.get("manifests", ())),
                    size_kb=int(entry.get("size_kb", 0)),
                )
            )

    if entries:
        save_pool(existing.merged_with(entries), args.pool)
        print(f"\npool now holds {len(existing.repos) + len(entries)} repositories: {args.pool}")
    return 0


# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    proposer = sub.add_parser("propose", help="search and assess; writes a file, forks nothing")
    proposer.add_argument("--query", default=DEFAULT_QUERY)
    proposer.add_argument(
        "--applications",
        action="store_const",
        const=list(APPLICATION_QUERIES),
        dest="query",
        help="three searches — flask, django, fastapi — merged; asks for what we want "
        "rather than excluding what we do not",
    )
    proposer.add_argument(
        "--dormant",
        action="store_const",
        const=list(DORMANT_QUERIES),
        dest="query",
        help="repositories last touched between one and three years ago: the only "
        "population whose advisories are old enough for the fix to be a major "
        "version away, and therefore the only one where an upgrade breaks anything",
    )
    proposer.add_argument("--search", type=int, default=200, help="how many to assess")
    proposer.add_argument("--limit", type=int, default=50, help="how many to propose")
    proposer.add_argument("--out", default="fleet/candidates.json")
    proposer.add_argument(
        "--no-advisories",
        action="store_true",
        help="skip the OSV check; faster, but proposes repositories with nothing to fix",
    )

    forker = sub.add_parser("fork", help="fork what a human marked keep=true")
    forker.add_argument("--from", dest="source", default="fleet/candidates.json")
    forker.add_argument(
        "--org",
        default="",
        help="organisation the forks will live in; omit to fork to your own account",
    )
    forker.add_argument("--pool", default="fleet/pool.json")
    forker.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Reads .env if there is one, so the token does not have to be exported into
    # every new shell. A variable already in the environment still wins.
    load_env_file()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        if args.command == "fork":
            print(
                "GITHUB_TOKEN is not set, and forking needs it.\n"
                "  Put it in a .env file at the repository root:\n"
                "    GITHUB_TOKEN=github_pat_...\n"
                "  .env is gitignored and is read automatically from now on.",
                file=sys.stderr,
            )
            return 2
        print(
            "GITHUB_TOKEN is not set. Search works without one but is rate-limited to "
            "60 requests an hour, which runs out after about thirty repositories.",
            file=sys.stderr,
        )

    return run_propose(args, token) if args.command == "propose" else run_fork(args, token)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
