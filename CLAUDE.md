# Nightshift — working context

Read this before doing anything in this repository. It is the durable memory of the
project: what it is, what must not drift, and where the work currently stands.

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

## 4. Architecture

Nightly: Cloud Scheduler → scanner reads manifests → one batched OSV.dev query → Gemma
triage → one Pub/Sub message per affected repository → Cloud Run Job workers fan out.

Inside a worker: baseline (tests untouched) → upgrade → verify → **repair loop** → PR.
Every tool call passes through the policy engine before execution. State and checkpoints in
Firestore; repair knowledge accumulates in ADK Memory Bank keyed by library and version
transition.

Full write-up: `docs/architecture.md`. Diagram source: `docs/architecture.mmd`.
Decisions and their trade-offs: `docs/decisions/`.

## 5. Repo map

```
packages/nightshift_core/   models · osv · policy · config · store   (shared domain)
services/scanner/           nightly scan, publishes jobs, then exits
services/worker/            per-repo agent: baseline → upgrade → repair → PR
services/api/               read model + approvals for the dashboard
dashboard/                  fleet control tower (Next.js) — not a chat UI
scripts/                    fork pool construction and vetting
infra/deploy.sh             idempotent GCP deployment, least-privilege service accounts
templates/pr_body.md        PR body, includes mandatory AI-authorship disclosure
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

Implemented and working — `make check` is green (ruff · mypy --strict · 88 tests):

- `packages/nightshift_core/models.py` — closed outcome/phase enums, job aggregate
- `packages/nightshift_core/osv.py` — OSV.dev batch client (no key, no rate limit)
- `packages/nightshift_core/policy.py` — policy engine, 44 tests, the most tested module
- `packages/nightshift_core/{config,store}.py` — settings, memory + Firestore stores
- `infra/deploy.sh`, Dockerfiles, CI (ruff · mypy · pytest · gitleaks)
- `docs/` — architecture, rendered diagram, three ADRs
- `templates/pr_body.md`, `RESPONSIBLE_USE.md`, `README.md`

Deliberately stubbed, raising `NotImplementedError`. A stub returning an empty
result would make a broken scan look like a quiet night, so none of them does:

- `services/scanner/main.py` — fleet loading, manifest reading, triage, publish
  (the OSV query and the job-assembly flow around them are real)
- `services/worker/main.py` — clone, environment build, test runner, upgrade,
  repair loop, PR (the phase machine and every outcome path around them are real)
- `services/worker/agent.py` — the ADK agent itself; the instruction prompt is
  written in full and is a design artefact, not a placeholder
- `services/api/main.py` — approvals and the FastAPI app; the read model is real
- `scripts/{build_fork_pool,vet_fork_pool,run_local}.py`
- `dashboard/` — not scaffolded yet

## 8. Work blocks

Not a day-by-day schedule. Each block is done when its condition is met, then the next
starts.

**Block 1 — thin slice.** One repository, manually triggered, one agent, end to end,
producing a real PR. `make run-local REPO=owner/name` actually works. Dashboard skeleton
reads live from Firestore.

**Block 2 — fleet.** Ten repositories, real queue, real parallelism. **The repair loop
works.** Policy layer and approval queue in place.

**Block 3 — scale.** ~300 repositories forked and vetted, nightly runs producing real
accumulating data. Cost measured per repository and displayed.

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
