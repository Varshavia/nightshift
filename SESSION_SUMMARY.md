# Session Summary

**Read this before doing anything. Written by humans and agents, for both.**

`CLAUDE.md` is the durable *design* of Nightshift — what it is and what must not
drift. This file is the durable *state* of it — what is actually true today and
what happened to get here. Two people work on this repository at different hours
and neither sees the other's session. This file is the entire handoff.

---

## How to use this file

1. **Read `NOW` first.** It is the only place that claims to describe the present.
2. **Skim the log** down to the last entry you recognise. That is what changed
   while you were away.
3. **Before you finish a session:** append an entry to the top of the log and
   rewrite `NOW` in place. A session that ends without this has silently broken
   the other developer's next session.
4. **Never edit someone else's log entry.** Correct it in a new one instead — if
   an entry turns out to be wrong, that fact is itself worth recording.
5. **Write what a stranger needs**, not what you will remember. The reader is a
   teammate at 2am, or an agent with no memory of you.

`NOW` is overwritten. The log is append-only. Keeping both is deliberate: an
append-only file alone always decays into "which of these six entries is still
true?", and a status block alone loses the reasoning.

---

## NOW

**Last updated:** 2026-08-19 · Etka

| | |
|---|---|
| **Branch** | `docs/migration-ledger` — design and docs only, no code yet. `main` is at 14 commits. |
| **Green** | Core domain, policy engine, OSV client, config/stores, manifest parsing, worker toolchain (clone · build · test · upgrade), zero-token fleet probe, benchmark Tier A case #1. |
| **Direction** | **The Migration Ledger — approved.** Spec: `docs/superpowers/specs/2026-08-19-migration-ledger-design.md`. ADR 0004. Four agents: Triage, Repair, Librarian, Reviewer. |
| **Next action** | **Execute Block 1** — `docs/superpowers/plans/2026-08-19-block-1-repair-loop.md`, seven tasks, TDD. Nothing else in the design matters until `make run-local` opens a real PR. |
| **Not built** | The repair loop (Block 1). The Ledger (Block 2). Registry, identities, Model Armor, Reviewer (Block 3). Scanner's `load_fleet` / `read_manifests` / `publish`. API approvals. Fork-pool scripts. `dashboard/`. |

**Cut line: 27 August.** If Block 2 is not working by then, ship Block 1 plus
governance, drop the curve to a smaller N, and report it as measured. See §8 of
the spec.

**Resolved 19 Aug — `make check` is green.** `.venv` created with
`python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"`.
**113 tests pass**, ruff clean, `mypy --strict` clean across 28 source files.
The old "88 tests" figure was stale, not wrong-in-kind: 80 test *functions*
expand to 113 test *cases* through parametrization. Quote **113 tests** from
here on, and re-run before quoting it to a judge.

**Local dev note:** the repo needs `.venv` (gitignored). The system `python3`
on this machine is 3.11.3 with no pytest — running `pytest` outside the venv
fails confusingly. Use `.venv/bin/python -m pytest`, or activate first.

---

## Log

### 2026-08-19 · Etka · Ledger design approved; Block 1 plan written; docs updated

**Did:** Approved the Migration Ledger design and wrote everything downstream of
it. No implementation code yet — deliberately.

- `docs/superpowers/plans/2026-08-19-block-1-repair-loop.md` — Block 1, seven
  TDD tasks with real test and implementation code in every step.
- `docs/decisions/0004-the-migration-ledger.md` — the ADR, per CLAUDE.md §6.
- `CLAUDE.md` — §1 (the second insight), §3 (two new non-negotiables), §4 (four
  agents, three-tier retrieval, the two guard layers), §5, §8 (blocks rewritten
  around the Ledger, cut line recorded). **§7 no longer carries state** — it
  points here instead, which closes the drift logged yesterday.
- `README.md` — the Ledger section, agent table, honest status table.
- `docs/architecture.md` — a full Ledger section and the Model Armor / policy
  engine distinction.
- `docs/architecture.mmd` + re-rendered `architecture.png`.

**Why:** The design was agreed but lived in one spec file. Every other document
still described a single-agent fleet, and a judge reads the README and the
diagram before anything else.

**Three defects the plan's self-review caught** (worth knowing, they would each
have cost an hour):
1. Task 5 used `settings.repair_model` before Task 6 created it — the config
   split moved into Task 1.
2. `services.worker.main` and `services.worker.repair` each import `run_tests`
   into their own namespace, so patching one does not reach the other. The
   worker tests would have silently run real pytest against an empty directory.
   Fixed with a `patch_suite` helper that patches both.
3. `main.py` needed a `PullRequestBlocked` import for the `POLICY_BLOCKED` path.

**Also:** first render of the new diagram was worse than the old one — mermaid
scattered the spine and drew a phantom `UPGRADE → Reviewer` edge. Restructured
so the worker is one vertical spine and the Ledger connects only by dotted
retrieval lines. Look at the PNG after every `make diagram`; do not assume.

**State:** No implementation code changed. `make check` not run (see NOW).

**Next:** Execute the Block 1 plan, task by task, TDD. Start with Task 1 — the
policy-gated tool layer.

**Watch out:** Task 6 wires ADK, and the `LlmAgent` / `FunctionTool` / `run`
surface in the plan is written from documentation, not from the installed
package. Verify against `google-adk` before concluding the plan is wrong — and
change only `GeminiRepairAgent.attempt`. The `RepairAgent` Protocol is what
keeps that churn out of the rest of the code.

---

### 2026-08-19 · Etka · Migration Ledger design written, awaiting review

**Did:** Verified the Gemini Enterprise Agent Platform products against live
docs rather than memory, then wrote the full design to
`docs/superpowers/specs/2026-08-19-migration-ledger-design.md`.

**Why:** Chose "go big" on 18 Aug but the design was unwritten, and two of its
load-bearing assumptions were about products I only half-remembered.

**What the doc check changed:**
- Memory Bank scopes are **exact-match key/value maps**, so
  `{library, from_version, to_version}` is a precise retrieval key — with
  similarity search as a genuine second tier for adjacent transitions. The
  three-tier retrieval design exists because of this, not despite it.
- IAM Conditions can gate on `aiplatform.googleapis.com/memoryScope`, so
  "repair agent reads recipes, Librarian writes them, neither can do the
  other's job" is enforceable in IAM. Agent Identity became a real control
  rather than a narration job.
- Agent Registry and Memory Bank are both **public preview** — quotas and API
  stability unknown. Every integration in the design has a local fallback.

**The design, in one line:** every successful repair is generalized by a new
**Librarian** agent into a migration recipe in Memory Bank, so the fortieth
repository hitting `jinja2 2.11→3.1` starts from the answer instead of the
traceback — and cost per repo falls as the fleet works.

**State:** No code changed. Spec is uncommitted and unreviewed. `Outcome`
deliberately left closed — a Reviewer block maps to `POLICY_BLOCKED` rather than
earning a new enum member.

**Next:** Human reviews the spec. Then Block 1 plan — the repair loop **only**.
The Ledger's interface should be shaped by a repair loop that exists.

**Watch out:** The temptation is to start on the Librarian because it is the
interesting part. It sits on top of a repair loop that does not exist yet. Block
1 first — the cut line assumes it.

---

### 2026-08-18 · Etka · direction reset toward the full track-3 platform

**Did:** Audited the repository against the hackathon's category-3 text
(Fortified Enterprise Fleet) rather than against `CLAUDE.md` §2 alone. The track
names a specific component vocabulary that our durable memory was not carrying:
Agent Registry, Agent Runtime, Memory Bank, Agent Identity, Agent Gateway, Model
Armor, Agent Observability. Mapped each to what exists.

**Why:** `CLAUDE.md` §2 recorded only the three universal requirements (Gemini
3.5+, a Google agent framework, one GCP service). Every session since has been
optimising against an incomplete rubric.

**Findings:**
- *Strong already:* Agent Runtime (Cloud Run Jobs + Pub/Sub + Firestore phase
  checkpoints) and Agent Gateway (`policy.py` is unified policy enforcement; it
  simply never uses that word).
- *Designed, not built:* Memory Bank, keyed `(library, from→to)`.
- *Partial:* Agent Identity — three least-privilege service accounts in
  `infra/deploy.sh`, no per-agent identity.
- *Absent:* Model Armor, Agent Registry, any OpenTelemetry instrumentation.
- *Note:* the "What to Build" paragraph names **Gemini 3.5 Flash**; our config
  defaults to `gemini-3.5-pro`. Both satisfy the hard requirement ("3.5 or
  newer"), but Flash is cheaper and is the model the brief names.

**Decision:** the current design is too thin for the track — one agent type run
N times is horizontal scale, not "a network of institutional agents cataloged
for cross-department use". Going big on both axes: a multi-agent catalog **and**
an institutional memory layer **and** governance. Architecture design is in
progress and not yet agreed; nothing implemented against it.

**State:** No code changed. Docs only — this file and `CLAUDE.md` §0.

**Next:** Finish the design, write it to `docs/superpowers/specs/`, then decide
what Block 1 becomes.

**Watch out:** 13 days to submission (31 Aug), 10 to the cloud-credit cutoff
(28 Aug). Going big while the repair loop is still unbuilt is the real risk in
this decision — `CLAUDE.md` §3 says the repair loop is the product, and no new
layer may be allowed to push it later.

---

### Template — copy this

```markdown
### YYYY-MM-DD · <name> · <one-line what>

**Did:**       what you actually changed, in files a reader can open
**Why:**       the reasoning a stranger cannot reconstruct from the diff
**State:**     make check green / red — and the evidence, not the assumption
**Next:**      the single thing you would do first if you sat down again now
**Watch out:** the trap you nearly fell into, or the thing you left half-done
```
