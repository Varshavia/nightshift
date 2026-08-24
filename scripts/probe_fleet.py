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

    python scripts/probe_fleet.py --repos fleet.txt --out benchmark/cases.json

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
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

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
from nightshift_core.models import Vulnerability
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
    #: The suite was already failing before we touched anything.
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
        return ProbeResult(repo=repo, verdict=ProbeVerdict.UNBUILDABLE, notes=str(exc)[:500])

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
    if not baseline.passed:
        return ProbeResult(
            repo=repo,
            verdict=ProbeVerdict.BASELINE_RED,
            baseline_seconds=baseline.duration_seconds,
            notes=f"pytest exit {baseline.exit_code}",
        )

    dependencies = read_dependencies(repo_path)
    vulnerabilities: list[Vulnerability] = osv.find_vulnerabilities(dependencies)
    if not vulnerabilities:
        return ProbeResult(
            repo=repo,
            verdict=ProbeVerdict.NOT_AFFECTED,
            baseline_seconds=baseline.duration_seconds,
        )

    fixable = [v for v in vulnerabilities if v.actionable]
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
    return ProbeResult(
        repo=repo,
        verdict=ProbeVerdict.CLEAN if verified.passed else ProbeVerdict.BREAKING,
        upgrades=upgrades,
        advisories=tuple(v.osv_id for v in fixable),
        baseline_seconds=baseline.duration_seconds,
        verify_seconds=verified.duration_seconds,
        failing_output="" if verified.passed else verified.output,
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
    parser.add_argument("--repos", required=True, help="file with one owner/name per line")
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

    repos = [
        line.strip()
        for line in Path(args.repos).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
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
    print(f"benchmark cases written to {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
