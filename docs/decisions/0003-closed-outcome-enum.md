# ADR 0003 — `Outcome` is a closed enum, and failure is a member of it

**Status:** accepted · 2026-08-07

## Context

At fleet scale, most repositories are not going to go smoothly. A large fraction
of third-party Python projects will not build in a clean container; a further
fraction arrive with a test suite that is already failing on `main`. This is not
an edge case to be handled — it is the median experience, and it was the single
biggest surprise in early manual runs.

The tempting design is to treat these as exceptions: catch them, log them, move
on, and report a repair rate computed over the repositories that worked. That
number would be large and it would be a lie, because the denominator would
quietly exclude every repository that was hard.

## Decision

`Outcome` is a **closed** enum. Every job ends in exactly one member, and
`UNBUILDABLE`, `BASELINE_RED`, `NO_FIX_AVAILABLE`, `REPAIR_EXHAUSTED` and
`POLICY_BLOCKED` are ordinary members rather than error states. Only
`INFRA_ERROR` represents a bug on our side.

Adding a member requires an ADR. The dashboard reports counts for all of them,
and the headline repair rate is computed as:

```
PATCHED_REPAIRED / (PATCHED_REPAIRED + REPAIR_EXHAUSTED)
```

— of the upgrades that actually broke something, how many did the agent fix.
Dividing by the whole fleet would flatter us with `PATCHED_CLEAN` jobs where no
model was called at all.

## Alternatives considered

**Exceptions for infrastructure failures, enum for real outcomes.** Rejected.
The boundary is not stable: "the environment would not build" is an
infrastructure failure from the worker's point of view and a finding from the
fleet's. Putting it in the enum forces us to count it.

**An open enum, or a free-text status.** Rejected. Once a status can be an
arbitrary string, the dashboard's totals stop adding up to the size of the
fleet, and nobody notices for a week.

## Consequences

- The worker has no bare `except` that swallows a repository. Each failure path
  terminates a job with a named outcome.
- `summarise()` and `outcome_counts()` always total the fleet size, including
  `IN_FLIGHT`. That invariant is asserted in the tests.
- Our headline number will be smaller than a less careful project's. It will
  also be the one we can defend under questioning, which for a judged
  architecture is the better trade.
