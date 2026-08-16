# Responsible use

Nightshift is an autonomous system that writes code and opens pull requests. The
cost of getting that wrong is not borne by us — it is borne by open-source
maintainers who did not ask for our output and who review it for free. These
rules exist to make sure our project does not become someone else's chore.

## Forks by default

The fleet operates on forks in an organisation we control. `ALLOW_UPSTREAM_PRS`
defaults to `false`, and the policy engine denies any pull request whose target
owner is not our fork organisation. This is enforced in
`packages/nightshift_core/policy.py` and covered by tests, not left to
convention.

Upstream contribution is possible but is:

- **opt-in, per repository** — never a global switch;
- **human-reviewed** — a person reads the diff and approves it in the dashboard
  before it leaves our organisation;
- **disclosed** — the pull request body states plainly that it was written by an
  AI agent and not reviewed by a human before opening, and invites the
  maintainer to tell us to stop.

There is no bulk approve, and there will not be one.

## Nothing merges itself

There is no auto-merge implementation anywhere in this repository and no flag
that enables one. `git merge` is not reachable by the agent: the policy engine
denies the subcommand. A pull request is a request.

## The test suite is not ours to edit

An agent asked to turn a red suite green will, given the opportunity, delete the
failing test. That is the single most likely way this project could produce a
convincing lie. Writes to anything that looks like a test — and the check is
deliberately broad — are denied at the policy layer. So are writes to CI
configuration, which defines how the baseline is measured.

If a test genuinely encodes the old API's behaviour and cannot pass under the
new version, the agent is instructed to stop and report it. That is a finding
for a human, not something to smooth over.

## Bounded by construction

Every loop has a ceiling: repair attempts, wall-clock seconds, tokens. They are
checked before each tool call rather than after the loop, they have conservative
defaults, and a missing environment variable can never mean "unbounded". Hitting
a ceiling produces `REPAIR_EXHAUSTED`, which is a reported result.

## Honest numbers

`Outcome` is a closed enum in which `UNBUILDABLE` and `BASELINE_RED` are
first-class results rather than errors. Repositories we could not help stay in
the denominator. A repair rate computed any other way would be marketing.

## Rate and volume

The scan is one batched OSV query per night, not one request per dependency.
Workers are capped by `NIGHTSHIFT_MAX_CONCURRENT_WORKERS`. The fork pool is an
explicit, reviewed list — the fleet never discovers its own targets by wildcard
search.

## Secrets

Nothing credential-shaped is committed. Every secret is read from the
environment; new variables are added to `.env.example` with a comment and no
value. `gitleaks` runs in CI on every push.
