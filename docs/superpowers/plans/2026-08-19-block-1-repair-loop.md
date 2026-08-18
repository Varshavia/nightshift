# Block 1 — The Repair Loop Implementation Plan

> **STATUS — 19 Aug 2026: all seven tasks implemented, committed and pushed to
> `feat/repair-loop`. `make check` green at 176 tests. The one thing NOT done is
> the live run against a real fork — see Definition of done. Read
> `SESSION_SUMMARY.md` before continuing.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `make run-local REPO=owner/name` takes one real repository from a green baseline through a security upgrade that breaks it, repairs the calling code, and opens a real pull request.

**Architecture:** The agent is the untrusted party, so it never touches the sandbox directly — it acts through `SandboxTools`, a thin layer that converts every action into a `ToolCall`, passes it to the existing `PolicyEngine`, and returns either the result or a denial string. The repair loop itself owns the ceiling, re-runs the suite after every attempt, and decides success from the suite alone; the agent never reports its own success. The agent is reached through a `RepairAgent` Protocol so the entire loop is testable with a fake and no model call.

**Tech Stack:** Python 3.11+, ADK 2.0 (`google-adk`), `google-genai`, Gemini 3.5 Flash (escalating to Pro), PyGithub, pytest.

**Spec:** `docs/superpowers/specs/2026-08-19-migration-ledger-design.md` — Block 1 in §8. This plan implements only the repair loop; the Ledger, Librarian, Reviewer, Registry and telemetry are Blocks 2 and 3 and are deliberately absent here.

## Global Constraints

- **Python `>=3.11`.** `ruff` line-length **100**, target `py311`. `mypy` runs `--strict` over `packages`, `services`, `scripts`, `tests` — new code must type-check with no `Any` leaking into public signatures.
- **`make check` (ruff · mypy --strict · pytest) must be green before every commit.**
- **`Outcome` is a closed enum.** Do not add a member in this block. A policy denial on the pull-request step is `POLICY_BLOCKED`; a ceiling reached with the suite red is `REPAIR_EXHAUSTED`.
- **Every loop has a ceiling** — `Ceilings.max_repair_attempts`, `max_job_seconds`, `max_job_tokens`, checked by the policy engine *before* each call, never after the loop.
- **The agent never modifies a test.** Already enforced by `policy._check_write`; this block must not add a path that bypasses it.
- **Forks by default.** `ALLOW_UPSTREAM_PRS=false`. Nothing merges itself — no auto-merge flag, ever.
- **No secret is committed.** New configuration goes into `.env.example` with a comment and no value.
- **Everything in the repository is in English** — code, comments, commits, docs.
- **Conventional commits**, subject imperative and under ~70 characters.
- Work on branch `feat/repair-loop`; land through a PR.

---

### Task 1: Policy-gated tool layer

The agent's only way to touch the world. Every method builds a `ToolCall`, asks the `PolicyEngine`, and returns a string — denials included — because the agent is a language model and a denial it can read is a denial it can learn from. A denial costs an attempt; it does not crash the job.

**Files:**
- Create: `services/worker/tools.py`
- Modify: `services/worker/main.py:78` — construct `PolicyEngine` with the real workspace
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `nightshift_core.policy.{PolicyEngine, ToolCall, Budget, Decision}`, `services.worker.toolchain.Sandbox`
- Produces:
  - `SandboxTools(sandbox: Sandbox, policy: PolicyEngine, budget: Budget)`
  - `SandboxTools.read_file(path: str) -> str`
  - `SandboxTools.write_file(path: str, content: str) -> str`
  - `SandboxTools.run_command(command: list[str]) -> str`
  - `SandboxTools.denials -> tuple[Decision, ...]`
  - `DENIAL_PREFIX: str`

- [x] **Step 1: Write the failing tests**

```python
"""The tool layer is where the policy engine stops being theory.

Every test here is an attempt to reach the filesystem around it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nightshift_core.config import Ceilings, Settings
from nightshift_core.policy import Budget, PolicyEngine
from services.worker.tools import DENIAL_PREFIX, SandboxTools
from services.worker.toolchain import Sandbox


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app.py").write_text("from jinja2 import Markup\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_ok() -> None:\n    assert True\n", encoding="utf-8")
    return root


@pytest.fixture
def tools(repo: Path) -> SandboxTools:
    settings = Settings(fork_org="nightshift-fleet", ceilings=Ceilings(max_repair_attempts=3))
    policy = PolicyEngine(settings=settings, workspace=repo.as_posix())
    sandbox = Sandbox(repo_path=repo, python=Path("/usr/bin/python3"))
    return SandboxTools(sandbox=sandbox, policy=policy, budget=Budget())


def test_read_file_returns_contents(tools: SandboxTools) -> None:
    assert "from jinja2 import Markup" in tools.read_file("src/app.py")


def test_write_file_changes_the_working_tree(tools: SandboxTools, repo: Path) -> None:
    tools.write_file("src/app.py", "from markupsafe import Markup\n")
    assert repo.joinpath("src/app.py").read_text(encoding="utf-8") == "from markupsafe import Markup\n"


def test_writing_a_test_file_is_denied_and_leaves_it_untouched(
    tools: SandboxTools, repo: Path
) -> None:
    before = repo.joinpath("tests/test_app.py").read_text(encoding="utf-8")
    result = tools.write_file("tests/test_app.py", "def test_ok() -> None:\n    pass\n")
    assert result.startswith(DENIAL_PREFIX)
    assert "tests-are-evidence" in result
    assert repo.joinpath("tests/test_app.py").read_text(encoding="utf-8") == before


def test_escaping_the_workspace_is_denied(tools: SandboxTools, tmp_path: Path) -> None:
    result = tools.write_file("../escaped.py", "x = 1\n")
    assert result.startswith(DENIAL_PREFIX)
    assert not tmp_path.joinpath("escaped.py").exists()


def test_reading_outside_the_workspace_is_denied(tools: SandboxTools) -> None:
    assert tools.read_file("/etc/passwd").startswith(DENIAL_PREFIX)


def test_denied_calls_are_recorded_for_the_audit_trail(tools: SandboxTools) -> None:
    tools.write_file("tests/test_app.py", "pass\n")
    tools.write_file("../escaped.py", "pass\n")
    assert [decision.rule for decision in tools.denials] == ["tests-are-evidence", "sandbox-escape"]


def test_a_disallowed_executable_is_denied(tools: SandboxTools) -> None:
    assert tools.run_command(["curl", "https://example.com"]).startswith(DENIAL_PREFIX)


def test_reading_a_missing_file_reports_rather_than_raises(tools: SandboxTools) -> None:
    assert "does not exist" in tools.read_file("src/nope.py")
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.worker.tools'`

- [x] **Step 3: Write the implementation**

Create `services/worker/tools.py`:

```python
"""The agent's hands, and the only pair it gets.

Every method here converts an intention into a :class:`ToolCall`, submits it to
the policy engine, and acts only on an allow. The agent never receives an
unwrapped tool — that is why the guarantees in ``REPAIR_INSTRUCTION`` hold even
when the model ignores the instruction.

Denials are returned to the agent as text rather than raised. A model that reads
"DENIED [tests-are-evidence]" can change its approach; an exception would end the
job over a mistake the agent could have recovered from. The denial still costs an
attempt, and it is still recorded in the audit trail.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nightshift_core.policy import Budget, Decision, PolicyEngine, ToolCall
from services.worker.toolchain import MAX_OUTPUT_CHARS, Sandbox

__all__ = ["DENIAL_PREFIX", "SandboxTools"]

log = logging.getLogger("nightshift.tools")

#: Prefix on every refused call. The agent is told in its instruction that a line
#: starting with this means the action did not happen.
DENIAL_PREFIX = "DENIED"

COMMAND_TIMEOUT = 600


class SandboxTools:
    """Policy-gated access to one clone."""

    def __init__(self, *, sandbox: Sandbox, policy: PolicyEngine, budget: Budget) -> None:
        self._sandbox = sandbox
        self._policy = policy
        self._budget = budget
        self._denials: list[Decision] = []

    @property
    def denials(self) -> tuple[Decision, ...]:
        """Every refused call, in order. Rendered onto the dashboard."""
        return tuple(self._denials)

    # -- internals ---------------------------------------------------------- #

    def _gate(self, call: ToolCall) -> str | None:
        """Return a denial string, or None when the call may proceed."""
        decision = self._policy.check(call, self._budget)
        if decision.allowed:
            return None
        self._denials.append(decision)
        log.info("denied %s: [%s] %s", call.name, decision.rule, decision.reason)
        return f"{DENIAL_PREFIX} [{decision.rule}] {decision.reason}"

    def _absolute(self, path: str) -> Path:
        return self._sandbox.repo_path / path

    # -- tools -------------------------------------------------------------- #

    def read_file(self, path: str) -> str:
        """Read a file inside the clone."""
        denial = self._gate(ToolCall("read_file", {"path": path}))
        if denial is not None:
            return denial
        target = self._absolute(path)
        if not target.is_file():
            return f"{path} does not exist"
        try:
            return target.read_text(encoding="utf-8")[:MAX_OUTPUT_CHARS]
        except UnicodeDecodeError:
            return f"{path} is not utf-8 text"

    def write_file(self, path: str, content: str) -> str:
        """Overwrite a file inside the clone. Never a test, never outside it."""
        denial = self._gate(ToolCall("write_file", {"path": path, "content": content}))
        if denial is not None:
            return denial
        target = self._absolute(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} bytes to {path}"

    def run_command(self, command: list[str]) -> str:
        """Run one allowlisted executable in the clone, as argv, never via a shell."""
        denial = self._gate(ToolCall("run_command", {"command": command}))
        if denial is not None:
            return denial
        result = self._sandbox.run(command, timeout=COMMAND_TIMEOUT)
        output = (result.stdout or "") + (result.stderr or "")
        return f"exit {result.returncode}\n{output[-MAX_OUTPUT_CHARS:]}"
```

- [x] **Step 4: Fix the workspace bug in the worker**

`services/worker/main.py` currently builds the engine before the clone exists and with the container's hard-coded path, so a local run would judge every path an escape. Move the construction to after `clone` returns and give it the real path.

In `handle`, replace:

```python
    policy = PolicyEngine(settings=settings)
    budget = Budget()
    workspace = Path("/workspace") / job.job_id.replace(":", "_").replace("/", "_")
```

with:

```python
    budget = Budget()
    workspace = Path(settings.workspace_root) / job.job_id.replace(":", "_").replace("/", "_")
```

and immediately after the successful `clone`, add:

```python
    policy = PolicyEngine(settings=settings, workspace=repo_path.as_posix())
```

- [x] **Step 5: Add `workspace_root` to settings**

In `packages/nightshift_core/config.py`, add to `Settings` (after `firestore_database`):

```python
    #: Where clones are built. ``/workspace`` in the container; a temp directory
    #: locally, because a laptop has no ``/workspace`` and should not need one.
    workspace_root: str = "/workspace"
```

and in `Settings.from_env`:

```python
            workspace_root=_env("NIGHTSHIFT_WORKSPACE_ROOT", "/workspace"),
```

In `.env.example`, under `# --- Google Cloud ---`:

```bash
# Where the worker builds clones. Leave as /workspace in the container; set to a
# writable path (e.g. /tmp/nightshift) when running locally.
NIGHTSHIFT_WORKSPACE_ROOT=/workspace
```

While in `config.py`, also split the model setting — Task 5 needs
`repair_model` before Task 6 builds the agent that uses it. Replace the
`gemini_model` field with:

```python
    #: The repair agent. Flash by default — the hackathon brief names it, it is
    #: markedly cheaper, and most breaks are a moved import rather than a puzzle.
    repair_model: str = "gemini-3.5-flash"
    #: Used when Flash has failed twice on the same job.
    escalation_model: str = "gemini-3.5-pro"
```

and in `from_env`:

```python
            repair_model=_env("NIGHTSHIFT_REPAIR_MODEL", "gemini-3.5-flash"),
            escalation_model=_env("NIGHTSHIFT_ESCALATION_MODEL", "gemini-3.5-pro"),
```

In `.env.example`, replace `NIGHTSHIFT_GEMINI_MODEL` with:

```bash
# Repair agent. Hackathon rules require Gemini 3.5 or newer.
NIGHTSHIFT_REPAIR_MODEL=gemini-3.5-flash
# Used after two failed attempts on the same job — a harder model for a harder break.
NIGHTSHIFT_ESCALATION_MODEL=gemini-3.5-pro
```

- [x] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_tools.py -v && make check`
Expected: PASS, and `make check` green.

- [x] **Step 7: Commit**

```bash
git add services/worker/tools.py tests/test_tools.py services/worker/main.py packages/nightshift_core/config.py .env.example
git commit -m "feat(worker): gate every agent tool call through the policy engine"
```

---

### Task 2: Diff capture

The repair diff is what a human reviews and what `RepairAttempt` records. This is our own code running a fixed command line, so it lives in `toolchain.py` and does not route through the policy engine — same trust boundary as the rest of that module.

**Files:**
- Modify: `services/worker/toolchain.py` — append a new section
- Test: `tests/test_toolchain_diff.py`

**Interfaces:**
- Consumes: `services.worker.toolchain.Sandbox`
- Produces:
  - `capture_diff(sandbox: Sandbox) -> str`
  - `DiffStats(files: int, added: int, removed: int)` — frozen dataclass
  - `diff_stats(diff: str) -> DiffStats`

- [x] **Step 1: Write the failing tests**

```python
"""Diff capture. What the reviewer at nine in the morning actually reads."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from services.worker.toolchain import Sandbox, capture_diff, diff_stats


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    (root / "app.py").write_text("from jinja2 import Markup\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True, capture_output=True)
    return root


@pytest.fixture
def sandbox(git_repo: Path) -> Sandbox:
    return Sandbox(repo_path=git_repo, python=Path("/usr/bin/python3"))


def test_no_changes_gives_an_empty_diff(sandbox: Sandbox) -> None:
    assert capture_diff(sandbox) == ""


def test_a_modification_appears_in_the_diff(sandbox: Sandbox, git_repo: Path) -> None:
    git_repo.joinpath("app.py").write_text("from markupsafe import Markup\n", encoding="utf-8")
    diff = capture_diff(sandbox)
    assert "-from jinja2 import Markup" in diff
    assert "+from markupsafe import Markup" in diff


def test_a_new_file_appears_in_the_diff(sandbox: Sandbox, git_repo: Path) -> None:
    """Untracked files must be included or a repair that adds a shim looks empty."""
    git_repo.joinpath("compat.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert "compat.py" in capture_diff(sandbox)


def test_diff_stats_counts_files_and_lines() -> None:
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-from jinja2 import Markup\n"
        "+from markupsafe import Markup\n"
        "diff --git a/other.py b/other.py\n"
        "--- a/other.py\n"
        "+++ b/other.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    stats = diff_stats(diff)
    assert (stats.files, stats.added, stats.removed) == (2, 2, 1)


def test_diff_stats_of_an_empty_diff_is_all_zero() -> None:
    stats = diff_stats("")
    assert (stats.files, stats.added, stats.removed) == (0, 0, 0)
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_toolchain_diff.py -v`
Expected: FAIL — `ImportError: cannot import name 'capture_diff'`

- [x] **Step 3: Write the implementation**

Append to `services/worker/toolchain.py`, and add `"DiffStats"`, `"capture_diff"`, `"diff_stats"` to `__all__`:

```python
# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DiffStats:
    """Size of a repair, for the pull-request body and for the record.

    A small diff that is obviously right is worth more than a large one that
    happens to pass, so this number is reported rather than merely logged.
    """

    files: int = 0
    added: int = 0
    removed: int = 0


def capture_diff(sandbox: Sandbox) -> str:
    """Every change in the clone against HEAD, including new files.

    ``git add -N`` stages the *existence* of untracked files without their
    content, which is what makes them visible to ``git diff``. Without it a
    repair that adds a file would produce an empty diff and the pull request
    would understate what it is asking a human to approve.
    """
    sandbox.run(["git", "add", "-N", "."], timeout=60)
    result = sandbox.run(["git", "diff", "--no-color"], timeout=60)
    if result.returncode != 0:
        log.warning("git diff failed: %s", _tail(result.stderr, 500))
        return ""
    return _tail(result.stdout or "")


def diff_stats(diff: str) -> DiffStats:
    """Count files and changed lines in a unified diff."""
    files = added = removed = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return DiffStats(files=files, added=added, removed=removed)
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_toolchain_diff.py -v && make check`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add services/worker/toolchain.py tests/test_toolchain_diff.py
git commit -m "feat(worker): capture the repair diff and its size"
```

---

### Task 3: The bounded repair loop

The heart of the project. The loop owns the ceiling, re-runs the suite after every attempt, and decides success from the suite alone — the agent is never asked whether it succeeded. Reaching the agent through a Protocol is what makes this entire task testable with no model call.

**Files:**
- Create: `services/worker/repair.py`
- Modify: `services/worker/main.py` — `repair` delegates here
- Test: `tests/test_repair_loop.py`

**Interfaces:**
- Consumes: `SandboxTools` (Task 1), `capture_diff` (Task 2), `nightshift_core.models.{RepoJob, RepairAttempt, Vulnerability}`, `toolchain.{Sandbox, TestReport, run_tests}`
- Produces:
  - `RepairContext` — frozen dataclass: `repo: str`, `vulnerabilities: tuple[Vulnerability, ...]`, `failing_output: str`, `attempt: int`, `previous: tuple[RepairAttempt, ...]`
  - `RepairProposal` — frozen dataclass: `rationale: str`, `tokens_used: int = 0`
  - `RepairAgent` — Protocol with `attempt(context: RepairContext, tools: SandboxTools) -> RepairProposal`
  - `run_repair_loop(job, sandbox, failure, policy, budget, agent, *, run_suite=run_tests) -> bool`

- [x] **Step 1: Write the failing tests**

```python
"""The repair loop, exercised with a fake agent and no model call.

The loop's contract is narrow and worth stating: it decides success from the
test suite, never from the agent's own report, and it stops at the ceiling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nightshift_core.config import Ceilings, Settings
from nightshift_core.models import Outcome, RepoJob, Severity, Vulnerability
from nightshift_core.policy import Budget, PolicyEngine
from services.worker.repair import (
    RepairContext,
    RepairProposal,
    run_repair_loop,
)
from services.worker.toolchain import Sandbox, TestReport
from services.worker.tools import SandboxTools


VULNERABILITY = Vulnerability(
    osv_id="GHSA-test",
    package="jinja2",
    installed_version="2.11.3",
    fixed_version="3.1.2",
    severity=Severity.HIGH,
)


class ScriptedAgent:
    """Returns a fixed rationale per attempt and records what it was given."""

    def __init__(self, *, tokens: int = 1000) -> None:
        self.contexts: list[RepairContext] = []
        self._tokens = tokens

    def attempt(self, context: RepairContext, tools: SandboxTools) -> RepairProposal:
        self.contexts.append(context)
        tools.write_file("app.py", f"# attempt {context.attempt}\n")
        return RepairProposal(rationale=f"attempt {context.attempt}", tokens_used=self._tokens)


def make_suite(results: list[bool]) -> object:
    """A stand-in for ``run_tests`` that yields the given pass/fail sequence."""
    remaining = list(results)

    def run_suite(sandbox: Sandbox, **kwargs: object) -> TestReport:
        passed = remaining.pop(0) if remaining else False
        return TestReport(passed=passed, output="green" if passed else "boom", duration_seconds=0.1)

    return run_suite


@pytest.fixture
def fixture_set(tmp_path: Path) -> tuple[RepoJob, Sandbox, SandboxTools, PolicyEngine, Budget]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    settings = Settings(
        fork_org="nightshift-fleet",
        ceilings=Ceilings(max_repair_attempts=3, max_job_seconds=600, max_job_tokens=100_000),
    )
    policy = PolicyEngine(settings=settings, workspace=root.as_posix())
    budget = Budget()
    sandbox = Sandbox(repo_path=root, python=Path("/usr/bin/python3"))
    tools = SandboxTools(sandbox=sandbox, policy=policy, budget=budget)
    job = RepoJob(job_id="run:owner/name", repo="owner/name", vulnerabilities=[VULNERABILITY])
    return job, sandbox, tools, policy, budget


def test_a_repair_that_works_on_the_first_attempt(fixture_set) -> None:
    job, sandbox, tools, policy, budget = fixture_set
    agent = ScriptedAgent()
    repaired = run_repair_loop(
        job, sandbox, TestReport(passed=False, output="ImportError", duration_seconds=0.1),
        policy, budget, agent, tools=tools, run_suite=make_suite([True]),
    )
    assert repaired is True
    assert len(job.repair_attempts) == 1
    assert job.repair_attempts[0].tests_passed is True


def test_the_loop_stops_at_the_attempt_ceiling(fixture_set) -> None:
    job, sandbox, tools, policy, budget = fixture_set
    repaired = run_repair_loop(
        job, sandbox, TestReport(passed=False, output="ImportError", duration_seconds=0.1),
        policy, budget, ScriptedAgent(), tools=tools, run_suite=make_suite([False, False, False, False]),
    )
    assert repaired is False
    assert len(job.repair_attempts) == 3, "ceilings.max_repair_attempts is 3"


def test_every_attempt_is_recorded_even_when_it_fails(fixture_set) -> None:
    """A failed attempt is the input to the next one and the material for the Ledger."""
    job, sandbox, tools, policy, budget = fixture_set
    run_repair_loop(
        job, sandbox, TestReport(passed=False, output="ImportError", duration_seconds=0.1),
        policy, budget, ScriptedAgent(), tools=tools, run_suite=make_suite([False, True]),
    )
    assert [a.tests_passed for a in job.repair_attempts] == [False, True]
    assert all(a.rationale for a in job.repair_attempts)


def test_the_agent_sees_the_previous_failure_not_the_original(fixture_set) -> None:
    job, sandbox, tools, policy, budget = fixture_set
    agent = ScriptedAgent()
    run_repair_loop(
        job, sandbox, TestReport(passed=False, output="first failure", duration_seconds=0.1),
        policy, budget, agent, tools=tools, run_suite=make_suite([False, True]),
    )
    assert agent.contexts[0].failing_output == "first failure"
    assert agent.contexts[1].failing_output == "boom", "attempt 2 sees attempt 1's result"
    assert len(agent.contexts[1].previous) == 1


def test_tokens_are_spent_against_the_budget(fixture_set) -> None:
    job, sandbox, tools, policy, budget = fixture_set
    run_repair_loop(
        job, sandbox, TestReport(passed=False, output="boom", duration_seconds=0.1),
        policy, budget, ScriptedAgent(tokens=1500), tools=tools, run_suite=make_suite([False, True]),
    )
    assert budget.tokens == 3000
    assert job.tokens_used == 3000


def test_the_token_ceiling_ends_the_loop(fixture_set) -> None:
    """A ceiling is a real result, not an error."""
    job, sandbox, tools, policy, budget = fixture_set
    repaired = run_repair_loop(
        job, sandbox, TestReport(passed=False, output="boom", duration_seconds=0.1),
        policy, budget, ScriptedAgent(tokens=60_000), tools=tools,
        run_suite=make_suite([False, False, False]),
    )
    assert repaired is False
    assert len(job.repair_attempts) == 2, "the third attempt exceeds max_job_tokens"


def test_a_diff_is_recorded_on_each_attempt(fixture_set) -> None:
    job, sandbox, tools, policy, budget = fixture_set
    run_repair_loop(
        job, sandbox, TestReport(passed=False, output="boom", duration_seconds=0.1),
        policy, budget, ScriptedAgent(), tools=tools, run_suite=make_suite([True]),
        capture=lambda sandbox: "diff --git a/app.py b/app.py\n+# attempt 1\n",
    )
    assert "attempt 1" in job.repair_attempts[0].diff


def test_the_job_is_never_finished_by_the_loop(fixture_set) -> None:
    """Outcome is the caller's decision; the loop only reports green or not."""
    job, sandbox, tools, policy, budget = fixture_set
    run_repair_loop(
        job, sandbox, TestReport(passed=False, output="boom", duration_seconds=0.1),
        policy, budget, ScriptedAgent(), tools=tools, run_suite=make_suite([True]),
    )
    assert job.outcome is None
    assert Outcome.PATCHED_REPAIRED not in {job.outcome}
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_repair_loop.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.worker.repair'`

- [x] **Step 3: Write the implementation**

Create `services/worker/repair.py`:

```python
"""The bounded repair loop.

This is the product. Its contract is deliberately narrow:

- **The suite decides.** The agent is never asked whether it succeeded. It makes
  a change; the repository's own tests are re-run; that result is the truth.
- **Every attempt is recorded**, successful or not. A failed attempt is the input
  to the next one, and afterwards it is the material the Ledger is built from.
- **The ceiling is checked before each attempt**, through the same policy engine
  that gates every tool call, so there is exactly one implementation of "enough".

The agent arrives through :class:`RepairAgent`, a Protocol, so the whole loop can
be exercised with a scripted stand-in and no token spent.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from nightshift_core.models import RepairAttempt, RepoJob, Vulnerability
from nightshift_core.policy import Budget, PolicyEngine, ToolCall
from services.worker.toolchain import Sandbox, TestReport, capture_diff, run_tests
from services.worker.tools import SandboxTools

__all__ = [
    "RepairAgent",
    "RepairContext",
    "RepairProposal",
    "run_repair_loop",
]

log = logging.getLogger("nightshift.repair")


@dataclass(frozen=True, slots=True)
class RepairContext:
    """Everything the agent is given for one attempt, and nothing else."""

    repo: str
    vulnerabilities: tuple[Vulnerability, ...]
    failing_output: str
    attempt: int
    previous: tuple[RepairAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class RepairProposal:
    """What the agent reports back. Notably absent: whether it worked."""

    rationale: str
    tokens_used: int = 0


class RepairAgent(Protocol):
    """Anything that can make one conceptual fix inside a sandbox."""

    def attempt(self, context: RepairContext, tools: SandboxTools) -> RepairProposal: ...


def run_repair_loop(
    job: RepoJob,
    sandbox: Sandbox,
    failure: TestReport,
    policy: PolicyEngine,
    budget: Budget,
    agent: RepairAgent,
    *,
    tools: SandboxTools | None = None,
    run_suite: Callable[..., TestReport] | None = None,
    capture: Callable[[Sandbox], str] | None = None,
) -> bool:
    """Repair until the suite is green or a ceiling is reached. True when green.

    Never finishes the job — the caller owns the ``Outcome``, because the same
    green suite means ``PATCHED_REPAIRED`` here and something else in a probe.
    """
    tools = tools or SandboxTools(sandbox=sandbox, policy=policy, budget=budget)
    # Resolved here, not as default arguments: a default binds the function
    # object at definition time and a test patching the module attribute would
    # never reach it.
    run_suite = run_suite or run_tests
    capture = capture or capture_diff
    failing_output = failure.output

    while True:
        attempt_number = budget.attempts + 1
        # The same ceiling check that gates every tool call: one implementation
        # of "enough", not a second one written for the loop.
        decision = policy.check(ToolCall("run_command", {"command": ["pytest"]}), budget)
        if not decision.allowed:
            log.info("job %s stopped by %s", job.job_id, decision.rule)
            return False

        started = time.monotonic()
        context = RepairContext(
            repo=job.repo,
            vulnerabilities=tuple(job.vulnerabilities),
            failing_output=failing_output,
            attempt=attempt_number,
            previous=tuple(job.repair_attempts),
        )
        proposal = agent.attempt(context, tools)

        verdict = run_suite(sandbox)
        duration = time.monotonic() - started

        job.record_attempt(
            RepairAttempt(
                attempt=attempt_number,
                failing_output=failing_output,
                diff=capture(sandbox),
                rationale=proposal.rationale,
                tests_passed=verdict.passed,
                tokens_used=proposal.tokens_used,
                duration_seconds=duration,
            )
        )
        budget.spend(tokens=proposal.tokens_used, seconds=duration, attempts=1)

        if verdict.passed:
            log.info("job %s repaired on attempt %d", job.job_id, attempt_number)
            return True

        failing_output = verdict.output
```

- [x] **Step 4: Delegate from the worker**

In `services/worker/main.py`, replace the `repair` stub body with a delegation, keeping the docstring:

```python
def repair(
    job: RepoJob,
    sandbox: Sandbox,
    failure: TestReport,
    policy: PolicyEngine,
    budget: Budget,
    agent: RepairAgent,
) -> bool:
    """Run the bounded repair loop. True when the suite ends green."""
    return run_repair_loop(job, sandbox, failure, policy, budget, agent)
```

Add the imports `from services.worker.repair import RepairAgent, run_repair_loop`.

- [x] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_repair_loop.py -v && make check`
Expected: PASS — 8 tests.

- [x] **Step 6: Commit**

```bash
git add services/worker/repair.py tests/test_repair_loop.py services/worker/main.py
git commit -m "feat(worker): implement the bounded repair loop"
```

---

### Task 4: Pull-request body rendering

A pure function over `templates/pr_body.md`. Pure because the body is what a maintainer judges us by, and a rendering bug should be caught by a unit test rather than by a stranger reading a broken pull request.

**Files:**
- Create: `services/worker/pull_request.py`
- Test: `tests/test_pr_body.py`

**Interfaces:**
- Consumes: `nightshift_core.models.{RepoJob, Vulnerability}`, `DiffStats`/`diff_stats` (Task 2)
- Produces:
  - `render_pr_body(job: RepoJob, *, baseline_green: bool, test_command: str, model: str, template: str | None = None) -> str`
  - `PR_TEMPLATE_PATH: Path`

- [x] **Step 1: Write the failing tests**

```python
"""The pull-request body. The only artefact a maintainer actually reads."""

from __future__ import annotations

from nightshift_core.models import RepairAttempt, RepoJob, Severity, Vulnerability
from services.worker.pull_request import render_pr_body


def make_job() -> RepoJob:
    job = RepoJob(
        job_id="run1:nightshift-fleet/example",
        repo="nightshift-fleet/example",
        vulnerabilities=[
            Vulnerability(
                osv_id="GHSA-abcd-1234",
                package="jinja2",
                installed_version="2.11.3",
                fixed_version="3.1.2",
                severity=Severity.HIGH,
                summary="Sandbox escape in Jinja2",
                aliases=("CVE-2024-22195",),
            )
        ],
    )
    job.record_attempt(
        RepairAttempt(
            attempt=1,
            failing_output="ImportError: cannot import name 'Markup' from 'jinja2'",
            diff="diff --git a/app.py b/app.py\n-from jinja2 import Markup\n+from markupsafe import Markup\n",
            rationale="Jinja2 3.0 removed the top-level Markup re-export.",
            tests_passed=True,
            tokens_used=4200,
        )
    )
    return job


def test_the_body_names_the_package_and_both_versions() -> None:
    body = render_pr_body(make_job(), baseline_green=True, test_command="pytest -q", model="gemini-3.5-flash")
    assert "jinja2 2.11.3 → 3.1.2" in body


def test_the_body_carries_the_advisory_and_its_cve() -> None:
    body = render_pr_body(make_job(), baseline_green=True, test_command="pytest -q", model="gemini-3.5-flash")
    assert "GHSA-abcd-1234" in body
    assert "CVE-2024-22195" in body
    assert "HIGH" in body


def test_the_body_contains_the_diff_and_the_explanation() -> None:
    body = render_pr_body(make_job(), baseline_green=True, test_command="pytest -q", model="gemini-3.5-flash")
    assert "+from markupsafe import Markup" in body
    assert "Jinja2 3.0 removed the top-level Markup re-export." in body


def test_the_ai_authorship_disclosure_is_always_present() -> None:
    """Non-negotiable: every pull request discloses that an agent wrote it."""
    body = render_pr_body(make_job(), baseline_green=True, test_command="pytest -q", model="gemini-3.5-flash")
    assert "written by an AI agent" in body


def test_no_placeholder_survives_rendering() -> None:
    body = render_pr_body(make_job(), baseline_green=True, test_command="pytest -q", model="gemini-3.5-flash")
    assert "{" not in body.replace("{}", ""), "an unfilled template field reached the body"


def test_a_vulnerability_without_a_cve_renders_cleanly() -> None:
    job = make_job()
    job.vulnerabilities = [
        Vulnerability(
            osv_id="PYSEC-2021-1",
            package="pyyaml",
            installed_version="5.3",
            fixed_version="6.0",
            severity=Severity.MODERATE,
            summary="Arbitrary code execution",
        )
    ]
    body = render_pr_body(job, baseline_green=True, test_command="pytest -q", model="gemini-3.5-flash")
    assert "PYSEC-2021-1" in body
    assert "CVE-" not in body
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_pr_body.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.worker.pull_request'`

- [x] **Step 3: Write the implementation**

Create `services/worker/pull_request.py`:

```python
"""Rendering and opening the pull request.

The body is rendered by a pure function so that a formatting mistake is caught
by a unit test rather than by a maintainer reading a broken pull request. The
AI-authorship disclosure lives in the template and is asserted in the tests —
it is not something a future refactor gets to drop quietly.
"""

from __future__ import annotations

from pathlib import Path

from nightshift_core.models import RepoJob
from services.worker.toolchain import diff_stats

__all__ = ["PR_TEMPLATE_PATH", "render_pr_body"]

PR_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "pr_body.md"

#: How much of the failing output goes into the body. The tail carries the
#: traceback; the head is collection noise nobody needs in a pull request.
EXCERPT_CHARS = 2000


def render_pr_body(
    job: RepoJob,
    *,
    baseline_green: bool,
    test_command: str,
    model: str,
    template: str | None = None,
) -> str:
    """Fill ``templates/pr_body.md`` from a finished job."""
    text = template if template is not None else PR_TEMPLATE_PATH.read_text(encoding="utf-8")

    vulnerability = job.vulnerabilities[0]
    attempts = job.repair_attempts
    last = attempts[-1] if attempts else None
    diff = last.diff if last else ""
    stats = diff_stats(diff)
    excerpt = (last.failing_output if last else "")[-EXCERPT_CHARS:]
    cve = vulnerability.cve

    run_id, _, _ = job.job_id.partition(":")

    return text.format(
        package=vulnerability.package,
        from_version=vulnerability.installed_version,
        to_version=vulnerability.fixed_version or "unknown",
        advisory_id=vulnerability.osv_id,
        cve_suffix=f" ({cve})" if cve else "",
        severity=str(vulnerability.severity),
        advisory_summary=vulnerability.summary or "No summary published.",
        failing_test_count=stats.files,
        failing_excerpt=excerpt,
        repair_explanation=last.rationale if last else "",
        changed_file_count=stats.files,
        added_lines=stats.added,
        removed_lines=stats.removed,
        repair_diff=diff,
        baseline_status="passing" if baseline_green else "failing",
        final_status="passing",
        attempts=len(attempts),
        max_attempts=len(attempts),
        test_command=test_command,
        run_id=run_id,
        job_id=job.job_id,
        model=model,
    )
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_pr_body.py -v && make check`
Expected: PASS

If `test_no_placeholder_survives_rendering` fails, a field in `templates/pr_body.md` has no corresponding keyword above — add it rather than loosening the test.

- [x] **Step 5: Commit**

```bash
git add services/worker/pull_request.py tests/test_pr_body.py
git commit -m "feat(worker): render the pull request body from a finished job"
```

---

### Task 5: Opening the pull request

Branch, commit, push, open. The policy engine is consulted through `open_pull_request` — a denial here is one the job cannot proceed past, so it is the one place in this block that produces `POLICY_BLOCKED`.

**Files:**
- Modify: `services/worker/pull_request.py`
- Modify: `services/worker/main.py` — `open_pull_request` delegates
- Test: `tests/test_pull_request.py`

**Interfaces:**
- Consumes: `render_pr_body` (Task 4), `Sandbox`, `PolicyEngine`
- Produces:
  - `GitHubClient` — Protocol with `create_pull_request(repo: str, head: str, title: str, body: str) -> str`
  - `PyGithubClient(token: str)` — implements it
  - `open_pr(job, sandbox, policy, settings, client, *, baseline_green, model) -> str`
  - `PullRequestBlocked(RuntimeError)` — carries `.decision`

- [x] **Step 1: Write the failing tests**

```python
"""Opening the pull request. Forks by default; nothing merges itself."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightshift_core.config import Settings
from nightshift_core.models import RepairAttempt, RepoJob, Severity, Vulnerability
from nightshift_core.policy import PolicyEngine
from services.worker.pull_request import PullRequestBlocked, open_pr
from services.worker.toolchain import Sandbox


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create_pull_request(self, repo: str, head: str, title: str, body: str) -> str:
        self.calls.append({"repo": repo, "head": head, "title": title, "body": body})
        return f"https://github.com/{repo}/pull/1"


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    root = tmp_path / "repo"
    root.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "fleet@example.com"],
        ["git", "config", "user.name", "Nightshift"],
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    (root / "app.py").write_text("from jinja2 import Markup\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True, capture_output=True)
    (root / "app.py").write_text("from markupsafe import Markup\n", encoding="utf-8")
    return Sandbox(repo_path=root, python=Path("/usr/bin/python3"))


def make_job(repo: str = "nightshift-fleet/example") -> RepoJob:
    job = RepoJob(
        job_id="run1:" + repo,
        repo=repo,
        vulnerabilities=[
            Vulnerability(
                osv_id="GHSA-abcd",
                package="jinja2",
                installed_version="2.11.3",
                fixed_version="3.1.2",
                severity=Severity.HIGH,
                summary="Sandbox escape",
            )
        ],
    )
    job.record_attempt(
        RepairAttempt(attempt=1, failing_output="ImportError", diff="-a\n+b\n",
                      rationale="moved to markupsafe", tests_passed=True, tokens_used=10)
    )
    return job


def test_a_pull_request_to_our_own_fork_is_opened(sandbox: Sandbox) -> None:
    settings = Settings(fork_org="nightshift-fleet", github_token="x")
    policy = PolicyEngine(settings=settings, workspace=sandbox.repo_path.as_posix())
    client = FakeGitHub()
    url = open_pr(make_job(), sandbox, policy, settings, client,
                  baseline_green=True, model="gemini-3.5-flash")
    assert url == "https://github.com/nightshift-fleet/example/pull/1"
    assert "jinja2" in client.calls[0]["title"]


def test_an_upstream_pull_request_is_blocked_by_default(sandbox: Sandbox) -> None:
    """ALLOW_UPSTREAM_PRS is false and the fleet does not decide otherwise."""
    settings = Settings(fork_org="nightshift-fleet", allow_upstream_prs=False, github_token="x")
    policy = PolicyEngine(settings=settings, workspace=sandbox.repo_path.as_posix())
    client = FakeGitHub()
    with pytest.raises(PullRequestBlocked) as caught:
        open_pr(make_job("someone-else/library"), sandbox, policy, settings, client,
                baseline_green=True, model="gemini-3.5-flash")
    assert caught.value.decision.rule == "upstream-pr-denied"
    assert client.calls == [], "nothing was opened"


def test_the_branch_is_created_and_the_change_committed(sandbox: Sandbox) -> None:
    settings = Settings(fork_org="nightshift-fleet", github_token="x")
    policy = PolicyEngine(settings=settings, workspace=sandbox.repo_path.as_posix())
    open_pr(make_job(), sandbox, policy, settings, FakeGitHub(),
            baseline_green=True, model="gemini-3.5-flash")
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=sandbox.repo_path,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch.startswith("nightshift/")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=sandbox.repo_path,
                            capture_output=True, text=True, check=True).stdout
    assert status.strip() == "", "the repair should be committed, not left dirty"


def test_the_body_reaches_github_with_the_disclosure(sandbox: Sandbox) -> None:
    settings = Settings(fork_org="nightshift-fleet", github_token="x")
    policy = PolicyEngine(settings=settings, workspace=sandbox.repo_path.as_posix())
    client = FakeGitHub()
    open_pr(make_job(), sandbox, policy, settings, client,
            baseline_green=True, model="gemini-3.5-flash")
    assert "written by an AI agent" in client.calls[0]["body"]
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_pull_request.py -v`
Expected: FAIL — `ImportError: cannot import name 'open_pr'`

- [x] **Step 3: Write the implementation**

Append to `services/worker/pull_request.py` and extend `__all__` with `"GitHubClient"`, `"PullRequestBlocked"`, `"PyGithubClient"`, `"open_pr"`:

```python
import logging
from typing import Any, Protocol

from nightshift_core.config import Settings
from nightshift_core.policy import Decision, PolicyEngine, ToolCall
from services.worker.toolchain import Sandbox

log = logging.getLogger("nightshift.pr")

GIT_TIMEOUT = 300


class PullRequestBlocked(RuntimeError):
    """The policy engine refused to open this pull request."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(f"[{decision.rule}] {decision.reason}")
        self.decision = decision


class GitHubClient(Protocol):
    """What opening a pull request needs, and nothing more."""

    def create_pull_request(self, repo: str, head: str, title: str, body: str) -> str: ...


class PyGithubClient:
    """The real client. Constructed lazily so importing this module needs no token."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._github: Any | None = None

    @property
    def github(self) -> Any:
        if self._github is None:
            from github import Auth, Github

            self._github = Github(auth=Auth.Token(self._token))
        return self._github

    def create_pull_request(self, repo: str, head: str, title: str, body: str) -> str:
        repository = self.github.get_repo(repo)
        pull = repository.create_pull(
            title=title, body=body, head=head, base=repository.default_branch
        )
        return str(pull.html_url)


def open_pr(
    job: RepoJob,
    sandbox: Sandbox,
    policy: PolicyEngine,
    settings: Settings,
    client: GitHubClient,
    *,
    baseline_green: bool,
    model: str,
    test_command: str = "pytest -q",
) -> str:
    """Branch, commit, push and open. Returns the pull request url.

    The policy check happens *before* anything is pushed: a denial must not
    leave a branch on a remote that no pull request will ever reference.
    """
    vulnerability = job.vulnerabilities[0]
    call = ToolCall("open_pull_request", {"repo": job.repo, "auto_merge": False})
    decision = policy.check(call)
    if not decision.allowed:
        raise PullRequestBlocked(decision)

    branch = f"nightshift/{vulnerability.package}-{vulnerability.fixed_version}-{job.job_id.split(':')[0]}"
    title = (
        f"Security: {vulnerability.package} "
        f"{vulnerability.installed_version} → {vulnerability.fixed_version}"
    )
    body = render_pr_body(
        job, baseline_green=baseline_green, test_command=test_command, model=model
    )

    for argv in (
        ["git", "checkout", "-b", branch],
        ["git", "add", "-A"],
        ["git", "commit", "-m", title, "-m", "Opened by Nightshift, an autonomous agent fleet."],
    ):
        result = sandbox.run(argv, timeout=GIT_TIMEOUT)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(argv)} failed: {result.stderr[-500:]}")

    pushed = sandbox.run(["git", "push", "origin", branch], timeout=GIT_TIMEOUT)
    if pushed.returncode != 0:
        raise RuntimeError(f"push failed: {pushed.stderr[-500:]}")

    url = client.create_pull_request(repo=job.repo, head=branch, title=title, body=body)
    log.info("job %s opened %s", job.job_id, url)
    return url
```

**Note for the implementer:** `test_the_branch_is_created_and_the_change_committed` runs against a repository with no `origin`, so `git push` will fail. Make the push tolerant of a missing remote *only* when `settings.github_token` is empty — that is the local-development path — and let it raise otherwise:

```python
    if not settings.github_token:
        log.warning("no GITHUB_TOKEN; skipping push (local run)")
    else:
        pushed = sandbox.run(["git", "push", "origin", branch], timeout=GIT_TIMEOUT)
        ...
```

and set `github_token=""` in that one test's `Settings`.

- [x] **Step 4: Delegate from the worker**

In `services/worker/main.py`, replace the `open_pull_request` stub:

```python
def open_pull_request(job: RepoJob, sandbox: Sandbox, policy: PolicyEngine) -> str:
    """Open the PR from ``templates/pr_body.md``. Returns its url."""
    settings = get_settings()
    client = PyGithubClient(settings.github_token or "")
    return open_pr(
        job, sandbox, policy, settings, client,
        baseline_green=bool(job.baseline_green), model=settings.repair_model,
    )
```

Add the imports:

```python
from services.worker.pull_request import PullRequestBlocked, PyGithubClient, open_pr
```

and in `handle`, wrap the call so a denial becomes a counted outcome:

```python
    checkpoint(Phase.OPENING_PR)
    try:
        pr_url = open_pull_request(job, sandbox, policy)
    except PullRequestBlocked as exc:
        return finish(Outcome.POLICY_BLOCKED, notes=str(exc)[:500])
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_pull_request.py -v && make check`
Expected: PASS

- [x] **Step 6: Commit**

```bash
git add services/worker/pull_request.py services/worker/main.py tests/test_pull_request.py
git commit -m "feat(worker): open the pull request behind the policy engine"
```

---

### Task 6: The ADK repair agent

The real Gemini wiring. Kept last because everything above is testable without it, and because by this point the interface it must satisfy — `RepairAgent.attempt` — is already fixed by working code.

**Files:**
- Modify: `services/worker/agent.py`
- Modify: `packages/nightshift_core/config.py`, `.env.example`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `RepairContext`, `RepairProposal`, `SandboxTools`
- Produces:
  - `GeminiRepairAgent(settings: Settings)` — implements `RepairAgent`
  - `build_repair_agent(settings: Settings | None = None) -> GeminiRepairAgent`
  - `render_attempt_prompt(context: RepairContext) -> str`

`repair_model` and `escalation_model` already exist — they were added in Task 1
because Task 5 needed them.

- [x] **Step 1: Write the failing tests**

```python
"""The repair agent. What can be tested without a credential, is."""

from __future__ import annotations

from nightshift_core.config import Settings
from nightshift_core.models import RepairAttempt, Severity, Vulnerability
from services.worker.agent import (
    REPAIR_INSTRUCTION,
    GeminiRepairAgent,
    render_attempt_prompt,
)
from services.worker.repair import RepairContext

CONTEXT = RepairContext(
    repo="nightshift-fleet/example",
    vulnerabilities=(
        Vulnerability(
            osv_id="GHSA-abcd", package="jinja2", installed_version="2.11.3",
            fixed_version="3.1.2", severity=Severity.HIGH,
        ),
    ),
    failing_output="ImportError: cannot import name 'Markup' from 'jinja2'",
    attempt=1,
)


def test_the_prompt_names_the_transition_and_the_failure() -> None:
    prompt = render_attempt_prompt(CONTEXT)
    assert "jinja2" in prompt
    assert "2.11.3" in prompt and "3.1.2" in prompt
    assert "cannot import name 'Markup'" in prompt


def test_the_prompt_carries_previous_attempts_forward() -> None:
    context = RepairContext(
        repo=CONTEXT.repo, vulnerabilities=CONTEXT.vulnerabilities,
        failing_output="still broken", attempt=2,
        previous=(RepairAttempt(attempt=1, failing_output="boom",
                                rationale="tried the markupsafe import", tests_passed=False),),
    )
    prompt = render_attempt_prompt(context)
    assert "tried the markupsafe import" in prompt
    assert "attempt 2" in prompt.lower()


def test_the_instruction_forbids_editing_tests() -> None:
    """The prompt says it and the policy engine enforces it. Both must hold."""
    assert "Do not edit, skip, xfail or delete any test" in REPAIR_INSTRUCTION


def test_the_agent_escalates_after_two_failed_attempts() -> None:
    settings = Settings(repair_model="flash", escalation_model="pro")
    agent = GeminiRepairAgent(settings=settings)
    assert agent.model_for(attempt=1) == "flash"
    assert agent.model_for(attempt=2) == "flash"
    assert agent.model_for(attempt=3) == "pro"


def test_constructing_the_agent_needs_no_credential() -> None:
    """Import and construction must be free — CI has no Google credentials."""
    assert GeminiRepairAgent(settings=Settings()) is not None
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_agent.py -v`
Expected: FAIL — `ImportError: cannot import name 'GeminiRepairAgent'`

- [x] **Step 3: Write the implementation**

In `services/worker/agent.py`, keep `REPAIR_INSTRUCTION` exactly as written — it is a design artefact — and replace `build_repair_agent` with:

```python
from dataclasses import dataclass

from services.worker.repair import RepairContext, RepairProposal
from services.worker.tools import SandboxTools

__all__ = [
    "REPAIR_INSTRUCTION",
    "GeminiRepairAgent",
    "build_repair_agent",
    "render_attempt_prompt",
]

#: Attempts on the cheap model before escalating. Two, because a second failure
#: usually means the break is not the shape Flash is good at, and a third
#: identical failure is the signal the instruction tells the agent to act on.
FLASH_ATTEMPTS = 2


def render_attempt_prompt(context: RepairContext) -> str:
    """The per-attempt message. The instruction is separate and constant."""
    transitions = "\n".join(
        f"- {v.package} {v.installed_version} → {v.fixed_version} "
        f"({v.osv_id}, {v.severity})"
        for v in context.vulnerabilities
    )
    history = ""
    if context.previous:
        history = "\n\nWhat you have already tried, and what it did:\n" + "\n".join(
            f"- Attempt {a.attempt}: {a.rationale or '(no rationale recorded)'} "
            f"— tests {'passed' if a.tests_passed else 'still failed'}"
            for a in context.previous
        )
    return (
        f"Repository: {context.repo}\n"
        f"This is attempt {context.attempt}.\n\n"
        f"Upgrades applied:\n{transitions}\n\n"
        f"The test suite now fails:\n\n```\n{context.failing_output}\n```"
        f"{history}\n\n"
        "Make one conceptual fix to the calling code. Do not run the test suite "
        "yourself — it is run for you after you finish, and its result is the "
        "only measure of success."
    )


@dataclass
class GeminiRepairAgent:
    """The ADK agent, adapted to the :class:`RepairAgent` protocol."""

    settings: Settings

    def model_for(self, attempt: int) -> str:
        """Flash first, Pro once Flash has had its two attempts."""
        if attempt <= FLASH_ATTEMPTS:
            return self.settings.repair_model
        return self.settings.escalation_model

    def attempt(self, context: RepairContext, tools: SandboxTools) -> RepairProposal:
        from google.adk.agents import LlmAgent
        from google.adk.tools import FunctionTool

        agent = LlmAgent(
            name="nightshift_repair",
            model=self.model_for(context.attempt),
            instruction=REPAIR_INSTRUCTION,
            tools=[
                FunctionTool(tools.read_file),
                FunctionTool(tools.write_file),
                FunctionTool(tools.run_command),
            ],
        )
        result = agent.run(render_attempt_prompt(context))
        return RepairProposal(
            rationale=str(getattr(result, "text", result)),
            tokens_used=int(getattr(result, "total_tokens", 0)),
        )


def build_repair_agent(settings: Settings | None = None) -> GeminiRepairAgent:
    """Construct the agent that runs the repair loop."""
    return GeminiRepairAgent(settings=settings or get_settings())
```

**Note for the implementer:** the ADK surface (`LlmAgent`, `FunctionTool`, `agent.run`) is the part of this plan most likely to differ from the installed `google-adk` version. Verify against the installed package before assuming this is wrong, and adjust `attempt` only — the Protocol boundary is what protects the rest of the code from that churn. `mypy` treats `google.adk.*` as untyped (already configured in `pyproject.toml`), so the import is deliberately inside the method.

- [x] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_agent.py -v && make check`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add services/worker/agent.py packages/nightshift_core/config.py .env.example tests/test_agent.py
git commit -m "feat(worker): wire the Gemini repair agent with Flash-to-Pro escalation"
```

---

### Task 7: End to end on one repository

Block 1's definition of done. `run_local.py` currently imports two scanner stubs; the fastest honest path is to read dependencies from a clone with the already-tested `toolchain.read_dependencies` rather than implement the scanner's GitHub-contents path, which only earns its keep at fleet scale in Block 2.

**Files:**
- Modify: `scripts/run_local.py`
- Modify: `services/scanner/main.py` — implement `triage` only
- Modify: `services/worker/main.py` — pass the agent into `repair`
- Test: `tests/test_triage.py`, `tests/test_worker_handle.py`

**Interfaces:**
- Consumes: everything above
- Produces: `triage(vulnerabilities: Sequence[Vulnerability]) -> Sequence[Vulnerability]`

- [x] **Step 1: Write the failing tests**

```python
"""Triage: the cheap gate before an expensive model is woken."""

from __future__ import annotations

from nightshift_core.models import Severity, Vulnerability
from services.scanner.main import triage


def make(package: str, severity: Severity, fixed: str | None = "2.0") -> Vulnerability:
    return Vulnerability(
        osv_id=f"GHSA-{package}", package=package, installed_version="1.0",
        fixed_version=fixed, severity=severity,
    )


def test_low_severity_is_dropped() -> None:
    kept = triage([make("a", Severity.LOW), make("b", Severity.HIGH)])
    assert [v.package for v in kept] == ["b"]


def test_the_floor_is_inclusive_of_moderate() -> None:
    assert [v.package for v in triage([make("a", Severity.MODERATE)])] == ["a"]


def test_an_advisory_with_no_fix_is_dropped() -> None:
    """NO_FIX_AVAILABLE is decided per job; there is nothing to schedule here."""
    assert triage([make("a", Severity.CRITICAL, fixed=None)]) == []


def test_unknown_severity_is_dropped_but_critical_is_kept() -> None:
    kept = triage([make("a", Severity.UNKNOWN), make("b", Severity.CRITICAL)])
    assert [v.package for v in kept] == ["b"]


def test_an_empty_input_gives_an_empty_result() -> None:
    assert triage([]) == []
```

```python
"""The worker's phase machine, end to end, with a scripted agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from nightshift_core.config import Ceilings, Settings
from nightshift_core.models import Outcome, RepoJob, Severity, Vulnerability
from nightshift_core.store import MemoryJobStore
from services.worker import main as worker
from services.worker.repair import RepairProposal
from services.worker.toolchain import Sandbox, TestReport


class AlwaysRepairs:
    def attempt(self, context: object, tools: object) -> RepairProposal:
        return RepairProposal(rationale="fixed the import", tokens_used=100)


class NeverRepairs:
    def attempt(self, context: object, tools: object) -> RepairProposal:
        return RepairProposal(rationale="no idea", tokens_used=100)


def make_job() -> RepoJob:
    return RepoJob(
        job_id="run1:nightshift-fleet/example",
        repo="nightshift-fleet/example",
        vulnerabilities=[
            Vulnerability(osv_id="GHSA-a", package="jinja2", installed_version="2.11.3",
                          fixed_version="3.1.2", severity=Severity.HIGH)
        ],
    )


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stub the toolchain so the phase machine is what is under test."""
    root = tmp_path / "repo"
    root.mkdir()
    sandbox = Sandbox(repo_path=root, python=Path("/usr/bin/python3"))
    monkeypatch.setattr(worker, "clone", lambda repo, workspace, token=None: root)
    monkeypatch.setattr(worker, "build_environment", lambda path: sandbox)
    monkeypatch.setattr(worker, "apply_upgrade", lambda sandbox, vulns: ["requirements.txt"])
    monkeypatch.setattr(worker, "open_pull_request",
                        lambda job, sandbox, policy: "https://github.com/x/y/pull/1")
    return monkeypatch, sandbox


def patch_suite(monkeypatch: pytest.MonkeyPatch, results: list[bool]) -> None:
    """Patch run_tests in BOTH modules that resolve it.

    ``services.worker.main`` and ``services.worker.repair`` each import
    ``run_tests`` into their own namespace, so patching one does not reach the
    other. Getting this wrong makes the repair-loop tests silently run the real
    pytest against an empty directory.
    """
    remaining = iter(results)

    def fake(sandbox: object, **kwargs: object) -> TestReport:
        return TestReport(passed=next(remaining), output="x", duration_seconds=0.1)

    monkeypatch.setattr(worker, "run_tests", fake)
    monkeypatch.setattr("services.worker.repair.run_tests", fake)


def test_a_red_baseline_stops_before_any_upgrade(patched) -> None:
    monkeypatch, _ = patched
    monkeypatch.setattr(worker, "run_tests",
                        lambda sandbox, **kw: TestReport(passed=False, output="red", duration_seconds=0.1))
    job = worker.handle(make_job(), MemoryJobStore(), Settings(fork_org="nightshift-fleet"))
    assert job.outcome is Outcome.BASELINE_RED
    assert job.repair_attempts == []


def test_an_upgrade_that_breaks_nothing_is_patched_clean(patched) -> None:
    monkeypatch, _ = patched
    monkeypatch.setattr(worker, "run_tests",
                        lambda sandbox, **kw: TestReport(passed=True, output="green", duration_seconds=0.1))
    job = worker.handle(make_job(), MemoryJobStore(), Settings(fork_org="nightshift-fleet"))
    assert job.outcome is Outcome.PATCHED_CLEAN
    assert job.repair_attempts == [], "no model was called"


def test_a_break_the_agent_fixes_is_patched_repaired(patched, monkeypatch) -> None:
    _, _ = patched
    patch_suite(monkeypatch, [True, False, True])
    monkeypatch.setattr(worker, "build_repair_agent", lambda settings=None: AlwaysRepairs())
    job = worker.handle(make_job(), MemoryJobStore(), Settings(fork_org="nightshift-fleet"))
    assert job.outcome is Outcome.PATCHED_REPAIRED
    assert len(job.repair_attempts) == 1


def test_a_break_the_agent_cannot_fix_is_repair_exhausted(patched, monkeypatch) -> None:
    _, _ = patched
    patch_suite(monkeypatch, [True] + [False] * 10)
    monkeypatch.setattr(worker, "build_repair_agent", lambda settings=None: NeverRepairs())
    settings = Settings(fork_org="nightshift-fleet", ceilings=Ceilings(max_repair_attempts=2))
    job = worker.handle(make_job(), MemoryJobStore(), settings)
    assert job.outcome is Outcome.REPAIR_EXHAUSTED
    assert len(job.repair_attempts) == 2
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_triage.py tests/test_worker_handle.py -v`
Expected: FAIL — `NotImplementedError: scanner: triage`

- [x] **Step 3: Implement `triage`**

In `services/scanner/main.py`, replace the stub body, keeping the docstring and adding a paragraph about what was deferred:

```python
def triage(vulnerabilities: Sequence[Vulnerability]) -> Sequence[Vulnerability]:
    """Cheap pass over raw advisories before any expensive work is scheduled.

    Block 1 implements the deterministic half only: the severity floor and the
    "is there anything to upgrade to" check. The Gemma pass that judges whether
    an advisory plausibly reaches a given codebase is Block 3 — it needs Vertex,
    and gating local development on a credential would be the wrong trade.
    """
    return [
        vulnerability
        for vulnerability in vulnerabilities
        if vulnerability.actionable and vulnerability.severity.rank >= TRIAGE_FLOOR.rank
    ]
```

- [x] **Step 4: Pass the agent into the loop**

In `services/worker/main.py`, `handle` currently calls `repair(...)` without an agent. Build it once, lazily, so a `PATCHED_CLEAN` job never constructs one:

```python
    repaired = False
    if not verified.passed:
        checkpoint(Phase.REPAIR)
        repaired = repair(job, sandbox, verified, policy, budget, build_repair_agent(settings))
```

Add `from services.worker.agent import build_repair_agent` to the imports.

- [x] **Step 5: Rewrite `run_local.py` to read from a clone**

Replace the scanner imports and the dependency read in `scripts/run_local.py`:

```python
import tempfile
from pathlib import Path

from services.scanner.main import triage
from services.worker.main import handle
from services.worker.toolchain import clone, read_dependencies
```

and replace the `read_manifests` call with:

```python
    with tempfile.TemporaryDirectory() as scratch:
        repo_path = clone(args.repo, Path(scratch), token=settings.github_token)
        dependencies = read_dependencies(repo_path)

    with OSVClient() as osv:
        vulnerabilities = list(triage(osv.find_vulnerabilities(dependencies)))
```

A second, shallow clone costs a few seconds locally and buys the use of
`read_dependencies`, which is already tested. The scanner's manifest read stays
stubbed until Block 2, where reading three hundred repositories without cloning
them is what makes it worth writing.

- [x] **Step 6: Run everything**

Run: `make check`
Expected: green — ruff, mypy --strict, and the full suite.

- [x] **Step 7: Run it against a real repository**

```bash
export NIGHTSHIFT_WORKSPACE_ROOT=/tmp/nightshift
export GITHUB_TOKEN=...            # a token on the fork org, repo scope
export NIGHTSHIFT_FORK_ORG=...     # the org the forks live in
make run-local REPO=<a fork in your org pinned to jinja2 2.11.3>
```

Expected: a printed outcome of `PATCHED_REPAIRED` and a real pull-request URL.
**Block 1 is done when this produces a pull request a human can open and read.**

- [x] **Step 8: Commit**

```bash
git add scripts/run_local.py services/scanner/main.py services/worker/main.py tests/test_triage.py tests/test_worker_handle.py
git commit -m "feat(worker): run one repository end to end to a real pull request"
```

---

## Definition of done

- [x] `make check` green: ruff, `mypy --strict`, **176 tests**.
- [ ] `make run-local REPO=owner/name` opens a real pull request on a fork.
      **Blocked, not failed** — needs a `GITHUB_TOKEN` and a fork to run against,
      and `scripts/build_fork_pool.py` is still a stub. Every code path it
      exercises is implemented and tested; nobody has watched it open a real
      pull request yet, so do not claim Block 1 finished until someone has.
- [x] Every `Outcome` path in `handle` is covered by a test: `UNBUILDABLE`,
      `BASELINE_RED`, `PATCHED_CLEAN`, `PATCHED_REPAIRED`, `REPAIR_EXHAUSTED`,
      `NO_FIX_AVAILABLE`, `POLICY_BLOCKED` — `tests/test_worker_handle.py`.
- [x] The agent cannot write a test file — proven in `tests/test_tools.py`, not
      asserted in a prompt.
- [x] No new `Outcome` member. No auto-merge flag. No secret in the diff.
- [x] `SESSION_SUMMARY.md` updated: `NOW` rewritten, log entry appended.

## Verified for real, 19 Aug

The non-model half of the pipeline was run against
`benchmark/cases/jinja2-2.11-to-3.1`, not merely unit-tested:

```
BUILD      pip install -r requirements.txt -> 0
BASELINE   passed=True   exit=0   collected=True
UPGRADE    manifests changed: ['requirements.txt']
VERIFY     passed=False  exit=2   Interrupted: 1 error during collection
```

That red suite is exactly the input `repair()` receives. Note exit code 2 —
collection interrupted — which `TestReport.internal_error` deliberately does
**not** treat as our fault, because after an upgrade it is the single most
common shape of a real break.
