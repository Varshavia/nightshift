"""The repair agent.

This is the product. Everything else in the repository exists to get a Gemini
agent in front of a failing test run with the right context and the right
constraints, and to record honestly what it did.

The instruction prompt below is written in full and is a design artefact. The
ADK wiring around it is real as of Block 1.

The model call itself cannot be unit-tested, so the seams either side of it are:
:func:`render_attempt_prompt` going in, and :func:`final_text` /
:func:`total_tokens` coming out. Both are pure and both are covered.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from nightshift_core.config import Settings, get_settings
from services.worker.repair import RepairContext, RepairProposal
from services.worker.tools import SandboxTools

__all__ = [
    "REPAIR_INSTRUCTION",
    "GeminiRepairAgent",
    "build_repair_agent",
    "final_text",
    "render_attempt_prompt",
    "total_tokens",
]

log = logging.getLogger("nightshift.agent")

#: Attempts on the cheap model before escalating. Two, because a second failure
#: usually means the break is not the shape Flash is good at, and a third
#: identical failure is the signal the instruction tells the agent to act on.
FLASH_ATTEMPTS = 2

APP_NAME = "nightshift"
AGENT_USER = "nightshift-fleet"


REPAIR_INSTRUCTION = """\
You are Nightshift, a repair agent. A security upgrade has already been applied
to this repository and it broke the code that calls the upgraded library. Your
job is to make the existing test suite pass again with the new version in place.

WHAT YOU ARE LOOKING AT

- The dependency has been bumped to a version that fixes a published advisory.
- The test suite passed on this commit before the bump. You have that baseline.
- It now fails. The failure is almost always an API that moved: a renamed
  keyword argument, a return type that became a model instead of a dict, a
  function that became a method, a default that flipped, an import path that
  moved one level down.

HOW TO WORK

1. Read the failure before you touch anything. The traceback names the call
   site. Go there first.
2. Find out what the new version actually expects. Prefer evidence in the
   installed package over recollection: read the library's own source in
   site-packages, its type stubs, its CHANGELOG. Your memory of this library's
   API is a hypothesis, not a fact.
3. Change the calling code, minimally. One conceptual fix per attempt. A large
   diff that happens to pass is worth less than a small one that is obviously
   right, because a human has to review it.
4. Re-run the tests. Read what changed. If the same test fails the same way,
   your model of the problem is wrong — go back to step 2 rather than trying a
   variation of the same fix.

WHAT YOU MUST NOT DO — these are enforced by the policy engine, and attempting
them wastes an attempt you do not get back:

- Do not edit, skip, xfail or delete any test. The suite is the evidence that
  the repair worked. Changing it destroys the only thing that makes your output
  trustworthy. If a test genuinely encodes the old API's behaviour, stop and say
  so: that is a real finding for a human, not something for you to smooth over.
- Do not touch CI configuration.
- Do not pin the dependency back down, or add a compatibility shim that keeps
  the old version working. The upgrade is the point.
- Do not widen the change beyond what the failure requires. You are not here to
  refactor, reformat, or improve unrelated code.

WHEN TO STOP

Stop and report failure when: the same failure survives two different fixes;
the breakage is in a transitive dependency rather than this codebase; the fix
would require changing a test. Reporting REPAIR_EXHAUSTED honestly is a correct
outcome and is counted as one. Producing a diff you are not confident in is not.

WHAT TO RETURN

- The diff you applied.
- One paragraph: what the upgrade changed in the library's API, and why this
  edit is the right response to it. Write it for the human reviewing the pull
  request at nine in the morning, who has not read the traceback.
"""


def render_attempt_prompt(context: RepairContext) -> str:
    """The per-attempt message. The instruction is separate and constant."""
    transitions = "\n".join(
        f"- {v.package} {v.installed_version} → {v.fixed_version} ({v.osv_id}, {v.severity})"
        for v in context.vulnerabilities
    )
    history = ""
    if context.previous:
        history = "\n\nWhat you have already tried, and what it did:\n" + "\n".join(
            f"- Attempt {a.attempt}: {a.rationale or '(no rationale recorded)'}"
            f" — tests {'passed' if a.tests_passed else 'still failed'}"
            for a in context.previous
        )
    # After the traceback, deliberately. The agent should form its own reading of
    # the failure before it is handed somebody else's conclusion; a recipe placed
    # first would anchor it on a fix that may not be this repository's problem.
    prior_art = f"\n\nWhat the fleet already knows:\n\n{context.recipe}" if context.recipe else ""
    return (
        f"Repository: {context.repo}\n"
        f"This is attempt {context.attempt}.\n\n"
        f"Upgrades applied:\n{transitions}\n\n"
        f"The test suite now fails:\n\n```\n{context.failing_output}\n```"
        f"{history}{prior_art}\n\n"
        "Make one conceptual fix to the calling code. Do not run the test suite "
        "yourself — it is run for you after you finish, and its result is the "
        "only measure of success."
    )


def final_text(events: Iterable[Any]) -> str:
    """The last thing the agent actually said.

    Most events in a run are tool calls and carry no text at all. The rationale
    we want is the final narrative turn, so the last non-empty one wins rather
    than the concatenation of everything — a reviewer wants the explanation, not
    the agent's working.
    """
    latest = ""
    for event in events:
        content = getattr(event, "content", None)
        if content is None:
            continue
        chunks = [
            part.text
            for part in getattr(content, "parts", None) or []
            if getattr(part, "text", None)
        ]
        if chunks:
            latest = "".join(chunks).strip()
    return latest


def total_tokens(events: Iterable[Any]) -> int:
    """Every token the run spent, summed across turns.

    Read from the events rather than estimated, because this number is charged
    against the job's ceiling and reported as cost per repository.
    """
    total = 0
    for event in events:
        usage = getattr(event, "usage_metadata", None)
        if usage is not None:
            total += int(getattr(usage, "total_token_count", 0) or 0)
    return total


@dataclass
class GeminiRepairAgent:
    """The ADK agent, adapted to the :class:`~services.worker.repair.RepairAgent` protocol.

    Tools given to it — ``read_file``, ``write_file``, ``run_command`` — come
    from :class:`~services.worker.tools.SandboxTools`, so every call passes
    through the policy engine before execution. The agent never receives an
    unwrapped tool; that is why the guarantees in ``REPAIR_INSTRUCTION`` hold
    even when the model ignores the instruction.

    Block 2 adds the Ledger: a recipe retrieved for this transition is injected
    ahead of the failure as prior art. See ADR 0004.
    """

    settings: Settings

    def model_for(self, attempt: int) -> str:
        """Flash first, Pro once Flash has had its attempts."""
        if attempt <= FLASH_ATTEMPTS:
            return self.settings.repair_model
        return self.settings.escalation_model

    def build_adk_agent(self, attempt: int, tools: SandboxTools) -> Any:
        """The ADK agent for one attempt. Separated so it can be asserted on."""
        from google.adk.agents import LlmAgent

        # Imported from the submodule rather than the package: `google.adk.tools`
        # builds its __all__ lazily at runtime, so a static checker cannot see
        # FunctionTool there even though it resolves fine when executed.
        from google.adk.tools.function_tool import FunctionTool

        return LlmAgent(
            name="nightshift_repair",
            model=self.model_for(attempt),
            instruction=REPAIR_INSTRUCTION,
            tools=[
                FunctionTool(tools.read_file),
                FunctionTool(tools.write_file),
                FunctionTool(tools.run_command),
            ],
        )

    def attempt(self, context: RepairContext, tools: SandboxTools) -> RepairProposal:
        """One turn of the loop. The suite, not this method, decides success."""
        from google.adk.runners import InMemoryRunner
        from google.genai import types

        runner = InMemoryRunner(agent=self.build_adk_agent(context.attempt, tools),
                                app_name=APP_NAME)
        session_id = _session_id(context)
        asyncio.run(
            runner.session_service.create_session(
                app_name=APP_NAME, user_id=AGENT_USER, session_id=session_id
            )
        )
        events: Sequence[Any] = list(
            runner.run(
                user_id=AGENT_USER,
                session_id=session_id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text=render_attempt_prompt(context))]
                ),
            )
        )
        return RepairProposal(rationale=final_text(events), tokens_used=total_tokens(events))


def _session_id(context: RepairContext) -> str:
    """One session per attempt. ADK session ids are opaque but must be tame."""
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", context.repo)
    return f"{slug}-{context.attempt}"


def build_repair_agent(settings: Settings | None = None) -> GeminiRepairAgent:
    """Construct the agent that runs the repair loop."""
    return GeminiRepairAgent(settings=settings or get_settings())
