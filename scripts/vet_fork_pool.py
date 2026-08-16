"""Find out which forks are usable, before a night is spent finding out.

Test environment setup is the real difficulty in this project, not the agent. A
large fraction of third-party repositories will not build, or arrive with a
suite that was already failing. That is expected and it is modelled as a
first-class outcome — but it is much cheaper to discover it here, once, than
inside a Cloud Run worker at three in the morning.

For each fork this records: does the environment build, does the suite pass
untouched, how long does it take. Repositories that fail are not deleted from
the pool — they stay in it, labelled, because ``UNBUILDABLE`` and
``BASELINE_RED`` are numbers we report rather than numbers we hide.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VetResult:
    repo: str
    builds: bool
    baseline_green: bool
    test_seconds: float
    notes: str = ""


def vet(repo: str) -> VetResult:
    """Clone, build, run the suite once. No changes are made to the fork."""
    raise NotImplementedError("scripts: vet")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--out", default="fork_pool_vetting.json")
    args = parser.parse_args(argv)
    raise NotImplementedError(f"scripts: vet_fork_pool for org {args.org} -> {args.out}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
