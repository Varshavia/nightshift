"""Run one repository end to end on this machine, with no cloud involved.

    make run-local REPO=owner/name

This is the loop the engine is developed against: no Pub/Sub, no Firestore, no
Cloud Run — a :class:`~nightshift_core.store.MemoryJobStore` and the same worker
code that runs at night. Block 1 is done when this produces a real pull request.
"""

from __future__ import annotations

import argparse
import logging
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path

from services.scanner.main import triage
from services.worker.main import handle
from services.worker.toolchain import clone, read_dependencies

from nightshift_core.config import get_settings
from nightshift_core.models import RepoJob
from nightshift_core.osv import OSVClient
from nightshift_core.store import MemoryJobStore


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    settings = get_settings()

    # Read the pins from a throwaway clone rather than through the GitHub
    # contents API. It costs a second locally and reuses `read_dependencies`,
    # which is already tested; the scanner's API-based read only earns its keep
    # in Block 2, where three hundred repositories must be read without cloning
    # any of them.
    with tempfile.TemporaryDirectory() as scratch:
        repo_path = clone(args.repo, Path(scratch), token=settings.github_token)
        dependencies = read_dependencies(repo_path)

    with OSVClient() as osv:
        vulnerabilities = list(triage(osv.find_vulnerabilities(dependencies)))

    if not vulnerabilities:
        print(f"{args.repo}: nothing to fix")
        return 0

    job = RepoJob(
        job_id=f"local-{uuid.uuid4().hex[:8]}:{args.repo}",
        repo=args.repo,
        vulnerabilities=vulnerabilities,
    )
    store = MemoryJobStore()
    store.put(job)

    finished = handle(job, store, settings)
    print(f"{finished.repo}: {finished.outcome} ({len(finished.repair_attempts)} repair attempts)")
    if finished.pr_url:
        print(finished.pr_url)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
