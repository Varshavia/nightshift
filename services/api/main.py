"""Read model and approval queue for the control tower.

Deliberately thin and read-mostly. The dashboard is a fleet control tower, not
a chat UI: it answers "what happened last night, and what needs a human" and
nothing else. The one write it exposes is an approval — a human deciding that a
particular repository's pull request may go upstream — because that decision
must be a person's, recorded, and revocable.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from nightshift_core.config import get_settings
from nightshift_core.store import (
    Approval,
    ApprovalStore,
    FirestoreApprovalStore,
    FirestoreJobStore,
    JobStore,
    outcome_counts,
    unfinished_state,
)

log = logging.getLogger("nightshift.api")


def get_store() -> JobStore:
    settings = get_settings()
    # A project, not a fork organisation: this service reads and never forks.
    settings.require_project()
    return FirestoreJobStore(
        project=settings.gcp_project, database=settings.firestore_database
    )


def fleet_summary(run_id: str | None = None) -> dict[str, Any]:
    """Outcome counts for a run. The number the whole project reports."""
    store = get_store()
    counts = outcome_counts(store, run_id=run_id)
    # None of the three unfinished states was attempted. Counting them would say
    # the fleet had reached a verdict on repositories it never finished cloning
    # — or in most cases never started.
    unfinished = {"IN_FLIGHT", "WAITING", "ABANDONED"}
    attempted = sum(v for k, v in counts.items() if k not in unfinished)
    repaired = counts["PATCHED_REPAIRED"]
    broke = repaired + counts["REPAIR_EXHAUSTED"]
    return {
        "run_id": run_id,
        "counts": counts,
        "attempted": attempted,
        # The honest denominator: of the upgrades that actually broke something,
        # how many did the agent fix? Dividing by the whole fleet would flatter us.
        "repair_rate": (repaired / broke) if broke else None,
    }


def list_jobs(run_id: str | None = None) -> list[dict[str, Any]]:
    """Stored records, each carrying whether anything is still working on it.

    ``state`` is added here rather than stored, because it is a fact about the
    clock rather than about the job: the same record is in flight at minute ten
    and stalled at minute fifty without anybody writing to it. Adding it at the
    edge keeps that derivation in one place, and means the JSON and the HTML
    cannot come to different conclusions about the same record.
    """
    jobs = []
    for job in get_store().list_jobs(run_id=run_id):
        record = job.to_dict()
        record["state"] = unfinished_state(job)
        jobs.append(record)
    return jobs


#: Outcomes get a colour so a night can be read at a glance rather than
#: counted. Three buckets, not nine: the eye is being asked "is anything wrong",
#: and a nine-colour legend answers a question nobody had.
_OUTCOME_TONE = {
    "PATCHED_REPAIRED": "good",
    "PATCHED_CLEAN": "good",
    "REPAIR_EXHAUSTED": "warn",
    "NO_FIX_AVAILABLE": "warn",
    "BASELINE_RED": "muted",
    "UNBUILDABLE": "muted",
    "POLICY_BLOCKED": "warn",
    "INFRA_ERROR": "bad",
    "IN_FLIGHT": "muted",
    # A queue nobody has drained yet is normal between nights; a job a worker
    # started and dropped is not. Only the second one should catch the eye, and
    # it earns the same colour as an infrastructure error because that is
    # usually what caused it.
    "WAITING": "muted",
    "ABANDONED": "bad",
}


def render_dashboard(summary: dict[str, Any], jobs: list[dict[str, Any]]) -> str:
    """The control tower as one server-rendered page.

    No build step and no client framework, because the page answers two
    questions — what happened last night, and what needs a human — and a
    toolchain that has to be installed before either can be answered is a
    liability during a demo and afterwards.

    Everything from the store is escaped on the way in. Repository names come
    from a reviewed pool and job notes come from our own code, but the failing
    test output that reaches ``notes`` originates in somebody else's repository,
    and that is exactly the path an injected ``<script>`` would take.
    """
    counts = summary.get("counts", {})
    rate = summary.get("repair_rate")
    rate_text = "not yet measured" if rate is None else f"{rate:.0%}"

    tiles = "".join(
        f'<div class="tile {_OUTCOME_TONE.get(name, "muted")}">'
        f"<span class='n'>{int(value)}</span><span class='l'>{html.escape(name)}</span></div>"
        for name, value in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if value
    ) or '<p class="empty">No jobs recorded yet.</p>'

    rows = "".join(_row(job) for job in jobs) or (
        '<tr><td colspan="5" class="empty">Nothing yet.</td></tr>'
    )

    return _PAGE.format(
        run_id=html.escape(str(summary.get("run_id") or "latest")),
        attempted=int(summary.get("attempted", 0)),
        rate=html.escape(rate_text),
        tiles=tiles,
        rows=rows,
    )


def _row(job: dict[str, Any]) -> str:
    outcome = str(job.get("outcome") or job.get("state") or "IN_FLIGHT")
    tone = _OUTCOME_TONE.get(outcome, "muted")
    return (
        "<tr>"
        f"<td>{html.escape(str(job.get('repo', '')))}</td>"
        f"<td><span class='pill {tone}'>{html.escape(outcome)}</span></td>"
        f"<td>{html.escape(str(job.get('phase', '')))}</td>"
        f"<td class='num'>{int(job.get('tokens_used', 0) or 0):,}</td>"
        f"<td>{_pr_cell(job)}</td>"
        "</tr>"
    )


def _pr_cell(job: dict[str, Any]) -> str:
    url = str(job.get("pr_url") or "")
    if not url.startswith("https://github.com/"):
        # Anything else is not a pull request we opened. Rendering it as a link
        # would put a URL from the store into an href, which is a redirect
        # waiting to happen.
        return "&mdash;"
    return f'<a href="{html.escape(url)}" rel="noopener noreferrer">review</a>'


def get_approvals() -> ApprovalStore:
    settings = get_settings()
    settings.require_project()
    return FirestoreApprovalStore(
        project=settings.gcp_project, database=settings.firestore_database
    )


def approve_upstream(repo: str, approver: str, note: str = "") -> dict[str, Any]:
    """Record a human's decision to allow one repository's PR upstream.

    Per-repository, human-reviewed and disclosed — see RESPONSIBLE_USE.md. There
    is no bulk approve and there will not be one: this takes a single repository
    and there is no endpoint that takes a list, which makes the promise
    structural rather than cultural.

    Recorded rather than configured. ``ALLOW_UPSTREAM_PRS`` stays false; an
    approval is the narrow exception to it, and it carries a name because an
    unattributed approval is indistinguishable from none.
    """
    approval = Approval(repo=repo, approver=approver, note=note)
    get_approvals().approve(approval)
    log.info("upstream approved for %s by %s", approval.repo, approval.approver)
    return approval.to_dict()


def revoke_upstream(repo: str) -> dict[str, str]:
    """Take an approval back. Revocable is half of what RESPONSIBLE_USE promises."""
    get_approvals().revoke(repo)
    log.info("upstream approval revoked for %s", repo)
    return {"repo": repo, "approved": "false"}


def create_app() -> Any:
    """The control tower: one page, three JSON endpoints, one write.

    Built here rather than at module scope so that importing this module — which
    the tests and the local runner do — neither constructs a Firestore client
    nor requires FastAPI to be installed.

    ``/health`` deliberately touches nothing. Cloud Run probes the container
    port before a revision goes live, and a probe that reached Firestore would
    turn a slow database into a failed deployment.

    It is ``/health`` and not ``/healthz`` because the latter never arrives:
    requests to it are answered by Google's frontend with a 404 that our logs
    never see, while ``/``, ``/robots.txt`` and ``/openapi.json`` all reach the
    container normally. Renaming costs nothing and beats arguing with
    infrastructure about a path name.
    """
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.responses import HTMLResponse

    app = FastAPI(
        title="Nightshift control tower",
        summary="What happened last night, and what needs a human.",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(run_id: str | None = None) -> str:
        return render_dashboard(fleet_summary(run_id), list_jobs(run_id))

    @app.get("/api/summary")
    def summary(run_id: str | None = None) -> dict[str, Any]:
        return fleet_summary(run_id)

    @app.get("/api/jobs")
    def jobs(run_id: str | None = None) -> list[dict[str, Any]]:
        return list_jobs(run_id)

    @app.get("/api/approvals")
    def approvals() -> list[dict[str, Any]]:
        return [approval.to_dict() for approval in get_approvals().list_approvals()]

    @app.post("/api/approvals")
    def approve(
        repo: str,
        approver: str,
        note: str = "",
        x_nightshift_approval_key: str = Header(default=""),
    ) -> dict[str, Any]:
        # The read side is public so a reviewer can see the fleet without
        # credentials. The one write is not: sending a repository upstream is a
        # decision with somebody else's name on the receiving end.
        expected = get_settings().approval_key
        if not expected or x_nightshift_approval_key != expected:
            raise HTTPException(status_code=403, detail="approval key missing or wrong")
        return approve_upstream(repo, approver, note)

    return app


#: The page itself. Kept at the bottom and out of the way: it is the least
#: interesting thing in this module and the easiest to mistake for the most.
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nightshift control tower</title>
<style>
  :root {{
    --ground: #0f1115; --surface: #171a21; --line: #262b35;
    --ink: #e7e9ee; --muted: #8b93a4;
    --good: #7cbb9c; --warn: #d6a748; --bad: #e08a7c;
    --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--ground); color: var(--ink);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .wrap {{ max-width: 1000px; margin: 0 auto; padding: 40px 24px 80px; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 4px; letter-spacing: -0.01em; }}
  .sub {{ color: var(--muted); margin: 0 0 28px; }}
  .headline {{
    display: flex; gap: 32px; flex-wrap: wrap;
    border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
    padding: 18px 0; margin-bottom: 28px;
  }}
  .headline div {{ display: flex; flex-direction: column; }}
  .headline .n {{ font-size: 1.9rem; font-variant-numeric: tabular-nums; }}
  .headline .l {{
    font-family: var(--mono); font-size: 10px; letter-spacing: .1em;
    text-transform: uppercase; color: var(--muted); margin-top: 6px;
  }}
  .tiles {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 32px; }}
  .tile {{
    background: var(--surface); border: 1px solid var(--line);
    border-left-width: 3px; border-radius: 3px; padding: 10px 14px;
    display: flex; flex-direction: column; min-width: 132px;
  }}
  .tile .n {{ font-size: 1.3rem; font-variant-numeric: tabular-nums; }}
  .tile .l {{
    font-family: var(--mono); font-size: 10px; letter-spacing: .08em; color: var(--muted);
  }}
  .good {{ border-left-color: var(--good); }}
  .warn {{ border-left-color: var(--warn); }}
  .bad {{ border-left-color: var(--bad); }}
  .muted {{ border-left-color: var(--muted); }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface); }}
  th, td {{ text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line); }}
  th {{
    font-family: var(--mono); font-size: 10px; letter-spacing: .1em;
    text-transform: uppercase; color: var(--muted); font-weight: 500;
  }}
  td.num {{ font-variant-numeric: tabular-nums; font-family: var(--mono); }}
  .pill {{
    font-family: var(--mono); font-size: 10px; letter-spacing: .06em;
    padding: 3px 8px; border-radius: 2px; border: 1px solid currentColor;
  }}
  .pill.good {{ color: var(--good); }}
  .pill.warn {{ color: var(--warn); }}
  .pill.bad {{ color: var(--bad); }}
  .pill.muted {{ color: var(--muted); }}
  .empty {{ color: var(--muted); }}
  a {{ color: #90aae0; }}
  .scroll {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 3px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Nightshift</h1>
  <p class="sub">Run <code>{run_id}</code> &middot; what happened last night,
     and what needs a human.</p>

  <div class="headline">
    <div><span class="n">{attempted}</span><span class="l">repositories attempted</span></div>
    <div><span class="n">{rate}</span><span class="l">repair rate</span></div>
  </div>

  <div class="tiles">{tiles}</div>

  <div class="scroll">
  <table>
    <thead><tr><th>Repository</th><th>Outcome</th><th>Phase</th><th>Tokens</th><th>PR</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  </div>
</div>
</body>
</html>
"""
