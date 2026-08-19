# ADR 0004 — Repairs accumulate into a Migration Ledger

**Status:** accepted · 2026-08-19

## Context

Until now every repair was independent. Repository #1 and repository #40 could
hit the same `jinja2 2.11 → 3.1` break, produce the same one-line fix, and pay
the same forty thousand tokens for it. Cost was linear in fleet size and nothing
the fleet learned survived the job that learned it.

That is wrong twice over.

**Economically.** Vulnerable pins cluster hard. Across a few hundred Python
repositories a handful of transitions dominate — `jinja2 2.11→3.x`,
`requests 2.25→2.32`, `urllib3 1.26→2.x`, `pyyaml 5.3→6.0`. The tail is long but
the head is enormous. A fleet that solves the same migration forty times from
scratch is spending the credit on work it has already done.

**Architecturally.** The Fortified Enterprise Fleet category asks for a network
of institutional agents that maintain context across weeks of asynchronous
operation. One agent type replicated horizontally is scale, not a network, and a
per-job scratchpad is not institutional memory.

## Decision

Every successful repair is generalised into a **migration recipe** and written to
Vertex AI Memory Bank, scoped by `{library, from_version, to_version}`.

- A new **Librarian** agent does the generalising. It reads a finished repair
  chain and writes a rule; it never sees a repository and cannot modify one.
- Retrieval is three-tier: **exact** scope hit, **near** hit via similarity
  search over adjacent transitions of the same library, or **miss** — a cold
  repair at full price that produces a new provisional recipe.
- A recipe is `provisional` when written and becomes `verified` after **two
  independent confirmations**: two repositories, neither the originator, where
  the recipe was retrieved and the repair then succeeded.
- Structured evidence and confirmation counts live in Firestore at
  `ledger/{library}:{from}:{to}`. Memory Bank holds the retrievable text.

**A recipe is a hint, never an instruction.** The success criterion does not
change: the repository's own suite passes and the tests were not modified.

## Alternatives considered

**A shared cache of successful diffs, keyed the same way.** Simpler, and the cost
curve would be just as real. Rejected because a diff is not transferable — it
encodes one repository's call sites. The generalisation step is what makes the
knowledge apply to a repository nobody has seen, and it is the part a second team
could not trivially copy.

**Fine-tuning on successful repairs.** Rejected outright for this window. It is
slow, it is expensive, it cannot be audited, and a bad example cannot be deleted
the way a bad recipe can.

**One store instead of two.** Rejected. Incrementing a confirmation count by
rewriting a memory's text is the wrong shape, and Memory Bank is not a relational
store. Firestore is the ledger of record; Memory Bank is the recall surface.

## Consequences

- **Cost per repository falls as the fleet works.** That curve, derived from
  Cloud Trace span attributes rather than a spreadsheet, becomes the headline
  number alongside the repair rate.
- **Recipe poisoning is now a real failure mode.** A wrong recipe learned early
  could mislead every later repair. Three things bound it: provisional recipes
  are offered as hypotheses rather than instructions, verification requires two
  independent confirmations, and the suite remains the only measure of success —
  so a bad recipe costs attempts and cannot manufacture a false green.
- **Write access to the Ledger is an identity boundary**, enforced with IAM
  Conditions on `aiplatform.googleapis.com/memoryScope`: the repair agent reads
  and cannot write, the Librarian writes and cannot reach a repository. An agent
  that cannot write to the Ledger cannot poison it, and that is a property of the
  IAM policy rather than of a prompt.
- **`Outcome` is unchanged.** A Ledger miss is not an outcome, and a Reviewer
  block is `POLICY_BLOCKED`. ADR 0003 still holds.
- Memory Bank is in public preview. Every call has a local fallback: a preview
  outage degrades the fleet to cold repair and never stops a run.
