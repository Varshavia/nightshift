# Nightshift — working context

Read this before doing anything in this repository. It is the durable memory of the
project: what it is and what must not drift.

---

## 0. Before anything else

**Read `SESSION_SUMMARY.md` first — every session, before reading the rest of this
file and before touching any code.**

This file is the durable *design* of Nightshift. `SESSION_SUMMARY.md` is the
durable *state* of it. They answer different questions and neither substitutes
for the other: this file tells you what the project is meant to be, that file
tells you what is actually true this morning.

Two people build this repository at different hours, and neither sees the other's
session. That file is the entire handoff — read its `NOW` block to learn what is
true today, then skim the log for what changed since you were last here.

**Before you finish a session, append a log entry and rewrite `NOW` in place.**
This applies to agents as much as to humans: a session that ends without it has
silently broken the other developer's next session, and they will not find out
until they have already acted on stale information.

Where the two files disagree, `SESSION_SUMMARY.md` wins on matters of state and
this file wins on matters of design — and the disagreement itself is a bug in
whichever file is stale. Fix it in the same session you noticed it.

---

## 1. What this is

An agent fleet that finds vulnerable dependencies across hundreds of repositories,
upgrades them, and **repairs the code the upgrade breaks** — governed, auditable, and
finished by morning.

**The core insight, which every decision must serve:** version-bump bots already open the
PR and walk away. A large share of those PRs fail CI because the patched version moved an
API, and a human has to read the traceback and rewrite the calling code. That human step is
the bottleneck, and it is what this project automates. If a change makes the upgrade
mechanism better but the repair loop weaker, it is the wrong change.

**The second insight, added 19 Aug (ADR 0004):** vulnerable pins cluster hard. A handful
of transitions dominate any few hundred Python repositories. So every successful repair is
generalised into a **migration recipe** and kept in a **Migration Ledger**, and the fortieth
repository to hit `jinja2 2.11→3.1` starts from the answer instead of the traceback. Cost
per repository falls as the fleet works. That curve is the headline number.

Elevator pitch (Devpost, ≤200 chars):

> Dependabot opens the PR and walks away. Nightshift stays: an agent fleet that patches
> CVEs across hundreds of repos overnight, then repairs the code each upgrade breaks.

## 2. Hard constraints

Built for the **All Things Agentic Hackathon** (Google / Devpost).

| | |
|---|---|
| Submission deadline | **31 Aug 2026, 17:00 PDT** (01 Sep, 03:00 Europe/Istanbul) |
| Cloud credit request deadline | 28 Aug 2026, 12:00 PT — $150, non-negotiable cutoff |
| Category (choose one, chosen) | **The Fortified Enterprise Fleet** |
| Team | Two people, part-time |

**Required stack — missing any one of these is elimination at stage one:**

1. Gemini 3.5+ (Gemini API or Vertex AI)
2. A Google agent framework — we use **ADK 2.0**
3. At least one Google Cloud infrastructure service — we use Cloud Run, Pub/Sub,
   Cloud Scheduler, Firestore

**Scoring weights.** Innovation & operational utility 40% · Architectural discipline 30% ·
Demo & production readiness 30%. The last one is why the video and the README are not
afterthoughts: an hour spent there is worth more than an hour spent on code.

**Stage-three bonuses, max +0.6 on a 1–6 scale.** Published write-up (+0.2), social post
tagged `#AllThingsAgenticHackathon` (+0.2), integration of Gemma / Veo / Lyria (+0.2). The
Gemma triage pass in the scanner is the third one — do not remove it.

## 3. Non-negotiables

These are decisions already made. Do not quietly reverse them; if one seems wrong, say so
explicitly and wait.

- **Python/PyPI only.** Ecosystem-specific logic lives behind an adapter interface. See
  ADR 0001 — this is a scope decision, not an oversight.
- **The repair loop is the product.** Anything that dilutes attention from it is out of
  scope for the hackathon window.
- **Forks by default.** `ALLOW_UPSTREAM_PRS=false`. The fleet operates on forks in an org we
  control. Upstream contribution is opt-in, per repository, human-reviewed, and disclosed.
  See `RESPONSIBLE_USE.md`.
- **Nothing merges itself.** No auto-merge implementation, no flag that enables one.
- **Every loop has a ceiling** — attempts, wall-clock, tokens. No exceptions.
- **`Outcome` is a closed enum.** `UNBUILDABLE` and `BASELINE_RED` are first-class results,
  not errors. This is what makes the repair rate a meaningful number rather than a claim.
- **No secret is ever committed.** Everything credential-shaped is read from the
  environment. New variables go into `.env.example` with a comment and no value.
- **A recipe is a hint, never an instruction.** The Ledger informs the repair agent; it
  never overrides the success criterion. The suite passes and the tests were not modified,
  or the repair did not happen. A wrong recipe costs attempts; it cannot produce a false
  green. See ADR 0004.
- **Only the Librarian writes to the Ledger**, and it can never reach a repository. This is
  IAM, not prompt discipline — an agent that cannot write to the Ledger cannot poison it.

## 4. Architecture

Nightly: Cloud Scheduler → scanner reads manifests → one batched OSV.dev query → Gemma
triage → one Pub/Sub message per affected repository → Cloud Run Job workers fan out.

Inside a worker: baseline (tests untouched) → upgrade → verify → **Ledger lookup** →
**repair loop** → Reviewer → PR. Every tool call passes through the policy engine before
execution. State and checkpoints in Firestore.

**Four agents, each with a genuinely different job** — this is what makes the Registry real
rather than decorative:

| Agent | Model | Job |
|---|---|---|
| Triage | Gemma 3 | Is this advisory worth waking a worker for? |
| Repair | Gemini 3.5 Flash → Pro | Read the traceback, rewrite the call site. |
| Librarian | Gemini 3.5 Pro | Generalise a finished repair into a recipe. Promote it. |
| Reviewer | Gemini 3.5 Flash | Second opinion on the diff before the PR opens. |

**The Migration Ledger.** Recipes live in Vertex AI Memory Bank scoped by
`{library, from_version, to_version}` — an exact-match key. Retrieval is three-tier: exact
hit, near hit (similarity search over adjacent transitions of the same library), or miss.
Evidence and confirmation counts live in Firestore at `ledger/{library}:{from}:{to}`.
Memory Bank is the recall surface; Firestore is the ledger of record. See ADR 0004.

**Two guard layers, different failure modes.** Model Armor inspects *content* on the way in
("this docstring is trying to persuade you"); the policy engine inspects *actions* on the
way out ("you may not write to a test file, whatever you were persuaded of"). Neither
replaces the other.

**Telemetry is the metric, not a picture of it.** OpenTelemetry spans carry `ledger.hit`,
`agent.version`, `policy.rule` and tokens to Cloud Trace, and the cost curve is a query
over those attributes.

Full write-up: `docs/architecture.md`. Diagram source: `docs/architecture.mmd`.
Decisions and their trade-offs: `docs/decisions/`. The Ledger design in full:
`docs/superpowers/specs/2026-08-19-migration-ledger-design.md`.

## 5. Repo map

```
packages/nightshift_core/   models · osv · policy · config · store · ledger · telemetry
services/scanner/           nightly scan, publishes jobs, then exits
services/worker/            per-repo agents: toolchain · tools · repair · agent · pull_request
services/api/               read model + approvals for the dashboard
dashboard/                  fleet control tower (Next.js) — not a chat UI
scripts/                    fork pool construction, vetting, and the zero-token probe
benchmark/                  two-tier method: authored cases (A) and discovered ones (B)
infra/deploy.sh             idempotent GCP deployment, least-privilege service accounts
templates/pr_body.md        PR body, includes mandatory AI-authorship disclosure
docs/superpowers/           design specs and implementation plans
```

## 6. Conventions

**Language.** Everything in the repository is in English — code, comments, commits, docs.
Conversation with the team happens in Turkish; nothing Turkish goes into the repo.

**Commits.** Conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`chore:`). Subject in the imperative, under ~70 chars. Body explains *why*, not *what*.

**Branches and PRs.** Work happens on a branch and lands through a PR, even solo — the PR
trail is part of what a judge reads.

```
feat/<short-slug>      new capability
fix/<short-slug>       defect
docs/<short-slug>      documentation only
```

A PR description states what changed, why, and how it was verified. Small and frequent
beats large and occasional.

**Never force-push `main`.** The commit history is the originality evidence for the
hackathon: it must show the project growing from an empty repository after 3 August 2026.
(The single exception already spent: the very first push, which overwrote GitHub's
auto-generated README.)

**Architectural changes get an ADR.** A numbered file in `docs/decisions/` recording the
decision, the alternatives considered, and the trade-off accepted.

**Tests.** The policy engine is tested first and thoroughly — a bug there blocks real work
rather than merely mislabelling it.

## 7. Current state

**Not recorded here.** State lives in `SESSION_SUMMARY.md` — its `NOW` block, kept current
by whoever worked last. This file describes what the project *is*; duplicating what it
*currently is* in two places guarantees the two disagree within a week.

What belongs here instead is the shape that does not change session to session:

- The domain is deliberately free of infrastructure. `packages/nightshift_core` imports no
  Google Cloud client at module load, which is why the suite runs on a laptop with no
  credentials and why CI can prove it on every push.
- Stubs raise `NotImplementedError` and never return an empty result. A stub that returned
  `[]` would make a broken scan look like a quiet night — the failure mode this project is
  least willing to have.
- `packages/nightshift_core/policy.py` is the most heavily tested module and stays that
  way. A bug there lets an autonomous process do real work nobody asked for.

## 8. Work blocks

Not a day-by-day schedule. Each block is done when its condition is met, then the next
starts.

**Block 1 — the repair loop.** One repository, manually triggered, end to end, producing a
real PR. `make run-local REPO=owner/name` actually works. Nothing else in the design matters
until this does. Plan: `docs/superpowers/plans/2026-08-19-block-1-repair-loop.md`.

**Block 2 — the Ledger.** The Librarian writes recipes, the three-tier read path uses them,
OTel spans carry `ledger.hit`. Ten repositories, real queue, real parallelism. The cost
curve becomes measurable.

**Block 3 — governance and scale.** Agent Registry, four identities with IAM Conditions on
`memoryScope`, Model Armor on the untrusted-input path, the Reviewer. ~300 repositories
forked and vetted; cost per repository measured and displayed.

**The cut line is 27 August.** If Block 2 is not working by then, ship Block 1 plus
governance, drop the curve to a smaller N, and report it as measured. A smaller honest
curve beats a larger staged one.

**Block 4 — showcase.** Code frozen. Four-minute video recorded, README verified from a
clean checkout, architecture diagram current, write-up and social post published, Devpost
form submitted.

The 200-repository figure is a scale switch thrown near the end, not a weight carried from
the start. If the architecture is right at ten repositories, reaching several hundred is a
configuration change — and that is precisely the architectural story we tell the judges.

## 9. How the team works

Two people. One owns the engine (agent architecture, queue, sandbox, repair loop, policy);
the other owns the showcase (control tower UI, approval flow, README, diagram, video,
write-up). The showcase is not secondary work — it is 30% of the score.

Preferences to respect when proposing work: give **block-level objectives, not day-by-day
task lists**. State the goal and the definition of done; let the humans sequence it.

## 10. Known traps

- **Test environment setup is the real difficulty**, not the agent. A large fraction of
  third-party repositories will not build or arrive already failing. This is expected,
  modelled as a first-class outcome, and counted — never treated as an exception.
- **Runaway repair loops** burn the $150 credit and the night. Ceilings are not optional.
- **Scope creep into other ecosystems** would end the project. See ADR 0001.
- **Leaving the video to the last days** is the most common way good hackathon projects
  lose. Freeze the code early.
- **Do not spam open-source maintainers.** Forks by default; upstream only where invited,
  reviewed by a human, and disclosed.
