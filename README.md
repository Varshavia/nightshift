<div align="center">

# 🌙 Nightshift

**Dependabot opens the PR and walks away. Nightshift stays.**

An agent fleet that finds vulnerable dependencies across hundreds of repositories,
upgrades them, and **repairs the code the upgrade breaks** — governed, auditable,
and finished by morning.

[Architecture](docs/architecture.md) · [Decisions](docs/decisions/) · [Responsible use](RESPONSIBLE_USE.md)

</div>

---

## The problem

Version-bump bots are a solved problem. Dependabot and Renovate will find the
vulnerable pin and open the pull request, and they do it well.

Then they walk away — and a large share of those pull requests fail CI, because
the patched version moved an API. Someone has to read the traceback, work out
that the library's `Session.request()` no longer accepts `timeout` positionally,
rewrite the call site, and push. That human step is why security upgrades sit
open for months at organisational scale.

**That step is what Nightshift automates.** Not the version bump — the repair.

And it only does it once. Vulnerable pins cluster hard: a handful of transitions dominate
any few hundred Python repositories. So every repair Nightshift completes is generalised
into a **migration recipe**, and the fortieth repository to hit `jinja2 2.11→3.1` starts
from the answer instead of the traceback. **Cost per repository falls as the fleet works.**

## What it does, in one night

```
02:00  Cloud Scheduler wakes the scanner
02:01  One batched OSV.dev query covers every pinned dependency in the fleet
02:01  A Gemma pass drops the advisories not worth waking a worker for
02:02  One Pub/Sub message per affected repository; the scanner exits
02:02  Cloud Run workers fan out, one per repository:

         BASELINE   run the suite untouched  ──▶ already red?  BASELINE_RED
                                             ──▶ won't build?  UNBUILDABLE
         UPGRADE    rewrite the manifest
         VERIFY     run the suite again      ──▶ still green?  PATCHED_CLEAN
         LEDGER     has the fleet solved this transition before?
         REPAIR     bounded Gemini loop      ──▶ fixed it?     PATCHED_REPAIRED
                                             ──▶ ran out?      REPAIR_EXHAUSTED
         LIBRARIAN  generalise the fix into a recipe for next time
         REVIEW     second opinion on the diff
         PR         opened, authorship disclosed, nothing merged

08:00  A human opens the control tower and reads what happened.
```

## The Migration Ledger

Four agents, each with a genuinely different job:

| Agent | Model | Job |
|---|---|---|
| **Triage** | Gemma 3 | Is this advisory worth waking a worker for? |
| **Repair** | Gemini 3.5 Flash → Pro | Read the traceback, rewrite the call site. |
| **Librarian** | Gemini 3.5 Pro | Generalise a finished repair into a reusable recipe. |
| **Reviewer** | Gemini 3.5 Flash | Second opinion on the diff before the PR opens. |

The Librarian is the one that makes the fleet an institution rather than a batch job. It
reads a repair that worked and writes back a rule — *"Jinja2 3.0 removed the top-level
`Markup` and `escape` re-exports; import them from `markupsafe`"* — scoped in Vertex AI
Memory Bank by `{library, from_version, to_version}`. It never sees a repository and cannot
modify one.

Retrieval is three-tier: an **exact** scope hit, a **near** hit found by similarity search
across adjacent transitions of the same library, or a **miss** that pays full price and
teaches the Ledger something new. A recipe is provisional when written and verified only
after two independent confirmations.

**A recipe is a hint, never an instruction.** The success criterion never moves: the suite
passes and the tests were not modified. A wrong recipe costs attempts. It cannot
manufacture a false green. Full reasoning in [ADR 0004](docs/decisions/0004-the-migration-ledger.md).

## The three things that make it trustworthy

**The agent cannot touch the tests.** An agent asked to make a red suite green
will, given the chance, delete the failing test. That is the most likely way
this project could produce a convincing lie, so writes to test files are denied
by the policy engine — a code path, covered by tests, not a line in a prompt.
Writes to CI configuration are denied for the same reason.

**Baseline first, always.** Before anything is changed the suite runs as it
arrived. A repository that was already failing is `BASELINE_RED` and we stop.
Without that run, every number we report would be taking credit for breakage we
did not cause.

**Failure is a first-class result.** `Outcome` is a closed enum in which
`UNBUILDABLE` and `BASELINE_RED` are ordinary members. Repositories we could not
help stay in the denominator, which is what turns "repair rate" from a claim
into a number:

```
repair rate = PATCHED_REPAIRED / (PATCHED_REPAIRED + REPAIR_EXHAUSTED)
```

Of the upgrades that actually broke something — how many did the agent fix?

## Architecture

![Architecture](docs/architecture.png)

Every tool call the agent makes passes through the policy engine before
execution. It is a pure function of `(ToolCall, Budget)` — no I/O, no clock —
which is why it can be tested exhaustively and why it is the most heavily tested
module in the repository. Full write-up: [`docs/architecture.md`](docs/architecture.md).

## Stack

| | |
|---|---|
| **Gemini 3.5 Flash → Pro** | Repair, Librarian and Reviewer. Flash carries most breaks; Pro is reached for after two failed attempts |
| **Gemma 3** | Cheap triage over raw advisories, before an expensive model is woken |
| **ADK 2.0** | Agent runtime and policy-wrapped tools — the agent never holds an unwrapped tool |
| **Memory Bank** | The Migration Ledger, scoped by `{library, from→to}` so the fortieth repository is nearly free |
| **Agent Registry** | Each agent versioned as `(prompt, model, policy ruleset)`, so a benchmark number can be attributed to one |
| **Model Armor** | Repository content is attacker-controllable text going into a model with sandbox write access. It is screened |
| **Cloud Trace / OTel** | `ledger.hit`, `agent.version`, `policy.rule`, tokens. The cost curve is a query over spans, not a spreadsheet |
| **Cloud Run Jobs** | Scanner and per-repository workers; isolation per job, no idle cost between nights |
| **Pub/Sub** | Fan-out, one message per affected repository |
| **Cloud Scheduler** | The 02:00 trigger |
| **Firestore** | Job state and the Ledger of record, checkpointed at every phase transition |

## Quick start

```bash
git clone https://github.com/Varshavia/nightshift.git
cd nightshift

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
make install

make check          # ruff · mypy --strict · pytest
```

That runs with no credentials and no cloud access: the domain is deliberately
free of infrastructure so the suite runs on a laptop.

To run against real repositories:

```bash
cp .env.example .env     # fill in project, fork org, GitHub token
make run-local REPO=owner/name
```

To deploy the fleet:

```bash
./infra/deploy.sh        # idempotent; least-privilege service accounts
```

## Repository map

```
packages/nightshift_core/   models · osv · policy · config · store · ledger · telemetry
services/scanner/           nightly scan, publishes jobs, then exits
services/worker/            per-repo agents: toolchain · tools · repair · agent · pull_request
services/api/               read model + approvals for the dashboard
dashboard/                  fleet control tower (Next.js) — not a chat UI
scripts/                    fork pool construction, vetting, and the zero-token probe
benchmark/                  two-tier method: authored cases (A) and discovered ones (B)
infra/deploy.sh             idempotent GCP deployment
templates/pr_body.md        PR body, includes mandatory AI-authorship disclosure
docs/superpowers/           design specs and implementation plans
```

## Status

| | |
|---|---|
| ✅ Domain, policy engine, OSV client, stores, manifests | implemented, `mypy --strict` |
| ✅ Worker toolchain — clone, build, test, upgrade | implemented, calls no model |
| ✅ Zero-token fleet probe and the two-tier benchmark | implemented |
| ✅ CI, Dockerfiles, deployment script | working |
| ✅ Architecture, diagram, four ADRs, Ledger design | written |
| 🚧 Repair loop, Librarian, Reviewer, Registry, dashboard | in progress — see `SESSION_SUMMARY.md` |

Stubs raise rather than return empty. A broken scan must not look like a quiet
night.

**Live state — what is green right now, what is blocked, what is next — lives in
[`SESSION_SUMMARY.md`](SESSION_SUMMARY.md), not here.** This file describes the project;
that one describes today.

## Responsible use

The fleet operates on **forks in an organisation we control**.
`ALLOW_UPSTREAM_PRS=false` by default and the policy engine enforces it. Nothing
merges itself; there is no auto-merge implementation and no flag that enables
one. Every pull request discloses that it was written by an AI agent and invites
the maintainer to tell us to stop. See [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md).

## Licence

Apache-2.0.

---

<div align="center">
Built for the <b>All Things Agentic Hackathon</b> · The Fortified Enterprise Fleet
</div>
