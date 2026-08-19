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
| **Branch** | `feat/repair-loop`, **pushed**. Branched off `docs/migration-ledger`, which is also pushed. **No PRs opened yet** — deliberate. `main` still at 14 commits. |
| **Check** | **Green. 176 tests**, ruff clean, `mypy --strict` clean over 39 files. Run it with `.venv/bin/python -m pytest`. |
| **Block 1** | **All 7 tasks implemented, committed, pushed.** Repair loop, policy-gated tools, diff capture, PR body, PR opening, ADK agent, triage, end-to-end wiring. |
| **Block 1 remaining** | **The live run.** Nobody has watched `make run-local` open a real pull request. It needs a `GITHUB_TOKEN` and a fork to point at, and `scripts/build_fork_pool.py` is still a stub. **Do not call Block 1 finished until a human has seen the PR.** |
| **Next action** | Either (a) build a small fork pool and do the live run, closing Block 1 honestly, or (b) start Block 2 — the Ledger. (a) first if you have a GitHub token; the whole design rests on the loop working in the wild. |

**Cut line: 27 August.** If Block 2's Ledger is not working by then, ship what
exists plus governance and report a smaller curve as measured. See §8 of the
spec.

### How to pick this up cold

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest          # expect 176 passed
```

Read in this order: this file → `CLAUDE.md` → the spec
(`docs/superpowers/specs/2026-08-19-migration-ledger-design.md`) → the plan
(`docs/superpowers/plans/2026-08-19-block-1-repair-loop.md`, which now carries a
STATUS banner and ticked checkboxes showing exactly what landed).

**Local dev notes**
- The system `python3` here is 3.11.3 with no pytest. Always use `.venv/bin/python`.
- `make run-local` needs `NIGHTSHIFT_WORKSPACE_ROOT` set to something writable
  (`/tmp/nightshift`), plus `GITHUB_TOKEN` and `NIGHTSHIFT_FORK_ORG`.
- `google-adk` is now in the `dev` extra, not just the worker requirements — the
  suite asserts the agent is built with exactly the three policy-gated tools,
  and that assertion is worthless if CI cannot import the package and skips it.

### What Block 1 actually built

| File | What it is |
|---|---|
| `services/worker/tools.py` | The agent's only hands. Every action becomes a `ToolCall` the policy engine rules on; denials come back as readable text, not exceptions. |
| `services/worker/repair.py` | The bounded loop. The **suite** decides success, never the agent. Reaches the agent through a `RepairAgent` Protocol so it tests with no token spent. |
| `services/worker/agent.py` | `GeminiRepairAgent`, real ADK. Flash for attempts 1–2, Pro after. |
| `services/worker/pull_request.py` | Body rendering (pure) and `open_pr`. Policy checked **before** branching. |
| `services/worker/toolchain.py` | Gained `capture_diff` / `diff_stats`. |
| `services/scanner/main.py` | `triage` — severity floor only; the Gemma pass is Block 3. |

---

## Log

### 2026-08-19 · Etka · Block 1 implemented end to end, all 7 tasks pushed

**Did:** Executed the whole Block 1 plan, TDD, one commit per task, pushed to
`feat/repair-loop`. 113 → **176 tests**.

**Verified for real, not just unit-tested.** Ran the non-model pipeline against
`benchmark/cases/jinja2-2.11-to-3.1`:

```
BUILD      pip install -r requirements.txt -> 0
BASELINE   passed=True   exit=0   collected=True
UPGRADE    manifests changed: ['requirements.txt']
VERIFY     passed=False  exit=2   Interrupted: 1 error during collection
```

That red suite is exactly what `repair()` is handed. Everything up to the model
call now works on a real case.

**Five bugs found while building. Each cost real time; none is obvious:**

1. **Default arguments bind at definition time.** `run_repair_loop` took
   `run_suite=run_tests` as a default, so monkeypatching
   `services.worker.repair.run_tests` never reached it and the tests would have
   silently run the real pytest against an empty directory. Both `run_suite` and
   `capture` are now resolved inside the function. **If you add another injected
   default anywhere, do the same.**
2. **An empty `MemoryJobStore` is falsy** — it defines `__len__`. A
   `store or MemoryJobStore()` helper silently swapped in a fresh store and lost
   every checkpoint under test. Use `x if x is not None else y` for anything
   with `__len__`.
3. **The ceiling was off by one.** `Budget.attempts` counts *completed*
   attempts and the engine denies when that count *exceeds* the ceiling, so
   checking the live budget let a fourth attempt run under a ceiling of three.
   The loop now asks about the attempt it is *about to* make.
4. **The policy engine was built before the clone existed**, against a
   hard-coded `/workspace`. On any local run every path read as a sandbox
   escape. It is now constructed after `clone` returns, with the real path.
5. **`make run-local` could never have worked** — running the script by path
   leaves `services` unimportable. The Makefile now uses `-m scripts.run_local`.

**The plan was written from ADK documentation and the docs were wrong about the
invocation.** Installed `google-adk` 2.7.1 and checked: agents run through a
`Runner` with an explicit session, not `agent.run(prompt)`. `create_session_sync`
exists but is deprecated, so we use the async one via `asyncio.run`.
`FunctionTool` must be imported from `google.adk.tools.function_tool` because the
package builds its `__all__` lazily and no static checker can see it otherwise.
`LlmAgent(name=, model=, instruction=, tools=)` was correct as written.

**Also corrected in the plan itself** (so the document stays trustworthy): a
miscounted `diff_stats` expectation — that fixture has 2 added lines, not 3.

**State:** `make check` green — 176 tests, ruff clean, mypy --strict clean.
Everything pushed. No PRs opened, as asked.

**Next:** Either do the live run and close Block 1 honestly, or start Block 2.
The Ledger's first piece is `packages/nightshift_core/ledger.py` — three-tier
retrieval against Memory Bank, scoped `{library, from_version, to_version}`, with
Firestore holding confirmation counts. Spec §4. Write the Block 2 plan first;
do not improvise it.

**Watch out:**
- **Block 1 is not done.** Everything is implemented and tested, but no human
  has watched it open a pull request. Resist writing "Block 1 complete" anywhere
  a judge reads until that has happened.
- The repair agent's `attempt()` is the one method no test exercises — it needs
  a real model. The seams either side of it (`render_attempt_prompt`,
  `final_text`, `total_tokens`) are pure and covered, so when the live run
  misbehaves, suspect `attempt()` first.
- `services/worker/main.py` and `services/worker/repair.py` each import
  `run_tests` into their own namespace. Patch **both** — `tests/test_worker_handle.py`
  has a `patch_suite` helper that does it.

---

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
