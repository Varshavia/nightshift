# Benchmark

How the repair rate is measured, and why it is measured in two separate tiers
that are never added together.

## The problem with measuring on wild repositories alone

Point the fleet at real forks and you cannot control whether an upgrade breaks
anything, the environment may not build, and the run is not reproducible at
demo time. Measure only there and the number moves every night for reasons that
have nothing to do with the agent.

## The problem with measuring on fixtures alone

Write the broken repositories yourself and you are grading your own homework.
The number tells you something about your prompt, not about the world.

## So: two tiers, reported separately

### Tier A — regression suite

Purpose-built minimal repositories, one per known API break, living in
`cases/`. Each is small enough to install in seconds and deterministic enough to
run on every change. This is the set used **during development**: when the
repair prompt changes, this is what tells you what you broke.

Tier A is **not the headline number**. We wrote these cases, so a high score
here is a statement about our fixtures.

Each case directory contains a runnable repository plus `case.json`:

```json
{
  "id": "jinja2-2.11-to-3.1",
  "tier": "A",
  "package": "jinja2",
  "from_version": "2.11.3",
  "to_version": "3.1.2",
  "break_kind": "removed-top-level-name",
  "expected_failure": "ImportError: cannot import name 'Markup' from 'jinja2'"
}
```

`expected_failure` documents the break for a human reading the case. It is **not**
asserted against the agent's output: the only success criterion is the one below.

### Tier B — the wild set, and the headline number

Real forks where a real advisory upgrade really breaks the calling code.
Discovered, not authored — `scripts/probe_fleet.py` finds them by doing the
whole pipeline with **no model called**: build, test, upgrade, test again. The
repositories that go from green to red are the cases.

That probe costs zero tokens, which is why it can run across the entire fleet
before any of the cloud credit is spent. It also produces the statistic the
project rests on: what fraction of security upgrades break the code that calls
them.

```bash
python scripts/probe_fleet.py --repos fleet.txt --out benchmark/cases.json
```

## The success criterion, for both tiers

> The suite passes with the new version pinned, and the tests were not modified.

Nothing else. The expected diff is never asserted — there is more than one
correct way to fix a call site, and grading against one of them would measure
conformity rather than repair. The second half of the criterion is not checked
by the benchmark at all: it is guaranteed by the policy engine, which denies
writes to test files. The benchmark therefore exercises the production code
path rather than a parallel one.

## Held-out cases

**A third of the cases stay closed until the final run.** Tuning the prompt
against every case measures how well the prompt fits the benchmark, not how well
the agent repairs code. Held-out results are reported separately from tuned
ones, and the gap between them is itself a number worth publishing.

## What each run records

Straight off `RepoJob`, no separate data model: the outcome from the closed
enum, attempts used, tokens and dollars spent, wall-clock, and diff size. The
repair rate is then

```
PATCHED_REPAIRED / (PATCHED_REPAIRED + REPAIR_EXHAUSTED)
```

— of the upgrades that actually broke something, how many did the agent fix.
Repositories that were never upgraded are not evidence either way and stay out
of the denominator.
