"""Build the fork pool the fleet operates on.

The fleet never points itself at arbitrary repositories. It works on forks in an
organisation we control, created deliberately from a reviewed list. This script
is how that list becomes forks; ``vet_fork_pool.py`` is how we find out which of
them are usable before a night is spent discovering it one repository at a time.

Selection criteria, applied in this order:

1. Permissive licence (MIT, BSD, Apache-2.0) — we are copying and modifying code.
2. A Python test suite that a machine can find and run.
3. Pinned dependencies. An unpinned manifest has no version to compare to OSV.
4. At least one advisory affecting a pin. Repositories with nothing to fix add
   nothing but cost.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Candidate:
    repo: str
    stars: int
    license_id: str
    has_tests: bool
    pinned_manifests: tuple[str, ...]


def search_candidates(limit: int) -> Sequence[Candidate]:
    """Find plausible repositories through the GitHub search API."""
    raise NotImplementedError("scripts: search_candidates")


def fork(repo: str, into_org: str) -> str:
    """Fork one repository into our organisation. Returns the new full name."""
    raise NotImplementedError("scripts: fork")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--org", required=True, help="organisation that will own the forks")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    candidates = search_candidates(args.limit)
    for candidate in candidates:
        if args.dry_run:
            print(f"would fork {candidate.repo}")
            continue
        print(fork(candidate.repo, args.org))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
