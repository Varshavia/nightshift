# Architecture

## The problem this shape is solving

Version-bump bots are a solved problem. Dependabot, Renovate and their kin will
find the vulnerable pin and open the pull request, and they do it well. What
they do not do is stay: a large share of those pull requests fail CI because the
patched version moved an API, and from that moment the work is a human's. Someone
reads the traceback, works out that `Session.request()` no longer takes
`timeout` as a positional argument, rewrites the call site, and pushes.

That human step is the bottleneck, and at organisational scale it is the reason
security upgrades sit open for months. It is what this system automates.

Every decision below follows from that. Where a choice made the upgrade
mechanism better but the repair loop weaker, we took the other option.

## The two halves of a night

```
Cloud Scheduler ──▶ Scanner (Cloud Run Job)          ~1 minute, whole fleet
                      │
                      ├─ load fork pool
                      ├─ read manifests
                      ├─ ONE batched OSV.dev query
                      ├─ Gemma triage pass
                      └─ Pub/Sub: one message per affected repository
                                    │
                                    ▼
                    Worker (Cloud Run Job, fan-out)  ~minutes, per repository
                      │
                      ├─ clone fork into sandbox
                      ├─ BASELINE   run the suite untouched
                      ├─ UPGRADE    rewrite manifest, reinstall
                      ├─ VERIFY     run the suite again
                      ├─ REPAIR     bounded Gemini loop  ◀── the product
                      └─ PR         open it, disclose authorship, stop
```

Diagram source: [`architecture.mmd`](architecture.mmd).

### Why the scan is one process that dies

The scanner is stateless, short-lived and fans out. It does not wait for
workers. A repository that takes twenty minutes to build cannot hold the scan
open, and a scanner crash costs one night's *scan* rather than one night's
*work* — the jobs already published continue.

The batched OSV query is what makes this cheap: one request covers every pinned
dependency in the fleet, so scanning 300 repositories costs roughly what
scanning 10 costs. This is the first place the architecture is indifferent to
fleet size.

### Why the worker order is baseline → upgrade → verify → repair

**Baseline first, with the tests untouched.** Before anything is changed the
suite runs exactly as it arrived. A repository whose suite is already red is
`BASELINE_RED` and the worker stops. Skipping this step would let us take credit
for repairing breakage we did not cause, and take blame for breakage that was
already there. Every number this project reports depends on this run happening
first.

**Verify before repair.** If the upgrade did not break anything, the job ends
`PATCHED_CLEAN` having called no model at all. A large fraction of nights end
here and they are nearly free. The expensive path is entered only when it is
earned.

**Then the repair loop**, which is the only part where a model does open-ended
work, and the only part with a ceiling on attempts, wall-clock and tokens.

## The repair loop

```
   failing test output
          │
          ▼
   ┌─────────────────────────────────────────┐
   │ Memory Bank lookup                      │   keyed by (library, from→to)
   │ "has this transition broken this way    │   the tenth repo to hit a
   │  somewhere else in the fleet before?"   │   transition is cheaper
   └──────────────┬──────────────────────────┘   than the first
                  ▼
   ┌─────────────────────────────────────────┐
   │ Gemini agent (ADK)                      │
   │  read call site · read library source   │
   │  in site-packages · one conceptual fix  │
   └──────────────┬──────────────────────────┘
                  ▼
   ┌─────────────────────────────────────────┐
   │ Policy engine — EVERY tool call         │   deny: test files, CI config,
   │                                         │   outside workspace, unlisted
   └──────────────┬──────────────────────────┘   executables, merge, force-push
                  ▼
            re-run the suite
                  │
        green ────┴──── red ──▶ attempt + 1, ceiling check, loop
          │
          ▼
     PATCHED_REPAIRED
```

The agent reads the *installed library's own source* rather than relying on what
it remembers about the API. Recollection of a library's interface at a specific
version is exactly the kind of thing a model is confidently wrong about, and the
ground truth is sitting in `site-packages`.

## The policy engine

Every tool call the agent makes passes through
`packages/nightshift_core/policy.py` before execution — not as a wrapper the
agent could route around, but as the only path by which a tool exists. It is a
pure function of `(ToolCall, Budget)`, which is why it can be tested
exhaustively in milliseconds and why it is the most heavily tested module in the
repository.

It guarantees four things:

| Guarantee | Why it is at this layer and not in the prompt |
|---|---|
| Ceilings hold | Checked before each call, so a runaway loop cannot outrun its own limit |
| Tests are read-only | An agent told to make a suite green will delete the failing test; a prompt is a request, a denial is not |
| Blast radius is the clone | Path confinement is lexical and filesystem-independent |
| Nothing merges, nothing goes upstream | `git merge` is unreachable; PRs outside the fork org are denied |

Every decision is recorded with the rule that produced it. That audit trail is
what a reviewer reads to see that a refusal was designed rather than accidental.

## State

Firestore holds one document per `RepoJob`, written at every phase transition. A
worker killed mid-flight resumes from its last completed phase and re-does at
most one repair attempt. Checkpointing per tool call would triple the writes to
buy back seconds.

`Outcome` is a closed enum. `UNBUILDABLE` and `BASELINE_RED` are results, not
exceptions — which is what lets the dashboard show a repair rate whose
denominator is honest: of the upgrades that actually broke something, how many
did the agent fix?

## Required stack, and why each piece is load-bearing

| Requirement | Ours | Why this and not something else |
|---|---|---|
| Gemini 3.5+ | Repair agent | Long-context reasoning over a traceback plus library source is the whole task |
| Google agent framework | ADK 2.0 | Tool wrapping and Memory Bank; the memory is what makes fleet scale pay off |
| Google Cloud service | Cloud Run Jobs, Pub/Sub, Cloud Scheduler, Firestore | Fan-out with per-job isolation and no idle cost between nights |
| Bonus model | Gemma triage | Cheap judgement on advisory noise before Gemini is woken |

## Scaling from ten to three hundred

The 200–300 repository figure is a configuration change, not a rewrite, and that
is the architectural claim we are making. The scan is already one request. The
queue already fans out. The worker already holds no state another worker needs.
What grows with fleet size is the concurrency cap and the bill — which is why
cost per repository is measured and displayed rather than estimated.

## Decisions

- [ADR 0001 — Python and PyPI only](decisions/0001-python-pypi-only.md)
- [ADR 0002 — Forks by default](decisions/0002-forks-by-default.md)
- [ADR 0003 — Outcome is a closed enum](decisions/0003-closed-outcome-enum.md)
