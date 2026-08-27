"""Run the Tier A regression cases through the production repair pipeline.

    make benchmark
    python -m scripts.run_benchmark --case jinja2-2.11-to-3.1

Each case in ``benchmark/cases/`` is a minimal repository built around one known
API break, published to GitHub so that the fleet reaches it exactly the way it
reaches any other repository: clone, build, test, upgrade, test again, repair.
There is no shortcut around ``handle`` here and there must not be one — a
benchmark that runs a parallel code path measures the parallel path.

Publishing the fixtures is what makes that possible, and it costs nothing in
honesty: benchmark/README.md already says Tier A is authored by us and is not
the headline number. Where the files are hosted does not change who wrote them.

The success criterion is the one in benchmark/README.md and nothing else:

    the suite passes with the new version pinned, and the tests were not modified

The first half is what ``Outcome.PATCHED_REPAIRED`` means. The second half is
not checked here at all — the policy engine denies writes to test files, so a
run that cheated could not have got this far. Asserting it again in the runner
would only test the assertion.

What this measures is the repair loop: an upgrade that provably breaks the suite,
and whether the agent puts it back together. That makes it the only place in the
project where a model is called against a known-broken repository on demand,
which is also what makes it the thing to run after touching the repair prompt.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

# See the note in probe_fleet.py: run as a file, neither the repository root nor
# packages/ is on the import path.
if __package__ in {None, ""}:  # pragma: no cover - depends on how it was invoked
    _root = Path(__file__).resolve().parent.parent
    sys.path[:0] = [str(_root), str(_root / "packages")]

from services.worker.main import handle

from nightshift_core.config import Settings, get_settings
from nightshift_core.models import Outcome, RepoJob, Severity, Vulnerability
from nightshift_core.store import MemoryJobStore

log = logging.getLogger("nightshift.benchmark")

CASES = Path(__file__).resolve().parent.parent / "benchmark" / "cases"

#: Where a published case lives, given its id. Cases are ours and live in the
#: fork organisation beside the forks, because a fixture that lived somewhere
#: else would need its own credential and its own explanation.
REPO_TEMPLATE = "{org}/nightshift-case-{case_id}"

#: A repaired case and a case the upgrade never broke are both green, and only
#: one of them is evidence. Kept apart for the same reason the probe keeps
#: BREAKING apart from CLEAN.
SCORED = (Outcome.PATCHED_REPAIRED, Outcome.REPAIR_EXHAUSTED)


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    repo: str
    outcome: str
    attempts: int
    tokens: int
    cost_usd: float
    pr_url: str | None
    notes: str

    @property
    def scored(self) -> bool:
        """Whether this case says anything about the agent.

        A case that would not build, or whose suite was red before we touched
        it, measures our container. Counting it either way would put a number in
        the denominator that nothing supports.
        """
        return self.outcome in {str(outcome) for outcome in SCORED}


#: What a case must say before the pipeline is worth starting for it. Checked
#: rather than defaulted: a missing version would run the whole thing against
#: nothing and report the result as though a measurement had been taken.
REQUIRED = ("id", "package", "from_version", "to_version")


def _require(case: Mapping[str, object], where: str) -> None:
    missing = [key for key in REQUIRED if not case.get(key)]
    if missing:
        raise ValueError(f"{where} is missing {', '.join(missing)}")


def load_case(directory: Path) -> dict[str, object]:
    case: dict[str, object] = json.loads((directory / "case.json").read_text(encoding="utf-8"))
    _require(case, f"{directory.name}/case.json")
    return case


def job_for(case: Mapping[str, object], repo: str) -> RepoJob:
    """The job the scanner would have published for this case.

    Built by hand rather than by asking OSV, and deliberately: a fixture pinned
    to a version that OSV later stops calling vulnerable would silently stop
    being a test. The transition in ``case.json`` is the case, and it has to
    stay the case for as long as the file says it does.
    """
    _require(case, f"case {case.get('id', '?')}")
    return RepoJob(
        job_id=f"bench-{uuid.uuid4().hex[:8]}:{repo}",
        repo=repo,
        vulnerabilities=[
            Vulnerability(
                osv_id=str(case.get("osv_id") or f"BENCH-{case['id']}"),
                package=str(case["package"]),
                installed_version=str(case["from_version"]),
                fixed_version=str(case["to_version"]),
                severity=Severity.HIGH,
                summary=str(case.get("notes") or f"Tier A case {case['id']}"),
            )
        ],
    )


def run_case(directory: Path, settings: Settings) -> CaseResult:
    case = load_case(directory)
    repo = REPO_TEMPLATE.format(org=settings.fork_org, case_id=case["id"])
    job = job_for(case, repo)
    store = MemoryJobStore()
    store.put(job)

    log.info("running %s against %s", case["id"], repo)
    finished = handle(job, store, settings)
    return CaseResult(
        case_id=str(case["id"]),
        repo=repo,
        outcome=str(finished.outcome),
        attempts=len(finished.repair_attempts),
        tokens=finished.tokens_used,
        cost_usd=finished.cost_usd,
        pr_url=finished.pr_url,
        notes=finished.notes,
    )


def summarise(results: Sequence[CaseResult]) -> dict[str, object]:
    """Tier A's number, and the denominator it is honest about.

    ``repair_rate`` is over the cases that actually broke, matching the formula
    in benchmark/README.md. A case that never reached the repair loop is not
    counted as a success or a failure, because it was neither.
    """
    scored = [result for result in results if result.scored]
    repaired = [r for r in scored if r.outcome == str(Outcome.PATCHED_REPAIRED)]
    return {
        "cases": len(results),
        "scored": len(scored),
        "repaired": len(repaired),
        "repair_rate": (len(repaired) / len(scored)) if scored else None,
        "tokens": sum(r.tokens for r in results),
        "cost_usd": round(sum(r.cost_usd for r in results), 4),
        "results": [asdict(r) for r in results],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="one case id; default is every case")
    parser.add_argument("--out", type=Path, help="write the run to this JSON file")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    settings = get_settings()
    # A benchmark run opens a pull request on the case repository, so it needs
    # the same organisation the fleet operates in. Checked here rather than at
    # the end, where it would arrive after the tokens had been spent.
    settings.require_cloud()

    directories = sorted(d for d in CASES.iterdir() if (d / "case.json").is_file())
    if args.case:
        directories = [d for d in directories if d.name == args.case]
        if not directories:
            print(f"no case named {args.case!r} in {CASES}", file=sys.stderr)
            return 2

    results = [run_case(directory, settings) for directory in directories]
    summary = summarise(results)

    for result in results:
        print(f"{result.case_id}: {result.outcome} ({result.attempts} attempts)")
        if result.pr_url:
            print(f"  {result.pr_url}")
        elif result.notes:
            print(f"  {result.notes}")

    rate = summary["repair_rate"]
    print(
        f"\n{summary['repaired']}/{summary['scored']} repaired"
        + (" — unmeasured rather than zero" if rate is None else f" ({rate:.0%})")
    )
    print(f"{summary['tokens']:,} tokens, ${summary['cost_usd']}")

    if args.out:
        args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
