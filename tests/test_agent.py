"""The repair agent. What can be tested without a credential, is.

The model call itself cannot be unit-tested, so the seams either side of it are:
the prompt that goes in, and the parsing of the events that come out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.worker.agent import (
    REPAIR_INSTRUCTION,
    GeminiRepairAgent,
    build_repair_agent,
    final_text,
    render_attempt_prompt,
    total_tokens,
)
from services.worker.repair import RepairContext

from nightshift_core.config import Settings
from nightshift_core.models import RepairAttempt, Severity, Vulnerability

CONTEXT = RepairContext(
    repo="nightshift-fleet/example",
    vulnerabilities=(
        Vulnerability(
            osv_id="GHSA-abcd",
            package="jinja2",
            installed_version="2.11.3",
            fixed_version="3.1.2",
            severity=Severity.HIGH,
        ),
    ),
    failing_output="ImportError: cannot import name 'Markup' from 'jinja2'",
    attempt=1,
)


# -- the prompt ------------------------------------------------------------- #


def test_the_prompt_names_the_transition_and_the_failure() -> None:
    prompt = render_attempt_prompt(CONTEXT)
    assert "jinja2" in prompt
    assert "2.11.3" in prompt and "3.1.2" in prompt
    assert "cannot import name 'Markup'" in prompt


def test_the_prompt_carries_previous_attempts_forward() -> None:
    context = RepairContext(
        repo=CONTEXT.repo,
        vulnerabilities=CONTEXT.vulnerabilities,
        failing_output="still broken",
        attempt=2,
        previous=(
            RepairAttempt(
                attempt=1,
                failing_output="boom",
                rationale="tried the markupsafe import",
                tests_passed=False,
            ),
        ),
    )
    prompt = render_attempt_prompt(context)
    assert "tried the markupsafe import" in prompt
    assert "attempt 2" in prompt.lower()


def test_the_first_attempt_has_no_history_section() -> None:
    assert "already tried" not in render_attempt_prompt(CONTEXT)


def test_the_prompt_tells_the_agent_not_to_run_the_suite_itself() -> None:
    """The suite is the measure of success, so the agent does not get to run it."""
    assert "not run the test suite" in render_attempt_prompt(CONTEXT).lower()


def test_the_instruction_forbids_editing_tests() -> None:
    """The prompt says it and the policy engine enforces it. Both must hold."""
    assert "Do not edit, skip, xfail or delete any test" in REPAIR_INSTRUCTION


# -- escalation ------------------------------------------------------------- #


def test_the_agent_escalates_after_two_failed_attempts() -> None:
    settings = Settings(repair_model="flash", escalation_model="pro")
    agent = GeminiRepairAgent(settings=settings)
    assert agent.model_for(attempt=1) == "flash"
    assert agent.model_for(attempt=2) == "flash"
    assert agent.model_for(attempt=3) == "pro"
    assert agent.model_for(attempt=4) == "pro"


def test_constructing_the_agent_needs_no_credential() -> None:
    """Import and construction must be free — CI has no Google credentials."""
    assert isinstance(build_repair_agent(Settings()), GeminiRepairAgent)


def test_the_adk_agent_is_built_with_exactly_the_policy_gated_tools() -> None:
    """The agent must never hold a tool that did not come from SandboxTools."""

    class FakeTools:
        def read_file(self, path: str) -> str:
            return ""

        def write_file(self, path: str, content: str) -> str:
            return ""

        def run_command(self, command: list[str]) -> str:
            return ""

    built = GeminiRepairAgent(settings=Settings()).build_adk_agent(1, FakeTools())  # type: ignore[arg-type]
    assert {tool.func.__name__ for tool in built.tools} == {
        "read_file",
        "write_file",
        "run_command",
    }


# -- parsing what comes back ------------------------------------------------ #


@dataclass
class FakePart:
    text: str | None = None


@dataclass
class FakeContent:
    parts: list[FakePart]


@dataclass
class FakeUsage:
    total_token_count: int


@dataclass
class FakeEvent:
    content: Any = None
    usage_metadata: Any = None


def test_final_text_takes_the_last_thing_the_agent_said() -> None:
    events = [
        FakeEvent(content=FakeContent([FakePart("thinking out loud")])),
        FakeEvent(content=FakeContent([FakePart("the final explanation")])),
    ]
    assert final_text(events) == "the final explanation"


def test_final_text_survives_events_with_no_content() -> None:
    """Tool-call events carry no text and must not blank the rationale."""
    events = [
        FakeEvent(content=FakeContent([FakePart("the explanation")])),
        FakeEvent(content=None),
        FakeEvent(content=FakeContent([FakePart(None)])),
    ]
    assert final_text(events) == "the explanation"


def test_final_text_of_nothing_is_empty_not_an_error() -> None:
    assert final_text([]) == ""


def test_total_tokens_sums_every_turn() -> None:
    events = [
        FakeEvent(usage_metadata=FakeUsage(1200)),
        FakeEvent(usage_metadata=None),
        FakeEvent(usage_metadata=FakeUsage(800)),
    ]
    assert total_tokens(events) == 2000


def test_total_tokens_of_nothing_is_zero() -> None:
    assert total_tokens([]) == 0
