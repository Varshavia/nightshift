# ADR 0002 — Forks by default, upstream only by invitation

**Status:** accepted · 2026-08-06

## Context

To demonstrate anything at fleet scale we need hundreds of real repositories
with real vulnerable dependencies and real test suites. The obvious source is
open-source GitHub. The obvious action is to open pull requests against those
repositories.

The obvious action is also how an autonomous system becomes a burden on people
who never asked for it. Maintainers review pull requests for free; a machine
that can open three hundred of them overnight can consume more volunteer
attention in one night than it saves in a year. Several bot-driven contribution
waves have already made this a sore subject in open source, and rightly.

## Decision

The fleet operates on **forks in an organisation we control**.
`ALLOW_UPSTREAM_PRS` defaults to `false`, and the policy engine denies any pull
request whose target owner is not our fork organisation — enforced in code and
covered by tests, not left to convention.

Upstream contribution is opt-in per repository, human-reviewed before it leaves
our organisation, and disclosed in the pull request body. There is no bulk
approve.

## Alternatives considered

**Open upstream pull requests, rate-limited.** Rejected. A rate limit bounds the
volume of the imposition, not its legitimacy. The maintainer still did not ask.

**Synthetic repositories with injected vulnerabilities.** Rejected for the
opposite reason: it would be perfectly polite and prove nothing. The interesting
difficulty of this project — that real repositories do not build, that real
suites are already red, that real API breaks are strange — disappears entirely
if we author the test data ourselves.

**Forks, with upstream as a stretch goal.** Accepted, and this is what the ADR
records. Forks give us real code, real breakage and real numbers with no
imposition on anyone.

## Consequences

- Our pull requests are reviewed by us, which is weaker external validation than
  a merged upstream PR would be. We accept that trade and say so in the demo.
- The fork pool must be built and maintained (`scripts/build_fork_pool.py`),
  which is real work that produces no demo footage.
- The disclosure text in `templates/pr_body.md` is mandatory and includes an
  explicit invitation for a maintainer to tell us to stop.
