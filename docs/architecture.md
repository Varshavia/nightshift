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
                      ├─ LEDGER     has this transition been solved before?
                      ├─ REPAIR     bounded Gemini loop  ◀── the product
                      ├─ LIBRARIAN  generalise the fix into a recipe
                      ├─ REVIEW     second opinion on the diff
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

## The Migration Ledger

The repair loop above is linear in fleet size: three hundred repositories, three hundred
independent repairs, three hundred times the tokens. That is the wrong shape, because
vulnerable pins cluster. A handful of transitions — `jinja2 2.11→3.x`, `requests
2.25→2.32`, `urllib3 1.26→2.x`, `pyyaml 5.3→6.0` — dominate any real fleet. Solving the
same migration forty times is spending the credit on work already done.

So the fleet remembers. A **Librarian** agent reads each finished repair and writes back a
generalised rule, scoped in Vertex AI Memory Bank by `{library, from_version, to_version}`:

```
scope   {library: jinja2, from_version: 2.11.3, to_version: 3.1.2}
fact    Jinja2 3.0 removed the top-level Markup and escape re-exports.
        Import them from markupsafe instead.
topics  [verified, removed-top-level-name, PyPI]
```

Retrieval has three tiers, and which one fired is recorded on every job as `ledger.hit`:

| Tier | When | Cost |
|---|---|---|
| **exact** | the scope matches a known transition | one attempt, a few thousand tokens |
| **near** | similarity search finds an adjacent transition of the same library | fewer attempts, offered as lower-confidence |
| **miss** | nobody has seen this transition | full price, and it teaches the Ledger |

Two stores, deliberately. Memory Bank is the agent's recall surface — text, semantically
searchable, scoped. Firestore at `ledger/{library}:{from}:{to}` is the ledger of record —
confirmation counts, provenance, the audit trail. Incrementing a counter by rewriting a
memory's text would be the wrong shape, and Memory Bank is not a relational store.

**Why this cannot lie.** A recipe is offered to the repair agent as prior art, never as an
instruction, and the success criterion is untouched: the repository's own suite passes and
the tests were not modified. The worst a wrong recipe can do is waste attempts. Recipes
start `provisional` and become `verified` only after two independent confirmations —
two repositories, neither the originator, where the recipe was retrieved and the repair
then succeeded.

**Why the Librarian is a separate agent.** It writes to the Ledger and can never reach a
repository; the repair agent reads the Ledger and can never write to it. That is enforced
with IAM Conditions on `aiplatform.googleapis.com/memoryScope`, not with a prompt. An agent
that cannot write to the Ledger cannot poison it.

Full reasoning and the alternatives rejected: [ADR 0004](decisions/0004-the-migration-ledger.md).

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

### Model Armor sits in front of it, not instead of it

The repair agent reads repository file contents, docstrings, READMEs and test output. All
of it is attacker-controllable text going into a model that holds write access to a
sandbox. That is a real injection surface, not a hypothetical one, so untrusted content
passes a Model Armor template before it enters a prompt.

The two layers guard different failure modes and neither substitutes for the other:

| | Inspects | Says |
|---|---|---|
| **Model Armor** | content, on the way in | "this docstring is trying to persuade you" |
| **Policy engine** | actions, on the way out | "you may not write to a test file, whatever you were persuaded of" |

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
| Gemini 3.5+ | Repair, Librarian, Reviewer | Long-context reasoning over a traceback plus library source is the whole task. Flash carries most breaks; Pro is reached for after two failed attempts |
| Google agent framework | ADK 2.0 | Tool wrapping and Memory Bank; the memory is what makes fleet scale pay off |
| Memory Bank | The Migration Ledger | Exact-scope retrieval on `{library, from→to}` is precisely the key this problem has |
| Agent Registry | Agent versioning | A benchmark number is meaningless unless it can be attributed to one agent version |
| Model Armor | Untrusted-input screening | Repository content is attacker-controllable by construction |
| Cloud Trace / OTel | Observability | The cost curve is a query over span attributes, not an illustration of one |
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
- [ADR 0004 — Repairs accumulate into a Migration Ledger](decisions/0004-the-migration-ledger.md)

Design specs and implementation plans live in [`superpowers/`](superpowers/).
