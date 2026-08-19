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

from nightshift_core.fleet import (
    Candidate,
    FleetEntry,
    FleetPool,
    load_pool,
    propose,
    save_pool,
)
from nightshift_core.github import GitHubClient, GitHubError, RepoMetadata
from nightshift_core.manifests import RECOGNISED_MANIFESTS, parse_manifest

log = logging.getLogger("nightshift.forkpool")

#: Applications, not libraries. Stars are a proxy for "somebody maintains this",
#: and the push date for "the test suite has been run this year".
DEFAULT_QUERY = "language:python stars:>100 pushed:>2025-01-01 archived:false"

#: Path shapes that mean a machine can find the suite.
_TEST_MARKERS = ("tests/", "test/", "conftest.py")


def assess(client: GitHubClient, meta: RepoMetadata) -> Candidate:
    """Read enough of a repository to decide, without cloning it.

    Two or three small requests: the tree once, then each manifest the tree says
    exists. Cloning several hundred repositories to count ``==`` signs would be
    the same answer for a great deal more bandwidth.
    """
    paths = client.list_paths(meta.full_name, ref=meta.default_branch or "HEAD")
    has_tests = any(
        path.startswith(_TEST_MARKERS) or "/tests/" in path or path.endswith("/conftest.py")
        for path in paths
    )

    present = [name for name in RECOGNISED_MANIFESTS if name in paths]
    pinned = 0
    with_pins: list[str] = []
    for name in present:
        text = client.get_file(meta.full_name, name, ref=meta.default_branch)
        if not text:
            continue
        count = len(parse_manifest(text, name))
        if count:
            pinned += count
            with_pins.append(name)

    return Candidate(
        repo=meta.full_name,
        stars=meta.stars,
        license_id=meta.license_id,
        archived=meta.archived,
        fork=meta.fork,
        has_tests=has_tests,
        pinned_dependencies=pinned,
        manifests=tuple(with_pins),
    )


# --------------------------------------------------------------------------- #
# propose — reads only
# --------------------------------------------------------------------------- #


def run_propose(args: argparse.Namespace, token: str) -> int:
    with GitHubClient(token) as client:
        log.info("searching: %s", args.query)
        found = client.search_repositories(args.query, limit=args.search)
        log.info("assessing %d repositories", len(found))
        candidates: list[Candidate] = []
        for index, meta in enumerate(found, start=1):
            try:
                candidates.append(assess(client, meta))
            except GitHubError as exc:
                log.warning("skipping %s: %s", meta.full_name, exc)
            if index % 10 == 0:
                log.info("  %d/%d", index, len(found))

    accepted, rejected = propose(candidates)
    accepted = accepted[: args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "query": args.query,
                "assessed": len(candidates),
                "accepted": [
                    {
                        "repo": c.repo,
                        "stars": c.stars,
                        "license_id": c.license_id,
                        "pinned_dependencies": c.pinned_dependencies,
                        "manifests": list(c.manifests),
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

    print(f"\nassessed {len(candidates)}, proposing {len(accepted)}")
    for candidate in accepted[:20]:
        print(f"  {candidate.repo:<45} {candidate.pinned_dependencies:>3} pins")
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
        for entry in keeping:
            upstream = str(entry["repo"])
            target = f"{args.org}/{upstream.split('/')[1]}"
            if target in already:
                log.info("%s is already in the pool", target)
                continue
            if args.dry_run:
                print(f"would fork {upstream} -> {target}")
                continue
            try:
                forked = client.fork(upstream, organization=args.org)
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
    proposer.add_argument("--search", type=int, default=200, help="how many to assess")
    proposer.add_argument("--limit", type=int, default=50, help="how many to propose")
    proposer.add_argument("--out", default="fleet/candidates.json")

    forker = sub.add_parser("fork", help="fork what a human marked keep=true")
    forker.add_argument("--from", dest="source", default="fleet/candidates.json")
    forker.add_argument("--org", required=True, help="organisation the forks will live in")
    forker.add_argument("--pool", default="fleet/pool.json")
    forker.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print(
            "GITHUB_TOKEN is not set. Search works without one but is rate-limited to a "
            "handful of requests, and forking needs it.",
            file=sys.stderr,
        )
        if args.command == "fork":
            return 2

    return run_propose(args, token) if args.command == "propose" else run_fork(args, token)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
