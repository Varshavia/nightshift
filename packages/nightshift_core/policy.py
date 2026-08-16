"""The policy engine. Every tool call passes through here before execution.

This module is deliberately the most heavily tested thing in the repository. A
bug in the agent mislabels a result; a bug here lets an autonomous process do
real work in the world that nobody asked for. The engine is therefore pure —
no I/O, no clock beyond an injected budget, no network — so that it can be
exercised exhaustively in milliseconds.

Four things it exists to guarantee:

1. **The ceilings hold.** Attempts, wall-clock and tokens are checked before
   every call, not after the loop.
2. **The tests are not the agent's to edit.** An agent asked to make a red suite
   go green will, given the chance, delete the failing test. That is the single
   most likely way this project produces a convincing lie, so it is denied at
   the policy layer rather than discouraged in a prompt.
3. **The blast radius is the workspace.** Nothing is read or written outside the
   clone, whatever the path looks like.
4. **Nothing reaches upstream, and nothing merges itself.** Forks by default;
   ``ALLOW_UPSTREAM_PRS=false``; no merge verb is reachable at all.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from nightshift_core.config import Ceilings, Settings

__all__ = [
    "Budget",
    "Decision",
    "Effect",
    "PolicyEngine",
    "PolicyViolation",
    "ToolCall",
]


class Effect(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class Decision:
    """The engine's answer, always with the rule that produced it.

    The rule name is not decoration: it is what the dashboard groups denials by
    and what a judge reads in the audit trail to see that a refusal was a
    designed behaviour rather than an accident.
    """

    effect: Effect
    rule: str
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise PolicyViolation(self)


class PolicyViolation(RuntimeError):
    """Raised when a caller executes a denied call anyway."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(f"[{decision.rule}] {decision.reason}")
        self.decision = decision


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A request to do something, before it is done."""

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)

    def arg(self, key: str, default: Any = None) -> Any:
        return self.args.get(key, default)


@dataclass(slots=True)
class Budget:
    """What the job has spent so far. The worker advances this; policy reads it.

    ``elapsed_seconds`` is passed in rather than measured so that the engine
    stays pure and the tests stay deterministic.
    """

    attempts: int = 0
    tokens: int = 0
    elapsed_seconds: float = 0.0

    def spend(self, *, tokens: int = 0, seconds: float = 0.0, attempts: int = 0) -> None:
        self.tokens += tokens
        self.elapsed_seconds += seconds
        self.attempts += attempts


# --------------------------------------------------------------------------- #
# Rule data
# --------------------------------------------------------------------------- #

#: Commands the agent may run inside the sandbox. Anything not named is denied;
#: the list grows by pull request, never by a runtime flag.
ALLOWED_EXECUTABLES: frozenset[str] = frozenset(
    {
        "python",
        "python3",
        "pytest",
        "pip",
        "pip3",
        "uv",
        "tox",
        "nox",
        "make",
        "poetry",
        "ruff",
        "mypy",
        "git",
        "ls",
        "cat",
        "grep",
        "rg",
        "find",
        "head",
        "tail",
    }
)

#: Shell constructs that would let an allowed executable smuggle in a denied
#: one. Commands are parsed with ``shlex`` and rejected if any of these survive.
SHELL_METACHARACTERS: tuple[str, ...] = ("|", "&&", ";", "`", "$(", ">", ">>", "<", "\n")

#: ``git`` subcommands the agent may use. ``merge`` is absent on purpose: nothing
#: merges itself, so the verb is not reachable, not merely discouraged.
ALLOWED_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {"status", "diff", "log", "add", "commit", "checkout", "switch", "restore", "rev-parse",
     "branch", "stash", "show", "fetch", "push"}
)

DENIED_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {"merge", "rebase", "reset", "clean", "filter-branch", "gc", "config", "remote"}
)

#: Hosts the sandbox may reach. Package indexes to install, OSV to re-check an
#: advisory, GitHub to open the pull request. Nothing else.
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "pypi.org",
        "files.pythonhosted.org",
        "api.osv.dev",
        "github.com",
        "api.github.com",
        "codeload.github.com",
    }
)

#: Paths inside the clone that the agent may not write to, whatever it believes
#: it needs. Tests are the baseline's evidence; CI config is the baseline's
#: definition. An agent that can edit either can fake a green run.
PROTECTED_WRITE_PREFIXES: tuple[str, ...] = (".git/", ".github/", ".circleci/")
PROTECTED_WRITE_FILES: frozenset[str] = frozenset(
    {".travis.yml", "azure-pipelines.yml", "Jenkinsfile", ".pre-commit-config.yaml"}
)


def _is_test_path(path: str) -> bool:
    """True for anything that is part of the test suite.

    Intentionally broad. A false positive costs the agent one avenue of repair;
    a false negative costs the project its credibility.
    """
    pure = PurePosixPath(path.replace("\\", "/"))
    if any(part in {"test", "tests", "testing"} for part in pure.parts):
        return True
    name = pure.name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name in {"conftest.py", "pytest.ini"}
    )


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


class PolicyEngine:
    """Evaluates a :class:`ToolCall` against the fleet's non-negotiables."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        workspace: str = "/workspace/repo",
        ceilings: Ceilings | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.ceilings = ceilings or self.settings.ceilings
        self.workspace = PurePosixPath(workspace)
        self._audit: list[tuple[ToolCall, Decision]] = []

    # -- public API --------------------------------------------------------- #

    @property
    def audit_log(self) -> Sequence[tuple[ToolCall, Decision]]:
        """Every decision, in order. This is the trail a human reviews."""
        return tuple(self._audit)

    def check(self, call: ToolCall, budget: Budget | None = None) -> Decision:
        decision = self._evaluate(call, budget or Budget())
        self._audit.append((call, decision))
        return decision

    def enforce(self, call: ToolCall, budget: Budget | None = None) -> None:
        """Check and raise. For call sites that cannot meaningfully continue."""
        self.check(call, budget).raise_if_denied()

    # -- evaluation --------------------------------------------------------- #

    def _evaluate(self, call: ToolCall, budget: Budget) -> Decision:
        ceiling = self._check_ceilings(budget)
        if ceiling is not None:
            return ceiling

        handler = {
            "read_file": self._check_read,
            "write_file": self._check_write,
            "apply_patch": self._check_write,
            "delete_file": self._check_delete,
            "run_command": self._check_command,
            "http_request": self._check_http,
            "open_pull_request": self._check_pull_request,
        }.get(call.name)

        if handler is None:
            return Decision(
                Effect.DENY,
                "unknown-tool",
                f"{call.name!r} is not a tool the policy engine knows; "
                "new tools are added with their rule, not without one",
            )
        return handler(call)

    def _check_ceilings(self, budget: Budget) -> Decision | None:
        """Every loop has a ceiling. Checked before the call, not after."""
        if budget.attempts > self.ceilings.max_repair_attempts:
            return Decision(
                Effect.DENY,
                "ceiling-attempts",
                f"repair attempts {budget.attempts} exceeds "
                f"{self.ceilings.max_repair_attempts}",
            )
        if budget.elapsed_seconds > self.ceilings.max_job_seconds:
            return Decision(
                Effect.DENY,
                "ceiling-wallclock",
                f"elapsed {budget.elapsed_seconds:.0f}s exceeds "
                f"{self.ceilings.max_job_seconds}s",
            )
        if budget.tokens > self.ceilings.max_job_tokens:
            return Decision(
                Effect.DENY,
                "ceiling-tokens",
                f"tokens {budget.tokens} exceeds {self.ceilings.max_job_tokens}",
            )
        return None

    # -- filesystem --------------------------------------------------------- #

    def _resolve(self, raw: object) -> PurePosixPath | None:
        """Resolve a candidate path inside the workspace, or None if it escapes.

        Done lexically and without touching the filesystem: the engine must give
        the same answer in a unit test as it does in a container.
        """
        if not isinstance(raw, str) or not raw:
            return None
        candidate = PurePosixPath(raw.replace("\\", "/"))
        joined = candidate if candidate.is_absolute() else self.workspace / candidate

        parts: list[str] = []
        for part in joined.parts:
            if part == "..":
                if not parts or parts == [joined.anchor]:
                    return None
                parts.pop()
            elif part not in {".", ""}:
                parts.append(part)
        normalised = PurePosixPath(*parts)
        if not normalised.is_relative_to(self.workspace):
            return None
        return normalised

    def _check_read(self, call: ToolCall) -> Decision:
        path = self._resolve(call.arg("path"))
        if path is None:
            return Decision(
                Effect.DENY, "sandbox-escape", f"read outside workspace: {call.arg('path')!r}"
            )
        return Decision(Effect.ALLOW, "read-in-workspace")

    def _check_write(self, call: ToolCall) -> Decision:
        raw = call.arg("path")
        path = self._resolve(raw)
        if path is None:
            return Decision(
                Effect.DENY, "sandbox-escape", f"write outside workspace: {raw!r}"
            )
        relative = path.relative_to(self.workspace).as_posix()

        if _is_test_path(relative):
            return Decision(
                Effect.DENY,
                "tests-are-evidence",
                f"{relative} is part of the test suite; the agent repairs the code "
                "the upgrade broke, never the test that proves it broke",
            )
        if relative in PROTECTED_WRITE_FILES or relative.startswith(PROTECTED_WRITE_PREFIXES):
            return Decision(
                Effect.DENY,
                "protected-path",
                f"{relative} defines how the baseline is measured and is not the "
                "agent's to change",
            )
        return Decision(Effect.ALLOW, "write-in-workspace")

    def _check_delete(self, call: ToolCall) -> Decision:
        decision = self._check_write(call)
        if not decision.allowed:
            return decision
        return Decision(
            Effect.DENY,
            "no-deletion",
            "the repair loop rewrites files; it does not remove them",
        )

    # -- commands ----------------------------------------------------------- #

    def _check_command(self, call: ToolCall) -> Decision:
        raw = call.arg("command")
        if isinstance(raw, list):
            argv = [str(part) for part in raw]
            command_text = " ".join(argv)
        elif isinstance(raw, str):
            command_text = raw
            for meta in SHELL_METACHARACTERS:
                if meta in raw:
                    return Decision(
                        Effect.DENY,
                        "no-shell",
                        f"shell construct {meta!r} is not available; commands run "
                        "as argv, one executable at a time",
                    )
            try:
                argv = shlex.split(raw)
            except ValueError as exc:
                return Decision(Effect.DENY, "unparsable-command", str(exc))
        else:
            return Decision(Effect.DENY, "unparsable-command", f"bad command: {raw!r}")

        if not argv:
            return Decision(Effect.DENY, "unparsable-command", "empty command")

        executable = PurePosixPath(argv[0]).name
        if executable not in ALLOWED_EXECUTABLES:
            return Decision(
                Effect.DENY,
                "executable-not-allowed",
                f"{executable!r} is not on the allowlist",
            )
        if executable == "git":
            return self._check_git(argv, command_text)
        return Decision(Effect.ALLOW, "command-allowed")

    def _check_git(self, argv: list[str], command_text: str) -> Decision:
        subcommand = next((part for part in argv[1:] if not part.startswith("-")), "")
        if subcommand in DENIED_GIT_SUBCOMMANDS:
            reason = (
                "nothing merges itself"
                if subcommand in {"merge", "rebase"}
                else f"git {subcommand} is not available to the agent"
            )
            return Decision(Effect.DENY, f"git-{subcommand}-denied", reason)
        if subcommand not in ALLOWED_GIT_SUBCOMMANDS:
            return Decision(
                Effect.DENY, "git-subcommand-not-allowed", f"git {subcommand!r} is not allowed"
            )
        if subcommand == "push":
            if "--force" in argv or "-f" in argv or "--force-with-lease" in argv:
                return Decision(
                    Effect.DENY, "no-force-push", "history is the audit trail; it is not rewritten"
                )
            remote = argv[argv.index("push") + 1] if len(argv) > argv.index("push") + 1 else ""
            if remote.startswith("-"):
                remote = ""
            if remote and remote not in {"origin", "fork"}:
                return Decision(
                    Effect.DENY,
                    "push-remote-not-allowed",
                    f"the fleet pushes to its own fork, not to {remote!r}",
                )
        del command_text
        return Decision(Effect.ALLOW, "git-allowed")

    # -- network ------------------------------------------------------------ #

    def _check_http(self, call: ToolCall) -> Decision:
        url = call.arg("url")
        if not isinstance(url, str):
            return Decision(Effect.DENY, "bad-url", f"not a url: {url!r}")
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return Decision(Effect.DENY, "https-only", f"{parsed.scheme or 'no'} scheme refused")
        host = (parsed.hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            return Decision(Effect.DENY, "host-not-allowed", f"{host!r} is not on the allowlist")
        return Decision(Effect.ALLOW, "host-allowed")

    # -- pull requests ------------------------------------------------------ #

    def _check_pull_request(self, call: ToolCall) -> Decision:
        repo = call.arg("repo")
        if not isinstance(repo, str) or "/" not in repo:
            return Decision(Effect.DENY, "bad-repo", f"not a repository: {repo!r}")
        owner = repo.split("/", 1)[0]

        if call.arg("auto_merge"):
            return Decision(
                Effect.DENY,
                "no-auto-merge",
                "nothing merges itself — a human reviews every pull request",
            )
        if owner == self.settings.fork_org and self.settings.fork_org:
            return Decision(Effect.ALLOW, "pr-to-fork")
        if not self.settings.allow_upstream_prs:
            return Decision(
                Effect.DENY,
                "upstream-pr-denied",
                f"{repo} is upstream and ALLOW_UPSTREAM_PRS is false; the fleet "
                "operates on forks unless a human opts a repository in",
            )
        return Decision(Effect.ALLOW, "pr-to-upstream-opted-in")
