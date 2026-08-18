"""The tool layer is where the policy engine stops being theory.

Every test here is an attempt to reach the filesystem around it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services.worker.toolchain import Sandbox
from services.worker.tools import DENIAL_PREFIX, SandboxTools

from nightshift_core.config import Ceilings, Settings
from nightshift_core.policy import Budget, PolicyEngine


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app.py").write_text("from jinja2 import Markup\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def tools(repo: Path) -> SandboxTools:
    settings = Settings(fork_org="nightshift-fleet", ceilings=Ceilings(max_repair_attempts=3))
    policy = PolicyEngine(settings=settings, workspace=repo.as_posix())
    sandbox = Sandbox(repo_path=repo, python=Path("/usr/bin/python3"))
    return SandboxTools(sandbox=sandbox, policy=policy, budget=Budget())


def test_read_file_returns_contents(tools: SandboxTools) -> None:
    assert "from jinja2 import Markup" in tools.read_file("src/app.py")


def test_write_file_changes_the_working_tree(tools: SandboxTools, repo: Path) -> None:
    tools.write_file("src/app.py", "from markupsafe import Markup\n")
    assert (
        repo.joinpath("src/app.py").read_text(encoding="utf-8")
        == "from markupsafe import Markup\n"
    )


def test_writing_a_test_file_is_denied_and_leaves_it_untouched(
    tools: SandboxTools, repo: Path
) -> None:
    before = repo.joinpath("tests/test_app.py").read_text(encoding="utf-8")
    result = tools.write_file("tests/test_app.py", "def test_ok() -> None:\n    pass\n")
    assert result.startswith(DENIAL_PREFIX)
    assert "tests-are-evidence" in result
    assert repo.joinpath("tests/test_app.py").read_text(encoding="utf-8") == before


def test_escaping_the_workspace_is_denied(tools: SandboxTools, tmp_path: Path) -> None:
    result = tools.write_file("../escaped.py", "x = 1\n")
    assert result.startswith(DENIAL_PREFIX)
    assert not tmp_path.joinpath("escaped.py").exists()


def test_reading_outside_the_workspace_is_denied(tools: SandboxTools) -> None:
    assert tools.read_file("/etc/passwd").startswith(DENIAL_PREFIX)


def test_denied_calls_are_recorded_for_the_audit_trail(tools: SandboxTools) -> None:
    tools.write_file("tests/test_app.py", "pass\n")
    tools.write_file("../escaped.py", "pass\n")
    assert [decision.rule for decision in tools.denials] == [
        "tests-are-evidence",
        "sandbox-escape",
    ]


def test_a_disallowed_executable_is_denied(tools: SandboxTools) -> None:
    assert tools.run_command(["curl", "https://example.com"]).startswith(DENIAL_PREFIX)


def test_reading_a_missing_file_reports_rather_than_raises(tools: SandboxTools) -> None:
    assert "does not exist" in tools.read_file("src/nope.py")
