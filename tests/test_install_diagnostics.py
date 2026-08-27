"""What the fleet records when an install fails and says nothing about why.

Two repositories in one wild run came back as "the project would not install"
with an empty `last error:` beneath it. That note names a repository without
naming a cause, which is worse than useless at fleet scale: it looks like a
pattern in other people's code when it was a pattern in our container.
"""

from __future__ import annotations

from services.worker.toolchain import _silent_failure


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
