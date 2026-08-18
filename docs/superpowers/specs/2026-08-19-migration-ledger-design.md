# The Migration Ledger

**Status:** proposed, awaiting review · **Date:** 2026-08-19 · **Supersedes:** nothing

A design for turning Nightshift from one agent run N times into a network of
institutional agents that gets cheaper the more repositories it touches.

---

## 1. The problem with the current design

Nightshift today repairs each repository in isolation. Cost is linear in fleet
size: 300 repositories, 300 independent repairs, 300 × ~40k tokens. Nothing the
fleet learns on repository #1 is available to repository #2.

That is wrong on two counts.

**It is economically wrong.** Vulnerable dependency pins cluster hard. Across a
few hundred Python repositories the same handful of transitions dominate —
`jinja2 2.11→3.x`, `requests 2.25→2.32`, `urllib3 1.26→2.x`, `pyyaml 5.3→6.0`.
The long tail is long, but the head is enormous. A fleet that solves the same
migration forty times from scratch is burning the credit on work it has already
done.

**It is wrong for the category.** The Fortified Enterprise Fleet track asks for
"a scalable network of institutional agents… cataloged for cross-department use…
that safely maintain context across weeks of asynchronous operations." One agent
type replicated horizontally is scale, not a network, and a per-job scratchpad is
not institutional memory.

## 2. The idea

Every successful repair produces a **migration recipe**: a generalized,
evidence-backed rule about how one library transition breaks calling code and how
it was fixed. Recipes accumulate in Vertex AI Memory Bank, scoped by
`{library, from_version, to_version}`. A repair that finds a verified recipe
starts from the answer rather than from the traceback.

Cost per repository therefore falls as the fleet works, and that curve — not a
repair-rate percentage alone — becomes the headline number. It is the honest
shape of the problem, which is why it is also the demo.

**The invariant that makes this safe:** a recipe is a *hint*, never an
instruction, and the success criterion never changes — the repository's own test
suite passes and the tests were not modified. A wrong recipe can waste attempts.
It cannot manufacture a false green.

## 3. The agent catalog

The Ledger creates work that is honestly a different job, which is what makes a
registry of agents real rather than decorative.

| Agent | Model | Reads | Writes | Job |
|---|---|---|---|---|
| **Triage** | Gemma 3 | OSV records | nothing | Is this advisory worth waking a worker for? |
| **Repair** | Gemini 3.5 Flash → Pro on escalation | sandbox, Ledger | sandbox | Read the traceback, rewrite the call site. |
| **Librarian** | Gemini 3.5 Pro | a finished repair chain | Ledger | Generalize a diff into a rule. Promote provisional → verified. |
| **Reviewer** | Gemini 3.5 Flash | diff, test reports | nothing | Second opinion before the PR opens. |

The Librarian is the new idea and the one another team is least likely to have:
an agent whose entire job is to teach the other agents. It never sees a
repository and cannot modify one.

The Reviewer is deliberately not the agent that wrote the code. It checks that
the diff touches no test, contains nothing secret- or PII-shaped, and stays
scoped to the failure in the traceback. A Reviewer block terminates the job as
`POLICY_BLOCKED` — **`Outcome` stays closed and unchanged**, per CLAUDE.md §3 and
ADR 0003. If we later decide a reviewer block deserves its own member, that is an
ADR, not a patch.

## 4. Ledger mechanics

### Recipe

```
scope:         {library, from_version, to_version}   # exact-match retrieval key
fact:          the generalized rule, one paragraph, written for another agent
topics:        [provisional | verified, <break_kind>, PyPI]
```

Structured evidence lives in Firestore at `ledger/{library}:{from}:{to}`:

```
confirmations: int
evidence:      [{repo, osv_id, diff_sha, attempts_used, trace_id}]
first_seen, last_confirmed
```

**Two stores, on purpose.** Memory Bank is the agent's recall surface — text,
semantically searchable, scoped. Firestore is the ledger of record — counters,
provenance, and the audit trail. Incrementing a confirmation count by rewriting
a memory's text would be the wrong shape, and Memory Bank is not a relational
store. Do not try to make either one do the other's job.

### Retrieval, three tiers

1. **Exact hit** — `RetrieveMemories(scope={library, from, to})`. The transition
   has been solved before. The recipe enters the repair prompt as prior art.
2. **Near hit** — no exact scope match; similarity search across that library's
   recipes finds an adjacent transition (we know `2.11→3.0`, we are attempting
   `2.11→3.1`). Enters the prompt explicitly labelled lower-confidence.
3. **Miss** — cold repair at full price. On success it produces a new
   *provisional* recipe.

`ledger.hit` ∈ `{exact, near, miss}` is recorded on every job. It is the
independent variable of the cost curve.

### Promotion

A recipe is `provisional` when first written. It becomes `verified` after **two
independent confirmations**: two repositories, neither of them the one that
produced the recipe, where the recipe was retrieved *and* the repair then
succeeded. The originating repository does not count towards its own recipe —
it is the hypothesis, not evidence for it. A retrieval that is followed by
`REPAIR_EXHAUSTED` counts as neither confirmation nor refutation; it is recorded
against the recipe so a consistently unhelpful one is visible.

Provisional recipes are offered to the repair agent as a hypothesis; verified
recipes as prior art. Promotion is the Librarian's second job and the only thing
that writes the `verified` topic.

## 5. Registry, identity, gateway, telemetry

**Agent Registry.** Each of the four agents is registered and versioned. A
version is `(prompt hash, model id, policy ruleset hash, tool set)`. The worker
resolves the version at job start and stamps it into the `RepoJob`. This is not
rubric decoration — the benchmark's held-out methodology is meaningless unless a
number can be attributed to a specific agent version.
*Fallback:* if the preview API cannot express this, versioned `agents/*.yaml` in
repo plus a Firestore `agent_versions` collection, registered best-effort. The
Registry must never be able to block a run.

**Agent Identity.** Four service accounts with genuinely different powers, using
IAM Conditions on `aiplatform.googleapis.com/memoryScope`:

- `nightshift-repair` — Ledger read; sandbox write; **no** Ledger write.
- `nightshift-librarian` — Ledger write; **no** repository access of any kind.
- `nightshift-reviewer` — reads both; writes nothing.
- `nightshift-triage` — OSV and Gemma only; no repository, no Ledger.

An agent that cannot write to the Ledger cannot poison it, and that is a property
of the IAM policy rather than of the prompt.

**Agent Gateway + Model Armor.** Repository file contents, docstrings, READMEs
and test output are all attacker-controllable text that we feed to a model with
write access to a sandbox. That is a real injection surface, not a hypothetical
one. All untrusted content passes a Model Armor template (prompt injection,
jailbreak, sensitive data) before entering a prompt.

Model Armor does **not** replace the policy engine, and the distinction is worth
stating in the write-up: Model Armor is the first line and inspects *content*
("this docstring is trying to persuade you"); the policy engine is the last line
and inspects *actions* ("you may not write to a test file, whatever you were
persuaded of"). Two layers, different failure modes.

**Agent Observability.** OpenTelemetry spans, exported to Cloud Trace:

```
job ─▶ phase ─▶ agent.turn ─▶ tool.call
```

Attributes: `agent.name`, `agent.version`, `ledger.hit`, `policy.rule`,
`tokens`, `outcome`. The cost curve is a query over span attributes — the
telemetry is the source of the headline metric, not an illustration of it. A
judge can click into repository #12's trace and see `ledger.hit=exact` next to a
one-turn repair.

## 6. What changes in existing code

| File | Change |
|---|---|
| `services/worker/main.py` | Implement `repair` and `open_pull_request`. Insert Ledger lookup before the loop, Librarian call and Reviewer gate after it. |
| `services/worker/agent.py` | Build all four agents; recipe injection into `REPAIR_INSTRUCTION`. |
| `packages/nightshift_core/models.py` | `RepoJob` gains `agent_versions`, `ledger_hit`. `Outcome` unchanged. |
| `packages/nightshift_core/policy.py` | New rules: Ledger write denied to every agent but the Librarian; Model Armor verdict as a precondition. |
| `packages/nightshift_core/ledger.py` | **New.** Memory Bank + Firestore ledger, three-tier retrieval, promotion. |
| `packages/nightshift_core/telemetry.py` | **New.** OTel setup and span helpers. |
| `packages/nightshift_core/config.py` | Split `gemini_model` into `repair_model` (Flash) and `escalation_model` (Pro); add `librarian_model`, `reviewer_model`, Model Armor template id. Mirror into `.env.example`. |
| `infra/deploy.sh` | Four service accounts, IAM Conditions, Model Armor template, registry registration. |
| `services/scanner/main.py` | Implement the four stubs. |

## 7. Demo

The fleet is seeded so that roughly twelve repositories share the
`jinja2 2.11→3.1` transition — found by `scripts/probe_fleet.py` against real
forks, because that clustering is a fact about the ecosystem rather than a
staging trick. Run in order:

- **#1** — miss. Four attempts, ~40k tokens. Writes a provisional recipe.
- **#2, #3** — provisional hit. Two attempts. Third confirmation promotes it.
- **#4 onward** — verified hit. One attempt, ~2k tokens.
- **#12** — the curve, rendered live from Cloud Trace.

## 8. Sequencing and the cut line

Block-level, per CLAUDE.md §9.

Each block gets its own implementation plan; this document is the design for all
four, not a plan for any one of them. Block 1's plan is written first and the
later ones only after the block before them has landed — the Ledger's interface
should be shaped by a repair loop that exists, not by one we imagined.

1. **Block 1 — the repair loop.** `repair` and `open_pull_request`, one repository
   end to end, real PR. Nothing in this document matters without it.
2. **Block 2 — the Ledger.** Librarian write path, three-tier read path, OTel
   spans. The curve becomes measurable.
3. **Block 3 — governance.** Registry, four identities, Model Armor, Reviewer.
4. **Block 4 — showcase.** Fleet run, freeze, video, write-up.

**The cut line is 27 August.** If Block 2 is not working by then, we ship Block 1
plus governance, drop the curve to a smaller N, and report it as measured rather
than staging it. A smaller honest curve beats a larger staged one, and the
benchmark methodology in `benchmark/README.md` already commits us to that.

## 9. Risks

| Risk | Mitigation |
|---|---|
| **The repair loop is still unbuilt.** Everything here sits on top of it. | Block 1 first, no exceptions. The cut line above. |
| Memory Bank and Agent Registry are public preview — quotas and API stability unknown. | Every integration gets a local fallback. A preview outage degrades the fleet to cold repair; it never stops a run. |
| **Recipe poisoning** — one bad recipe learned early degrades every later repair. | Provisional recipes are hints, not instructions. Verification needs three independent confirmations. The suite remains the only success criterion, so a bad recipe costs attempts and cannot produce a false green. |
| Scope creep: four agents is four times the surface. | Triage and Repair already existed. Librarian and Reviewer are each one prompt plus one call site. If either slips, the fleet still runs without it. |
| The curve looks staged. | It is derived from Cloud Trace spans on real forks, and the held-out third of the benchmark is run last. Show the trace, not a spreadsheet. |
