"""What the fleet records when an install fails and says nothing about why.

Two repositories in one wild run came back as "the project would not install"
with an empty `last error:` beneath it. That note names a repository without
naming a cause, which is worse than useless at fleet scale: it looks like a
pattern in other people's code when it was a pattern in our container.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from services.worker import toolchain
from services.worker.toolchain import EnvironmentBuildError, _silent_failure


def test_a_killed_command_is_reported_as_killed_not_as_broken() -> None:
    """A negative return code is a signal, and a wheel build that is killed
    mid-link has not written its error yet. The distinction matters because one
    of these is a line in the deployment and the other is a fact about the
    repository."""
    note = _silent_failure(-9)

    assert "signal 9" in note
    assert "memory" in note, "the operator has to be told where to look"


def test_an_ordinary_silent_exit_is_not_dressed_up_as_an_out_of_memory() -> None:
    """Guessing wrong is worse than saying little. Exit 1 with no output is a
    mystery and must read as one."""
    note = _silent_failure(1)

    assert "exited 1" in note
    assert "memory" not in note


def test_the_explanation_is_never_empty() -> None:
    """The whole point: `last error:` with nothing after it is the failure this
    function exists to prevent."""
    assert all(_silent_failure(code).strip() for code in (-9, -11, 0, 1, 127))


def test_every_attempt_gets_the_interpreter_it_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`python -m venv` over an existing directory keeps the interpreter that
    made it.

    The worker builds the same clone twice on purpose — the second time with an
    older interpreter, which is the entire point of the second attempt. Without
    `--clear` it got the first attempt's environment and logged the version it
    had asked for, so two repositories were recorded as unbuildable on 3.9 by a
    virtualenv that was 3.12. A wrong number that looks like a finding.
    """
    seen: list[list[str]] = []

    def fake_run(argv: Sequence[object], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append([str(part) for part in argv])
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="no")

    monkeypatch.setattr("services.worker.toolchain.subprocess.run", fake_run)

    with pytest.raises(EnvironmentBuildError):
        toolchain.build_environment(tmp_path)

    assert seen, "the virtualenv is created before anything else"
    assert "--clear" in seen[0], "a reused virtualenv is the wrong interpreter, silently"
