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
