"""The zero-token probe. Runs the whole pipeline except the part that costs money.

For every repository: build it, run the suite untouched, apply the security
upgrade, run the suite again — and stop there. No model is called. What comes
out is three things we need before spending a cent of the cloud credit:

1. **The benchmark.** The repositories where a real advisory upgrade really
   breaks the calling code, discovered rather than authored. That set is the
   denominator the repair rate is computed over, and because it is produced by
   this script it is reproducible and re-derivable rather than curated by hand.

2. **The statistic the whole project rests on** — what fraction of security
   upgrades break the code that calls them. Nightshift exists because that
   number is not small. Until now we have asserted it; this measures it.

3. **Fleet sizing.** How many repositories must be scanned to find N breaking
   upgrades, which is what tells us whether the credit stretches to the run we
   want to demonstrate.

Usage::

    python scripts/probe_fleet.py --out benchmark/cases.json

It reads ``fleet/pool.json`` — the reviewed pool that ``build_fork_pool.py fork``
writes — so the probe surveys exactly the repositories a person signed off on
and nothing has to be retyped between the two steps. ``--repos`` still takes a
plain file of ``owner/name`` lines, which is how one repository gets probed on
its own while something is being debugged.

``ProbeVerdict`` is deliberately *not*
:class:`~nightshift_core.models.Outcome`. The probe never attempts a repair, so
it cannot produce ``PATCHED_REPAIRED`` or ``REPAIR_EXHAUSTED``, and stretching
the job enum to cover a different activity would weaken the guarantee that every
member of ``Outcome`` describes a finished repair job. See ADR 0003.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

# Run as a file — `python scripts/probe_fleet.py` — Python puts *this directory*
# on the import path and nothing else, so `services` is not importable. Whether
# `nightshift_core` is depends on whether someone ran `pip install -e .`, which
# is what makes the failure confusing: on one machine the first three imports
# succeed and the fourth does not, on another none of them do. Both roots go on
# the path, so the script behaves the same in a bare checkout as in a prepared
# environment. Requiring `python -m scripts.probe_fleet` would also work, but
# every person who runs this would learn that rule by tripping over it.
if __package__ in {None, ""}:  # pragma: no cover - depends on how it was invoked
    _root = Path(__file__).resolve().parent.parent
    sys.path[:0] = [str(_root), str(_root / "packages")]

from services.worker.toolchain import (
    EnvironmentBuildError,
    UpgradeError,
    apply_upgrade,
    build_environment,
    clone,
    read_dependencies,
    run_tests,
)

from nightshift_core.config import load_env_file
from nightshift_core.fleet import load_pool
from nightshift_core.models import Vulnerability, consolidate_upgrades
from nightshift_core.osv import OSVClient

log = logging.getLogger("nightshift.probe")

#: Format version for the emitted case file. Bumped when the schema changes, so
#: a benchmark run recorded weeks ago can still be read and compared.
CASES_SCHEMA = 1


class ProbeVerdict(StrEnum):
    """What the model-free pass found. One per repository."""

    #: The environment could not be built. Expected, and counted.
    UNBUILDABLE = "UNBUILDABLE"
    #: pytest failed on its own terms — internal error or bad invocation. Our
    #: fault, not the repository's, and kept separate so it cannot be quietly
    #: counted as a repository that arrived broken.
    PROBE_ERROR = "PROBE_ERROR"
    #: pytest collected nothing. A repository with no tests cannot be its own evidence.
    NO_TESTS = "NO_TESTS"
    #: There is a suite, and we could not assemble it — a missing settings module,
    #: fixtures wanting a database, a project driven by tox rather than pytest.
    #: Our limitation, stated as one, and deliberately not counted as the
    #: repository having arrived broken.
    SUITE_UNRUNNABLE = "SUITE_UNRUNNABLE"
    #: The suite ran and was already failing before we touched anything.
    BASELINE_RED = "BASELINE_RED"
    #: Vulnerable, but no published fix to upgrade to.
    NO_FIX_AVAILABLE = "NO_FIX_AVAILABLE"
    #: Nothing here is vulnerable. Not a finding, just a quiet repository.
    NOT_AFFECTED = "NOT_AFFECTED"
    #: The fixed version would not install, or the pin could not be rewritten.
    UPGRADE_FAILED = "UPGRADE_FAILED"
    #: Upgraded, suite still green. Dependabot would have been enough here.
    CLEAN = "CLEAN"
    #: Upgraded and the suite went red. **This is a benchmark case.**
    BREAKING = "BREAKING"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    repo: str
    verdict: ProbeVerdict
    upgrades: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()
    baseline_seconds: float = 0.0
    verify_seconds: float = 0.0
    failing_output: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["verdict"] = str(self.verdict)
        data["upgrades"] = list(self.upgrades)
        data["advisories"] = list(self.advisories)
        return data


@dataclass(slots=True)
class ProbeSummary:
    """The numbers, and the one that justifies the project."""

    counts: dict[str, int] = field(default_factory=dict)
    probed: int = 0
    #: Repositories where an upgrade was actually applied to a green baseline.
    upgrades_attempted: int = 0
    breaking: int = 0

    @property
    def break_rate(self) -> float | None:
        """Fraction of applied security upgrades that broke the calling code.

        ``None`` rather than zero when nothing was attempted: an empty run must
        not read as "upgrades never break anything".
        """
        if not self.upgrades_attempted:
            return None
        return self.breaking / self.upgrades_attempted


def summarise(results: Sequence[ProbeResult]) -> ProbeSummary:
    """Aggregate probe results. Pure, so the arithmetic is unit-testable."""
    counts = {str(verdict): 0 for verdict in ProbeVerdict}
    for result in results:
        counts[str(result.verdict)] += 1
    attempted = counts[str(ProbeVerdict.CLEAN)] + counts[str(ProbeVerdict.BREAKING)]
    return ProbeSummary(
        counts=counts,
        probed=len(results),
        upgrades_attempted=attempted,
        breaking=counts[str(ProbeVerdict.BREAKING)],
    )


def benchmark_cases(results: Sequence[ProbeResult]) -> list[dict[str, object]]:
    """The breaking repositories, in the shape the benchmark runner consumes."""
    return [result.to_dict() for result in results if result.verdict is ProbeVerdict.BREAKING]


# --------------------------------------------------------------------------- #
# Probing one repository
# --------------------------------------------------------------------------- #


def probe_one(
    repo: str, workspace: Path, osv: OSVClient, *, token: str | None = None
) -> ProbeResult:
    """Build, test, upgrade, test again. No model is called anywhere in here."""
    try:
        repo_path = clone(repo, workspace, token=token)
        sandbox = build_environment(repo_path)
    except EnvironmentBuildError as exc:
        # Generous, because this is the field that has to answer "why". 500
        # characters truncated exactly the part that mattered: the install log
        # came first and the actual error came last.
        return ProbeResult(repo=repo, verdict=ProbeVerdict.UNBUILDABLE, notes=str(exc)[:2500])

    baseline = run_tests(sandbox)
    if baseline.internal_error:
        return ProbeResult(
            repo=repo,
            verdict=ProbeVerdict.PROBE_ERROR,
            baseline_seconds=baseline.duration_seconds,
            notes=f"pytest exit {baseline.exit_code}; " + "; ".join(sandbox.install_log[-3:]),
        )
    if not baseline.collected:
        return ProbeResult(
            repo=repo,
            verdict=ProbeVerdict.NO_TESTS,
            baseline_seconds=baseline.duration_seconds,
        )
    if baseline.tests_collected == 0 and baseline.collection_errors:
        # Nothing at all could be imported. Before an upgrade that is our
        # environment, not their code, and it is kept out of BASELINE_RED so the
        # denominator is not padded with our own failures.
        #
        # A suite that yields *some* tests is not here: 107 usable tests behind
        # three modules wanting `dateutil` is a repository we can work with, and
        # discarding it was how twelve of twenty-four were lost.
        return ProbeResult(
            repo=repo,
            verdict=ProbeVerdict.SUITE_UNRUNNABLE,
            baseline_seconds=baseline.duration_seconds,
            failing_output=baseline.output,
            notes="collection failed before anything was changed; "
            + "; ".join(sandbox.install_log[-4:]),
        )
    # A suite is usable when *something* in it passes. Demanding a perfectly
    # green baseline sounds rigorous and is not: it discards a repository with a
    # hundred passing tests over one that fails for a reason particular to this
    # container, and it is why flask-jwt-extended — 106 passing, one failing on
    # an unavailable crypto backend — was thrown away.
    #
    # What replaces it is stricter where it counts. The failures that exist
    # before the upgrade are recorded by name and set aside; the break is what
    # the upgrade *changed*. A test that was red stays red without counting
    # against anything, and a test that was green and goes red is the finding,
    # which is a sharper instrument than a single pass-or-fail bit.
    already_failing = baseline.failures
    if baseline.tests_collected and len(already_failing) >= baseline.tests_collected:
        return ProbeResult(
            repo=repo,
            verdict=ProbeVerdict.BASELINE_RED,
            baseline_seconds=baseline.duration_seconds,
            # Recorded rather than discarded. Three times now a verdict has been
            # written down without the output that would explain it, and three
            # times the next question has been "why" with nothing to answer it.
            failing_output=baseline.output,
            notes=f"pytest exit {baseline.exit_code}; nothing in the suite passes",
        )

    dependencies = read_dependencies(repo_path)
    vulnerabilities: list[Vulnerability] = osv.find_vulnerabilities(dependencies)
    if not vulnerabilities:
        return ProbeResult(
            repo=repo,
            verdict=ProbeVerdict.NOT_AFFECTED,
            baseline_seconds=baseline.duration_seconds,
        )

    # One upgrade per package, not one per advisory. Four advisories against the
    # same pinned `black` asked pip for three versions of it at once and were
    # recorded as a repository that resisted being fixed.
    fixable = consolidate_upgrades(vulnerabilities)
    if not fixable:
        return ProbeResult(
            repo=repo,
            verdict=ProbeVerdict.NO_FIX_AVAILABLE,
            advisories=tuple(v.osv_id for v in vulnerabilities),
            baseline_seconds=baseline.duration_seconds,
        )

    upgrades = tuple(
        f"{v.package} {v.installed_version} -> {v.fixed_version}" for v in fixable
    )
    try:
        apply_upgrade(sandbox, fixable)
    except UpgradeError as exc:
        return ProbeResult(
            repo=repo,
            verdict=ProbeVerdict.UPGRADE_FAILED,
            upgrades=upgrades,
            advisories=tuple(v.osv_id for v in fixable),
            baseline_seconds=baseline.duration_seconds,
            notes=str(exc)[:500],
        )

    verified = run_tests(sandbox)
    # The break is the difference, not the state. Anything red before the
    # upgrade stays red without counting; anything that was green and is now red
    # is what we came to find — including a module that imported cleanly and now
    # does not, which is how an upgrade that removes a name announces itself,
    # before a single test has run.
    newly_failing = verified.failures - already_failing
    still_green = not newly_failing
    return ProbeResult(
        repo=repo,
        verdict=ProbeVerdict.CLEAN if still_green else ProbeVerdict.BREAKING,
        upgrades=upgrades,
        advisories=tuple(v.osv_id for v in fixable),
        baseline_seconds=baseline.duration_seconds,
        verify_seconds=verified.duration_seconds,
        failing_output="" if still_green else verified.output,
        notes=(
            f"{baseline.tests_collected} tests at baseline, "
            f"{len(already_failing)} already failing"
            + (
                ""
                if still_green
                else "; newly failing: " + ", ".join(sorted(newly_failing)[:12])
            )
        ),
    )


def probe_fleet(
    repos: Sequence[str], *, workspace_root: Path | None = None, token: str | None = None
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    root = workspace_root or Path(tempfile.mkdtemp(prefix="nightshift-probe-"))
    with OSVClient() as osv:
        for index, repo in enumerate(repos, start=1):
            started = time.monotonic()
            workspace = root / repo.replace("/", "_")
            try:
                result = probe_one(repo, workspace, osv, token=token)
            # Broad on purpose: one bad repository must not end a fleet-wide run.
            # The verdict is PROBE_ERROR, never UNBUILDABLE — an OSV outage or a
            # network fault is our problem, and recording it against the
            # repository would understate how much of the fleet is usable. That
            # estimate is what the cloud budget is sized from.
            except Exception as exc:
                log.exception("probe of %s raised", repo)
                result = ProbeResult(
                    repo=repo,
                    verdict=ProbeVerdict.PROBE_ERROR,
                    notes=f"{type(exc).__name__}: {exc}"[:500],
                )
            results.append(result)
            log.info(
                "[%d/%d] %s -> %s (%.0fs)",
                index, len(repos), repo, result.verdict, time.monotonic() - started,
            )
            shutil.rmtree(workspace, ignore_errors=True)
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--pool",
        default="fleet/pool.json",
        help="the reviewed fork pool; the default, and what the fleet actually runs on",
    )
    source.add_argument("--repos", help="a plain file with one owner/name per line")
    parser.add_argument("--out", default="benchmark/cases.json")
    parser.add_argument("--limit", type=int, default=0, help="0 means no limit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    load_env_file()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # The pool is the default source because it is the one list a person has
    # read and signed off. A plain file still works — it is how a single
    # repository gets probed while debugging — but nothing should have to be
    # retyped to get from `fork` to here, and a retyped list is a list nobody
    # reviewed.
    if args.repos:
        repos = [
            line.strip()
            for line in Path(args.repos).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    else:
        pool_path = Path(args.pool)
        if not pool_path.is_file():
            print(
                f"no fork pool at {pool_path}. Build one first:\n"
                "  python scripts/build_fork_pool.py propose --out fleet/candidates.json\n"
                "  ... read it, then ...\n"
                "  python scripts/build_fork_pool.py fork --from fleet/candidates.json",
                file=sys.stderr,
            )
            return 2
        repos = load_pool(pool_path).repos

    if not repos:
        print("the pool is empty; nothing to probe", file=sys.stderr)
        return 1
    if args.limit:
        repos = repos[: args.limit]

    results = probe_fleet(repos)
    summary = summarise(results)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "schema": CASES_SCHEMA,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "summary": {
                    "probed": summary.probed,
                    "counts": summary.counts,
                    "upgrades_attempted": summary.upgrades_attempted,
                    "breaking": summary.breaking,
                    "break_rate": summary.break_rate,
                },
                "cases": benchmark_cases(results),
                # Every repository, not only the breaking ones. The first real
                # run produced zero cases and a 439-byte file: six verdicts, and
                # not one word about why any of them happened, even though every
                # result carried a `notes` explaining itself. A run that finds
                # nothing is exactly the run whose reasons matter most — it is
                # the one that has to be diagnosed rather than reported.
                "results": [result.to_dict() for result in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\nprobed {summary.probed} repositories, no model called")
    for verdict, count in sorted(summary.counts.items(), key=lambda kv: -kv[1]):
        if count:
            print(f"  {verdict:<18} {count}")
    if summary.break_rate is not None:
        print(
            f"\n{summary.breaking} of {summary.upgrades_attempted} applied security upgrades "
            f"broke the calling code ({summary.break_rate:.0%})"
        )
    else:
        # Not the same as "upgrades never break anything", and the difference is
        # the whole point of the statistic.
        print(
            "\nno upgrade was applied to a green baseline, so the break rate is "
            "unmeasured rather than zero. The reasons are in the file."
        )

    # The funnel, which is a finding in its own right and the number the fleet is
    # sized from: how many repositories must be surveyed to reach one that can be
    # measured at all. Ours is far worse than anyone assumes before running it.
    ours = summary.counts[str(ProbeVerdict.SUITE_UNRUNNABLE)] + summary.counts[
        str(ProbeVerdict.PROBE_ERROR)
    ]
    theirs = (
        summary.counts[str(ProbeVerdict.UNBUILDABLE)]
        + summary.counts[str(ProbeVerdict.BASELINE_RED)]
        + summary.counts[str(ProbeVerdict.NO_TESTS)]
    )
    nothing_to_do = (
        summary.counts[str(ProbeVerdict.NOT_AFFECTED)]
        + summary.counts[str(ProbeVerdict.NO_FIX_AVAILABLE)]
    )
    print(
        f"\nfunnel: {summary.upgrades_attempted} of {summary.probed} reached the measurement"
        f"  ·  {ours} lost to our own limits"
        f"  ·  {theirs} to the repository's state"
        f"  ·  {nothing_to_do} had nothing to fix"
    )
    print(f"written to {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
