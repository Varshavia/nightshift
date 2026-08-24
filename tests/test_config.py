"""Reading ``.env``, and the one rule that makes it safe to do so.

A dotenv reader is trivial to write and easy to write wrongly in a way that only
shows up in production: if the file overwrites the environment rather than
filling gaps, a stale ``.env`` baked into an image silently outranks what Cloud
Run injects, and the service talks to the wrong project with the wrong
credential. The first test here is that rule; the rest are the format.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from nightshift_core import config
from nightshift_core.config import get_settings, load_env_file


@pytest.fixture(autouse=True)
def _unread(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts as though nothing had read a dotenv file yet.

    The module remembers that it has run, because reading twice cannot pick up
    edits anyway — the values are already in ``os.environ``. Tests need that
    memory cleared, and the settings cache with it — on the way out as well as
    on the way in, or a ``Settings`` built from a temporary directory outlives
    the temporary directory and reaches whatever runs next.
    """
    monkeypatch.setattr(config, "_ENV_FILE_LOADED", False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def write(tmp_path: Path, body: str) -> Path:
    target = tmp_path / ".env"
    target.write_text(body, encoding="utf-8")
    return target


def test_a_real_environment_variable_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule the whole file exists to protect."""
    monkeypatch.setenv("GITHUB_TOKEN", "from-the-environment")
    path = write(tmp_path, "GITHUB_TOKEN=from-the-file\n")

    assert load_env_file(path) == 0
    assert os.environ["GITHUB_TOKEN"] == "from-the-environment"


def test_fills_a_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    path = write(tmp_path, "GITHUB_TOKEN=github_pat_example\n")

    assert load_env_file(path) == 1
    assert os.environ["GITHUB_TOKEN"] == "github_pat_example"


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """Most machines have no ``.env``, and that is the normal case, not a fault."""
    assert load_env_file(tmp_path / "nothing-here") == 0


def test_understands_the_four_things_dotenv_files_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("A_TOKEN", "B_TOKEN", "C_TOKEN", "D_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    path = write(
        tmp_path,
        "\n".join(
            [
                "# a comment",
                "",
                "   ",
                "A_TOKEN=plain",
                'B_TOKEN="double quoted"',
                "C_TOKEN='single quoted'",
                "export D_TOKEN=exported",
                "not-an-assignment",
            ]
        )
        + "\n",
    )

    assert load_env_file(path) == 4
    assert os.environ["A_TOKEN"] == "plain"
    assert os.environ["B_TOKEN"] == "double quoted"
    assert os.environ["C_TOKEN"] == "single quoted"
    assert os.environ["D_TOKEN"] == "exported"


def test_a_value_may_contain_an_equals_sign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Base64 and connection strings are full of them; only the first splits."""
    monkeypatch.delenv("A_TOKEN", raising=False)
    path = write(tmp_path, "A_TOKEN=abc==def=\n")

    assert load_env_file(path) == 1
    assert os.environ["A_TOKEN"] == "abc==def="


def test_reads_once_per_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second call is a no-op, including for a different file.

    Not an optimisation: once the first file's values are in ``os.environ`` they
    are indistinguishable from variables that were always there, so a second
    file could never override them and pretending otherwise would mislead.
    """
    monkeypatch.delenv("A_TOKEN", raising=False)
    monkeypatch.delenv("B_TOKEN", raising=False)
    first = write(tmp_path, "A_TOKEN=first\n")
    second = tmp_path / "other.env"
    second.write_text("B_TOKEN=second\n", encoding="utf-8")

    assert load_env_file(first) == 1
    assert load_env_file(second) == 0
    assert "B_TOKEN" not in os.environ


def test_settings_see_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The end of the chain: this is what the scripts actually rely on."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "GITHUB_TOKEN=github_pat_example\n")

    assert get_settings().github_token == "github_pat_example"


def test_a_secret_never_reaches_the_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Loading reports a count, never a name and never a value."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    path = write(tmp_path, "GITHUB_TOKEN=github_pat_secret\n")

    with caplog.at_level("DEBUG"):
        load_env_file(path)

    assert "github_pat_secret" not in caplog.text
