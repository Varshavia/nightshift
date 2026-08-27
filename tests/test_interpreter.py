"""Which interpreter a repository gets offered, and why that one.

Fetching a Python is a subprocess and is not tested here. What is tested is the
decision in front of it, because the decision is where the fleet was wrong: it
offered 3.12 to every repository including the ones that had written down, in a
file we were already reading for other reasons, that they wanted something else.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from services.worker import interpreter
from services.worker.interpreter import (
    CANDIDATES,
    bounded_above,
    choose_interpreter,
    declared_requirement,
)


def write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_packaging_metadata_is_read_before_a_workflow(tmp_path: Path) -> None:
    """A promise to everyone installing the project outranks one machine's habit.

    `requires-python` is what the project guarantees. A CI workflow is usually
    right and occasionally a leftover from a version nobody supports any more.
    """
    write(tmp_path, "pyproject.toml", '[project]\nrequires-python = ">=3.9"\n')
    write(tmp_path, ".github/workflows/ci.yml", "        python-version: '3.8'\n")

    assert declared_requirement(tmp_path) == (">=3.9", "pyproject.toml requires-python")


def test_a_workflow_answers_when_the_metadata_does_not(tmp_path: Path) -> None:
    """Most repositories with no packaging metadata still have CI, and CI is a
    machine that demonstrably builds the project — better evidence than ours."""
    write(tmp_path, ".github/workflows/tests.yml", "      - uses: actions/setup-python\n"
          "        with:\n          python-version: \"3.9\"\n")

    requirement, source = declared_requirement(tmp_path)

    assert requirement == "==3.9.*", "their CI runs one version, not a range"
    assert source.endswith("tests.yml")


def test_setup_py_is_read_for_projects_that_never_moved_on(tmp_path: Path) -> None:
    """The repositories most likely to have a dangerous old pin are exactly the
    ones that never adopted pyproject.toml."""
    write(tmp_path, "setup.py", "setup(name='x', python_requires='>=3.6,<3.10')\n")

    assert declared_requirement(tmp_path) == (">=3.6,<3.10", "setup.py python_requires")


def test_a_repository_that_says_nothing_keeps_what_it_had(tmp_path: Path) -> None:
    """Silence is not a request. The fleet's own interpreter is the right answer
    and was the only answer before this module existed."""
    choice = choose_interpreter(tmp_path)

    assert choice.python == Path(sys.executable)
    assert choice.requirement == ""


def test_an_unparseable_requirement_does_not_fail_the_job(tmp_path: Path) -> None:
    """A malformed `requires-python` is somebody else's typo. It costs the fleet
    a better interpreter, not the repository."""
    write(tmp_path, "pyproject.toml", '[project]\nrequires-python = "not a specifier"\n')

    assert choose_interpreter(tmp_path).python == Path(sys.executable)


def test_a_requirement_outside_our_range_is_not_silently_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A project wanting 3.6 is outside what this fleet supports — which is a
    different sentence from "we could not build it", and the log has to say so
    or every such repository looks like a build failure we might have fixed."""
    write(tmp_path, "pyproject.toml", '[project]\nrequires-python = "<3.7"\n')

    with caplog.at_level("INFO"):
        choose_interpreter(tmp_path)

    assert "no supported interpreter satisfies" in caplog.text


def test_the_running_interpreter_wins_when_it_already_qualifies(tmp_path: Path) -> None:
    """No fetch, no subprocess, no wait. Most repositories accept a range that
    includes what the worker is already running, and paying `uv` for that would
    be a download per repository for no change at all."""
    running = ".".join(str(part) for part in sys.version_info[:2])
    write(tmp_path, "pyproject.toml", f'[project]\nrequires-python = ">={running}"\n')

    choice = choose_interpreter(tmp_path)

    assert choice.python == Path(sys.executable)
    assert choice.version == running


def test_a_narrow_ceiling_reaches_for_an_older_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the whole module exists for.

    Python 3.12 removed `distutils`, so a project pinned before 2023 installs
    through a `setup.py` that imports it and fails on a line that has nothing to
    do with the project. It arrives as UNBUILDABLE, which reads as a fact about
    the repository and was a fact about our container.
    """
    write(tmp_path, "pyproject.toml", '[project]\nrequires-python = ">=3.8,<3.10"\n')
    monkeypatch.setattr(interpreter, "resolve", lambda version, **kw: Path(f"/fake/{version}"))

    choice = choose_interpreter(tmp_path)

    assert choice.version == "3.9", "the top of the range they allow"
    assert choice.python == Path("/fake/3.9")


def test_a_machine_without_uv_degrades_rather_than_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing tool leaves the fleet doing what it did yesterday. It must not
    turn a repository we could half-build into a job that raises."""
    write(tmp_path, "pyproject.toml", '[project]\nrequires-python = ">=3.8,<3.10"\n')
    monkeypatch.setattr(interpreter, "resolve", lambda version, **kw: None)

    assert choose_interpreter(tmp_path).python == Path(sys.executable)


def test_the_candidates_are_ordered_newest_first() -> None:
    """A project that accepts a range is usually happiest at the top of it: that
    is where its dependencies have wheels and where its CI probably runs."""
    assert tuple(
        sorted(CANDIDATES, key=lambda v: [int(part) for part in v.split(".")], reverse=True)
    ) == CANDIDATES


def test_an_open_ended_requirement_rules_out_nothing_in_the_future() -> None:
    """`bonobo` publishes `>=3.5`, written when "later" meant 3.7.

    Read as a claim that 3.12 works it produced forty-one collection errors and
    one passing test. A lower bound says which interpreters are too old and
    nothing at all about which are too new, and the difference decides whether
    the fleet is allowed a second attempt.
    """
    assert not bounded_above(">=3.5")
    assert not bounded_above(">=3.9")


def test_a_ceiling_is_an_answer_and_is_respected() -> None:
    """A project that names an upper bound has been given what it asked for."""
    assert bounded_above(">=3.8,<3.10")
    assert bounded_above("==3.9.*")
    assert bounded_above("~=3.11")
    assert bounded_above("<3.13")


def test_a_requirement_nobody_can_parse_is_not_treated_as_a_ceiling() -> None:
    """A typo must cost the fleet a better interpreter, never a second attempt
    it would otherwise have been allowed."""
    assert not bounded_above("not a specifier")
