"""Diff capture. What the reviewer at nine in the morning actually reads."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from services.worker.toolchain import Sandbox, capture_diff, diff_stats


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    (root / "app.py").write_text("from jinja2 import Markup\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True, capture_output=True)
    return root


@pytest.fixture
def sandbox(git_repo: Path) -> Sandbox:
    return Sandbox(repo_path=git_repo, python=Path("/usr/bin/python3"))


def test_no_changes_gives_an_empty_diff(sandbox: Sandbox) -> None:
    assert capture_diff(sandbox) == ""


def test_a_modification_appears_in_the_diff(sandbox: Sandbox, git_repo: Path) -> None:
    git_repo.joinpath("app.py").write_text("from markupsafe import Markup\n", encoding="utf-8")
    diff = capture_diff(sandbox)
    assert "-from jinja2 import Markup" in diff
    assert "+from markupsafe import Markup" in diff


def test_a_new_file_appears_in_the_diff(sandbox: Sandbox, git_repo: Path) -> None:
    """Untracked files must be included or a repair that adds a shim looks empty."""
    git_repo.joinpath("compat.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert "compat.py" in capture_diff(sandbox)


def test_diff_stats_counts_files_and_lines() -> None:
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-from jinja2 import Markup\n"
        "+from markupsafe import Markup\n"
        "diff --git a/other.py b/other.py\n"
        "--- a/other.py\n"
        "+++ b/other.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    stats = diff_stats(diff)
    assert (stats.files, stats.added, stats.removed) == (2, 2, 1)


def test_diff_stats_of_an_empty_diff_is_all_zero() -> None:
    stats = diff_stats("")
    assert (stats.files, stats.added, stats.removed) == (0, 0, 0)
