"""The Librarian: what it is given, what it may write, and when it must refuse.

The Ledger's read path has tests. This is the write path, and it is the more
dangerous one. A wrong recipe is the only input to this fleet that no test can
catch downstream — it looks exactly like a right one until it costs a later
repository an attempt. So the parser is tested adversarially and refusal is
treated as the correct answer rather than as a failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services.worker.librarian import (
    LIBRARIAN_INSTRUCTION,
    LibrarianVerdict,
    parse_verdict,
    render_librarian_prompt,
    shelve_repair,
)

from nightshift_core.ledger import (
    InMemoryRecordStore,
    MigrationLedger,
    MigrationScope,
    RecipeStatus,
    RecordBackedRecall,
)
from nightshift_core.models import RepairAttempt, RepoJob, Severity, Vulnerability

JINJA = MigrationScope(library="jinja2", from_version="2.11.3", to_version="3.1.2")
RULE = (
    "Jinja2 3.0 removed the top-level Markup and escape re-exports; import them "
    "from markupsafe. It shows up as an ImportError during collection."
)

GOOD = f"GENERALISABLE: yes\nBREAK: removed-top-level-name\nRULE: {RULE}"


def make_job(attempts: list[RepairAttempt] | None = None) -> RepoJob:
    job = RepoJob(
        job_id="run1:org/example",
        repo="org/example",
        vulnerabilities=[
            Vulnerability(
                osv_id="GHSA-a",
                package="jinja2",
                installed_version="2.11.3",
                fixed_version="3.1.2",
                severity=Severity.HIGH,
            )
        ],
    )
    for attempt in attempts or []:
        job.record_attempt(attempt)
    return job


def attempt(n: int, *, passed: bool, diff: str = "", rationale: str = "") -> RepairAttempt:
    return RepairAttempt(
        attempt=n,
        failing_output="E ImportError: cannot import name 'Markup' from 'jinja2'",
        diff=diff,
        rationale=rationale,
        tests_passed=passed,
    )


def fresh_ledger() -> MigrationLedger:
    records = InMemoryRecordStore()
    return MigrationLedger(recall=RecordBackedRecall(records), records=records)


class ScriptedLibrarian:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    def consider(self, prompt: str) -> LibrarianVerdict:
        self.prompts.append(prompt)
        return parse_verdict(self.text)


# --------------------------------------------------------------------------- #
# It cannot reach a repository
# --------------------------------------------------------------------------- #


def test_the_librarian_is_given_a_record_not_a_repository() -> None:
    """The boundary is the signature, not the prompt.

    Checked against the annotations rather than the source text, so that
    *writing about* a Sandbox in a docstring is fine and *accepting* one is not.
    Nothing here takes a sandbox, a path or a tool, so there is no repository to
    reach even before IAM has an opinion about it.
    """
    import inspect

    from services.worker import librarian as module

    forbidden = ("Sandbox", "Path", "SandboxTools")
    for name, obj in vars(module).items():
        if not (inspect.isfunction(obj) and obj.__module__ == module.__name__):
            continue
        for parameter, annotation in inspect.get_annotations(obj).items():
            assert not any(word in str(annotation) for word in forbidden), (
                f"{name}({parameter}: {annotation}) would give the Librarian a repository"
            )

    assert not hasattr(module, "subprocess")
    assert not hasattr(module, "open_pull_request")


def test_the_instruction_says_the_suite_still_decides() -> None:
    assert "only thing that does" in LIBRARIAN_INSTRUCTION
    assert "not writing an instruction" in LIBRARIAN_INSTRUCTION


# --------------------------------------------------------------------------- #
# What it is shown
# --------------------------------------------------------------------------- #


def test_only_the_successful_diff_is_shown_in_full() -> None:
    """Pasting four rejected diffs would bury the one that worked."""
    job = make_job(
        [
            attempt(1, passed=False, diff="--- wrong turn ---", rationale="tried a shim"),
            attempt(2, passed=True, diff="--- the real fix ---", rationale="imported markupsafe"),
        ]
    )
    prompt = render_librarian_prompt(job, JINJA, job.repair_attempts)
    assert "--- the real fix ---" in prompt
    assert "--- wrong turn ---" not in prompt


def test_the_failed_attempts_are_still_counted() -> None:
    """A repair that took four tries is weaker evidence than one that took one."""
    job = make_job([attempt(1, passed=False, rationale="tried a shim"), attempt(2, passed=True)])
    prompt = render_librarian_prompt(job, JINJA, job.repair_attempts)
    assert "Attempts used: 2" in prompt
    assert "tried a shim" in prompt


def test_the_transition_and_advisory_are_named() -> None:
    job = make_job([attempt(1, passed=True)])
    prompt = render_librarian_prompt(job, JINJA, job.repair_attempts)
    assert "jinja2 2.11.3" in prompt and "3.1.2" in prompt
    assert "GHSA-a" in prompt


def test_a_prompt_can_be_rendered_with_no_attempts_at_all() -> None:
    assert render_librarian_prompt(make_job(), JINJA, []) != ""


# --------------------------------------------------------------------------- #
# What it may write — the parser, adversarially
# --------------------------------------------------------------------------- #


def test_a_well_formed_yes_becomes_a_recipe() -> None:
    verdict = parse_verdict(GOOD)
    assert verdict.writable
    assert verdict.break_kind == "removed-top-level-name"
    assert verdict.rule == RULE


def test_a_refusal_is_a_correct_answer_not_a_failure() -> None:
    verdict = parse_verdict(
        "GENERALISABLE: no\nBREAK: -\nRULE: the fix was a local wrapper, specific to this repo"
    )
    assert not verdict.writable
    assert not verdict.generalisable
    assert "local wrapper" in verdict.reason


@pytest.mark.parametrize(
    "text",
    [
        "",
        "I think this generalises! Here is my rule: import from markupsafe.",
        "BREAK: removed-top-level-name\nRULE: something",
        "```json\n{\"generalisable\": true}\n```",
    ],
)
def test_anything_unparseable_is_a_refusal_never_a_guess(text: str) -> None:
    """A malformed answer that became a recipe would put text of unknown
    provenance in front of every later repository hitting this transition."""
    verdict = parse_verdict(text)
    assert not verdict.writable
    assert not verdict.generalisable


def test_yes_with_an_empty_rule_writes_nothing() -> None:
    assert not parse_verdict("GENERALISABLE: yes\nBREAK: x\nRULE:   ").writable


def test_yes_with_no_break_kind_writes_nothing() -> None:
    """The break kind is a Memory Bank topic; an empty one is not a shelf."""
    assert not parse_verdict(f"GENERALISABLE: yes\nBREAK: -\nRULE: {RULE}").writable


def test_a_rule_that_is_really_a_diff_is_refused() -> None:
    """The value of a recipe is being shorter to read than the traceback."""
    verdict = parse_verdict(f"GENERALISABLE: yes\nBREAK: x\nRULE: {'word ' * 400}")
    assert not verdict.writable
    assert "paragraph" in verdict.reason


def test_the_first_answer_wins_if_the_model_restates_itself() -> None:
    """A model that repeats the block must not be able to overwrite its own
    answer further down, which is how a refusal becomes a yes by accident."""
    verdict = parse_verdict(
        "GENERALISABLE: no\nBREAK: -\nRULE: too specific\n\n"
        f"GENERALISABLE: yes\nBREAK: y\nRULE: {RULE}"
    )
    assert not verdict.generalisable


def test_the_break_kind_is_normalised_into_a_stable_token() -> None:
    verdict = parse_verdict(f"GENERALISABLE: yes\nBREAK: Removed Top-Level Name!\nRULE: {RULE}")
    assert verdict.break_kind == "removed-top-level-name"


def test_a_multiline_rule_is_flattened_rather_than_truncated() -> None:
    verdict = parse_verdict("GENERALISABLE: yes\nBREAK: x\nRULE: first half\nsecond half")
    assert verdict.rule == "first half"


# --------------------------------------------------------------------------- #
# The call site
# --------------------------------------------------------------------------- #


def test_a_successful_repair_is_shelved() -> None:
    ledger = fresh_ledger()
    job = make_job([attempt(1, passed=True, diff="d", rationale="r")])

    recipe = shelve_repair(job, JINJA, ledger, ScriptedLibrarian(GOOD))

    assert recipe is not None
    assert recipe.status is RecipeStatus.PROVISIONAL
    assert recipe.origin_repo == "org/example"
    assert ledger.lookup(JINJA).recipe is not None


def test_a_repair_that_never_succeeded_is_not_shelved() -> None:
    """There is nothing to generalise from a repair that did not work."""
    ledger = fresh_ledger()
    job = make_job([attempt(1, passed=False)])
    assert shelve_repair(job, JINJA, ledger, ScriptedLibrarian(GOOD)) is None
    assert ledger.lookup(JINJA).recipe is None


def test_a_declined_repair_leaves_the_ledger_empty() -> None:
    ledger = fresh_ledger()
    job = make_job([attempt(1, passed=True)])
    declined = ScriptedLibrarian("GENERALISABLE: no\nBREAK: -\nRULE: one codebase's own mess")

    assert shelve_repair(job, JINJA, ledger, declined) is None
    assert ledger.lookup(JINJA).recipe is None


def test_a_librarian_that_raises_does_not_undo_a_finished_repair() -> None:
    """This is bookkeeping after the work. The pull request already exists."""

    class Broken:
        def consider(self, prompt: str) -> LibrarianVerdict:
            raise RuntimeError("Gemini is unavailable")

    ledger = fresh_ledger()
    job = make_job([attempt(1, passed=True)])
    assert shelve_repair(job, JINJA, ledger, Broken()) is None


def test_shelving_twice_does_not_overwrite_the_evidenced_recipe() -> None:
    ledger = fresh_ledger()
    first = make_job([attempt(1, passed=True)])
    shelve_repair(first, JINJA, ledger, ScriptedLibrarian(GOOD))

    second = RepoJob(
        job_id="run1:org/other", repo="org/other", vulnerabilities=first.vulnerabilities
    )
    second.record_attempt(attempt(1, passed=True))
    rival = ScriptedLibrarian("GENERALISABLE: yes\nBREAK: z\nRULE: something else")
    shelve_repair(second, JINJA, ledger, rival)

    recipe = ledger.lookup(JINJA).recipe
    assert recipe is not None
    assert recipe.fact == RULE
    assert recipe.origin_repo == "org/example"


def test_the_librarian_reads_the_record_it_was_rendered(tmp_path: Path) -> None:
    ledger = fresh_ledger()
    job = make_job([attempt(1, passed=True, diff="--- fix ---", rationale="imported markupsafe")])
    librarian = ScriptedLibrarian(GOOD)

    shelve_repair(job, JINJA, ledger, librarian)

    assert "--- fix ---" in librarian.prompts[0]
    assert "imported markupsafe" in librarian.prompts[0]


def test_the_librarian_is_built_with_no_tools_at_all() -> None:
    """The boundary in the module docstring, asserted rather than described.

    "It cannot reach a repository. Not by prompt discipline — by signature."
    An empty tool list is what makes that true at runtime. One file-reading tool
    added later "just in case" would put a repository in front of the only agent
    whose output every later repository reads.
    """
    pytest.importorskip("google.adk")
    from services.worker.librarian import build_librarian

    from nightshift_core.config import Settings

    agent = build_librarian(Settings(gcp_project="p")).build_adk_agent()

    assert list(agent.tools) == []
    assert agent.name == "nightshift_librarian"


def test_the_librarian_reaches_for_the_escalation_model() -> None:
    """Generalising happens once per transition, not once per repository, so it
    is the cheapest place in the fleet to spend the better model."""
    pytest.importorskip("google.adk")
    from services.worker.librarian import build_librarian

    from nightshift_core.config import Settings

    settings = Settings(gcp_project="p", escalation_model="gemini-3.5-pro")

    assert build_librarian(settings).build_adk_agent().model == "gemini-3.5-pro"


def test_an_outage_is_not_recorded_as_the_librarian_declining() -> None:
    """`parse_verdict` reads anything it cannot understand as a refusal, which
    is right for a bad answer and wrong for no answer.

    Filing an outage as "declined" would leave the Ledger looking like it had
    considered this transition and decided there was nothing to learn — so the
    next repository with the same break inherits a silence that was never a
    judgement.
    """
    from services.worker.agent import ModelUnreachable

    class Dead:
        def consider(self, prompt: str) -> LibrarianVerdict:
            raise ModelUnreachable("no credentials")

    job, scope, ledger = _repaired_job()

    assert shelve_repair(job, scope, ledger, Dead()) is None
    assert ledger.lookup(scope).recipe is None, "nothing may be written from nothing"


def _repaired_job() -> tuple[RepoJob, MigrationScope, MigrationLedger]:
    job = RepoJob(
        job_id="run-1:a/b",
        repo="a/b",
        vulnerabilities=[
            Vulnerability(
                osv_id="GHSA-x",
                package="jinja2",
                installed_version="2.11.3",
                fixed_version="3.1.2",
                severity=Severity.HIGH,
            )
        ],
        repair_attempts=[
            RepairAttempt(
                attempt=1,
                tests_passed=True,
                diff="-a\n+b",
                rationale="moved import",
                failing_output="ImportError",
            )
        ],
    )
    scope = MigrationScope(library="jinja2", from_version="2.11.3", to_version="3.1.2")
    ledger = fresh_ledger()
    return job, scope, ledger
