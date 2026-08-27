"""The Librarian — the agent whose entire job is to teach the other agents.

It reads a finished repair and turns it into a rule that will help a different
repository. That is a genuinely different job from repairing, which is what
makes the agent catalog a network rather than one agent replicated.

**It cannot reach a repository.** Not by prompt discipline — by signature. Every
function here takes text and returns text; there is no ``Sandbox``, no path, no
tool. In production the same boundary is an IAM policy: the Librarian's service
account has Ledger write and no repository access at all, and the repair agent
has the reverse. An agent that cannot write to the Ledger cannot poison it, and
an agent that cannot read a repository cannot leak one into it.

**It must be able to say no.** Not every repair generalises: some are one
codebase's own mess, and a Librarian that always produces a recipe fills the
Ledger with advice that costs later repositories an attempt each to discard.
Refusal is a first-class answer here, and the parser treats anything it cannot
read as a refusal rather than guessing.

The model call cannot be unit-tested, so the seams either side of it are:
:func:`render_librarian_prompt` going in and :func:`parse_verdict` coming out.
Both are pure and both are covered.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from nightshift_core.config import Settings, get_settings
from nightshift_core.ledger import MigrationLedger, MigrationScope, Recipe
from nightshift_core.models import RepairAttempt, RepoJob
from services.worker.agent import (
    APP_NAME,
    ModelUnreachable,
    collect_events,
    configure_backend,
    final_text,
    total_tokens,
)

__all__ = [
    "LIBRARIAN_INSTRUCTION",
    "GeminiLibrarian",
    "Librarian",
    "LibrarianVerdict",
    "build_librarian",
    "parse_verdict",
    "render_librarian_prompt",
    "shelve_repair",
]

log = logging.getLogger("nightshift.librarian")

#: A rule longer than this is a diff in prose. The value of a recipe is that it
#: is shorter to read than the traceback it replaces.
MAX_RULE_CHARS = 900


LIBRARIAN_INSTRUCTION = """\
You are the Nightshift Librarian. A repair agent has just fixed a repository
whose test suite broke when a dependency was upgraded for a security advisory.
Your job is to decide whether that fix teaches us anything about the *library
transition*, and if so, to write it down for the next repository that hits it.

You will never see this repository again, and you cannot open any repository.
You are reading a finished record: the version transition, the failure, the diff
that fixed it, and the repairing agent's own explanation.

WHAT MAKES A GOOD RULE

A good rule is about the library, not about this codebase. The next reader is
another agent looking at a *different* repository with the same transition and a
similar traceback. Write for them.

- Name what the new version changed. "Jinja2 3.0 removed the top-level `Markup`
  and `escape` re-exports; import them from `markupsafe`."
- Say how it shows up. An ImportError at collection, a TypeError on a keyword
  argument, a return type that became a model instead of a dict.
- Say what the fix is, in general terms. Not this repository's line numbers.
- One paragraph. If it needs more, it is probably two rules or none.

WHEN TO REFUSE

Say no, and say it plainly, when:

- The fix was specific to this codebase — a local wrapper, a bad monkeypatch, a
  test fixture that happened to depend on private behaviour.
- The diff is large or touches several unrelated things. You cannot tell what
  actually fixed it, and guessing produces advice that wastes other agents'
  attempts.
- The repair took many attempts and the last one looks like a coincidence.
- You would be restating the traceback rather than explaining it.

Refusing costs nothing. A wrong rule costs every later repository an attempt.

WHAT YOU ARE NOT DECIDING

You are not deciding whether the repair was correct — the suite already did
that, and it is the only thing that does. You are not writing an instruction:
what you write is offered to later agents as prior art, and they will still be
judged only by their own repository's tests passing with the upgrade in place.

FORMAT — exactly these three lines, nothing else:

GENERALISABLE: yes
BREAK: removed-top-level-name
RULE: <one paragraph, no line breaks>

or:

GENERALISABLE: no
BREAK: -
RULE: <one sentence saying why not>
"""


@dataclass(frozen=True, slots=True)
class LibrarianVerdict:
    """What the Librarian concluded. ``generalisable`` may well be False."""

    generalisable: bool
    break_kind: str = ""
    rule: str = ""
    reason: str = ""
    tokens_used: int = 0

    @property
    def writable(self) -> bool:
        """A verdict only becomes a recipe if it says something and says yes."""
        return self.generalisable and bool(self.rule.strip()) and bool(self.break_kind)


class Librarian(Protocol):
    """Anything that can read a finished repair and produce a verdict."""

    def consider(self, prompt: str) -> LibrarianVerdict: ...


# --------------------------------------------------------------------------- #
# Going in
# --------------------------------------------------------------------------- #


def render_librarian_prompt(
    job: RepoJob, scope: MigrationScope, attempts: Sequence[RepairAttempt]
) -> str:
    """The finished repair, as a record rather than as a repository.

    Only the successful attempt's diff is shown in full. The failed ones are
    summarised: their value here is that they happened — a repair that took four
    tries is weaker evidence than one that took one — and pasting four rejected
    diffs would bury the one that worked.
    """
    successful = next((a for a in attempts if a.tests_passed), None)
    earlier = [a for a in attempts if not a.tests_passed]

    history = ""
    if earlier:
        history = "\n\nAttempts that did not work, in order:\n" + "\n".join(
            f"- Attempt {a.attempt}: {a.rationale or '(no rationale recorded)'}" for a in earlier
        )

    diff = successful.diff if successful else ""
    rationale = successful.rationale if successful else ""
    failure = attempts[0].failing_output if attempts else ""

    return (
        f"Transition: {scope}\n"
        f"Advisories: {', '.join(v.osv_id for v in job.vulnerabilities) or 'none recorded'}\n"
        f"Attempts used: {len(attempts)}\n\n"
        f"The failure the upgrade caused:\n\n```\n{failure[:4000]}\n```\n\n"
        f"The diff that made the suite pass:\n\n```diff\n{diff[:6000]}\n```\n\n"
        f"What the repairing agent said it did:\n\n{rationale or '(nothing recorded)'}"
        f"{history}"
    )


# --------------------------------------------------------------------------- #
# Coming out
# --------------------------------------------------------------------------- #

_FIELD = re.compile(r"^\s*(GENERALISABLE|BREAK|RULE)\s*:\s*(.*)$", re.IGNORECASE)
_YES = {"yes", "y", "true"}


def parse_verdict(text: str, *, tokens_used: int = 0) -> LibrarianVerdict:
    """Read the Librarian's answer, strictly.

    Anything unparseable is a refusal, never a guess. A malformed answer that
    became a recipe would put text of unknown provenance in front of every later
    repository that hits this transition — the one input to the fleet that no
    test can catch, because a plausible-sounding wrong rule looks exactly like a
    right one until it wastes an attempt.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = _FIELD.match(line)
        if match:
            key = match.group(1).upper()
            # First occurrence wins: a model that restates the block should not
            # be able to overwrite its own answer further down.
            fields.setdefault(key, match.group(2).strip())

    if "GENERALISABLE" not in fields:
        return LibrarianVerdict(generalisable=False, reason="unparseable answer")

    if fields["GENERALISABLE"].strip().lower() not in _YES:
        return LibrarianVerdict(
            generalisable=False,
            reason=fields.get("RULE", "").strip()[:300] or "declined without a reason",
            tokens_used=tokens_used,
        )

    rule = " ".join(fields.get("RULE", "").split())
    break_kind = _slug(fields.get("BREAK", ""))
    if not rule or not break_kind:
        return LibrarianVerdict(
            generalisable=False, reason="said yes but wrote nothing", tokens_used=tokens_used
        )
    if len(rule) > MAX_RULE_CHARS:
        return LibrarianVerdict(
            generalisable=False,
            reason=f"rule was {len(rule)} characters; a recipe is a paragraph",
            tokens_used=tokens_used,
        )
    return LibrarianVerdict(
        generalisable=True, break_kind=break_kind, rule=rule, tokens_used=tokens_used
    )


def _slug(raw: str) -> str:
    """A break kind is a topic in Memory Bank, so it has to be a stable token."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    return "" if cleaned in {"", "-", "n-a", "none"} else cleaned[:60]


# --------------------------------------------------------------------------- #
# The call site
# --------------------------------------------------------------------------- #


def shelve_repair(
    job: RepoJob,
    scope: MigrationScope,
    ledger: MigrationLedger,
    librarian: Librarian,
) -> Recipe | None:
    """Generalise a finished repair and write it down. None if it taught nothing.

    Called only after a repair actually succeeded and a pull request exists.
    Never raises: this is bookkeeping that happens after the work, and a
    Librarian failure must not turn a completed repair into a failed job.
    """
    if not any(a.tests_passed for a in job.repair_attempts):
        return None
    try:
        verdict = librarian.consider(render_librarian_prompt(job, scope, job.repair_attempts))
    except Exception:
        log.warning("librarian failed for %s; nothing shelved", job.repo, exc_info=True)
        return None

    if not verdict.writable:
        log.info("librarian declined %s: %s", scope, verdict.reason or "no reason given")
        return None

    try:
        return ledger.learn(
            scope, fact=verdict.rule, break_kind=verdict.break_kind, origin_repo=job.repo
        )
    except Exception:
        log.warning("could not write recipe for %s", scope, exc_info=True)
        return None


@dataclass(frozen=True, slots=True)
class GeminiLibrarian:
    """The Librarian, as an ADK agent with no tools and nothing to reach.

    The empty tool list is the whole security model of this agent, and it is
    worth being explicit about why it is empty rather than minimal. The repair
    agent needs to read and write a working tree, so it gets tools and a policy
    engine standing over them. The Librarian needs neither: it is handed a
    finished record as text and answers in text. Giving it one file-reading tool
    "just in case" would put a repository in front of the only agent whose
    output every later repository reads.
    """

    settings: Settings

    def build_adk_agent(self) -> Any:
        """The agent for one verdict. Separated so it can be asserted on."""
        from google.adk.agents import LlmAgent

        return LlmAgent(
            name="nightshift_librarian",
            model=self.settings.escalation_model,
            instruction=LIBRARIAN_INSTRUCTION,
            # Deliberately empty. See the class docstring: this is a boundary,
            # not an omission, and a test asserts it stays that way.
            tools=[],
        )

    def consider(self, prompt: str) -> LibrarianVerdict:
        """One reading of one finished repair."""
        from google.adk.runners import InMemoryRunner

        runner = InMemoryRunner(agent=self.build_adk_agent(), app_name=APP_NAME)
        # Derived from the prompt so a retry of the same record reuses nothing
        # and two different records never collide. ADK session ids are opaque
        # but must be tame, so this is a hash rather than the text.
        session_id = "librarian-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        # The same single-loop rule the repair agent follows, and for the same
        # reason: this runs in the same long-lived process, after it.
        events = collect_events(runner, session_id=session_id, prompt=prompt)

        text = final_text(events)
        tokens = total_tokens(events)
        # The same rule the repair agent follows, and it matters more here.
        # ``parse_verdict`` reads anything it cannot understand as a refusal,
        # which is the right default for a bad answer and the wrong one for no
        # answer: an outage would be filed as "the Librarian declined", and the
        # Ledger would look like it had considered this transition and said no.
        if not events or (tokens == 0 and not text.strip()):
            raise ModelUnreachable(
                "the librarian produced no answer and no token usage; "
                "see the SDK error logged above"
            )
        return parse_verdict(text, tokens_used=tokens)


def build_librarian(settings: Settings | None = None) -> GeminiLibrarian:
    """Construct the Gemini Librarian.

    Pro rather than Flash: generalising is the harder judgement in the fleet and
    it happens once per *transition*, not once per repository, so it is also the
    cheapest place to spend the better model. Which model that actually is comes
    from ``escalation_model``, so a project Vertex serves no Pro to degrades to
    Flash here rather than losing the Librarian altogether.
    """
    settings = settings or get_settings()
    configure_backend(settings)
    return GeminiLibrarian(settings=settings)
