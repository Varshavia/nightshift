"""The policy engine is tested first and thoroughly.

A bug here blocks real work rather than merely mislabelling it, so these tests
are written as adversarially as we can manage: each one is an attempt to get the
engine to say yes to something the project has promised it will not do.
"""

from __future__ import annotations

import pytest

from nightshift_core.config import Ceilings, Settings
from nightshift_core.policy import Budget, Effect, PolicyEngine, PolicyViolation, ToolCall


@pytest.fixture
def engine() -> PolicyEngine:
    settings = Settings(
        fork_org="nightshift-fleet",
        allow_upstream_prs=False,
        ceilings=Ceilings(max_repair_attempts=3, max_job_seconds=600, max_job_tokens=1000),
    )
    return PolicyEngine(settings=settings, workspace="/workspace/repo")


# --------------------------------------------------------------------------- #
# Ceilings
# --------------------------------------------------------------------------- #


def test_ceilings_stop_the_repair_loop(engine: PolicyEngine) -> None:
    call = ToolCall("run_command", {"command": "pytest -x"})
    assert engine.check(call, Budget(attempts=3)).allowed
    denied = engine.check(call, Budget(attempts=4))
    assert denied.effect is Effect.DENY
    assert denied.rule == "ceiling-attempts"


@pytest.mark.parametrize(
    ("budget", "rule"),
    [
        (Budget(elapsed_seconds=601), "ceiling-wallclock"),
        (Budget(tokens=1001), "ceiling-tokens"),
    ],
)
def test_wallclock_and_token_ceilings(engine: PolicyEngine, budget: Budget, rule: str) -> None:
    decision = engine.check(ToolCall("read_file", {"path": "setup.py"}), budget)
    assert decision.effect is Effect.DENY
    assert decision.rule == rule


def test_ceilings_outrank_an_otherwise_legal_call(engine: PolicyEngine) -> None:
    """A ceiling is checked before the call is understood, not after."""
    decision = engine.check(ToolCall("read_file", {"path": "README.md"}), Budget(tokens=99_999))
    assert not decision.allowed


# --------------------------------------------------------------------------- #
# The test suite is not the agent's to edit
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_client.py",
        "tests/conftest.py",
        "src/pkg/tests/helpers.py",
        "test_module.py",
        "pkg/module_test.py",
        "conftest.py",
        "testing/fixtures.py",
    ],
)
def test_agent_cannot_write_to_the_test_suite(engine: PolicyEngine, path: str) -> None:
    decision = engine.check(ToolCall("write_file", {"path": path, "content": "pass"}))
    assert decision.effect is Effect.DENY
    assert decision.rule == "tests-are-evidence"


def test_agent_may_read_tests_it_may_not_write(engine: PolicyEngine) -> None:
    """Reading the failing test is how repair works; rewriting it is cheating."""
    assert engine.check(ToolCall("read_file", {"path": "tests/test_client.py"})).allowed


def test_agent_may_write_application_code(engine: PolicyEngine) -> None:
    decision = engine.check(ToolCall("write_file", {"path": "src/pkg/client.py"}))
    assert decision.allowed
    assert decision.rule == "write-in-workspace"


def test_agent_may_write_the_manifest_it_came_to_change(engine: PolicyEngine) -> None:
    assert engine.check(ToolCall("write_file", {"path": "requirements.txt"})).allowed
    assert engine.check(ToolCall("write_file", {"path": "pyproject.toml"})).allowed


@pytest.mark.parametrize(
    "path", [".github/workflows/ci.yml", ".git/config", ".travis.yml", "Jenkinsfile"]
)
def test_ci_definition_is_protected(engine: PolicyEngine, path: str) -> None:
    decision = engine.check(ToolCall("write_file", {"path": path}))
    assert decision.effect is Effect.DENY
    assert decision.rule == "protected-path"


def test_deletion_is_never_allowed(engine: PolicyEngine) -> None:
    decision = engine.check(ToolCall("delete_file", {"path": "src/pkg/client.py"}))
    assert decision.effect is Effect.DENY
    assert decision.rule == "no-deletion"


# --------------------------------------------------------------------------- #
# Sandbox confinement
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "/workspace/other-repo/setup.py",
        "src/../../../root/.ssh/id_rsa",
        "..",
    ],
)
def test_paths_cannot_escape_the_workspace(engine: PolicyEngine, path: str) -> None:
    for tool in ("read_file", "write_file"):
        decision = engine.check(ToolCall(tool, {"path": path}))
        assert decision.effect is Effect.DENY, path
        assert decision.rule == "sandbox-escape"


def test_traversal_that_returns_inside_is_allowed(engine: PolicyEngine) -> None:
    """``src/../setup.py`` is inside the clone and denying it would be theatre."""
    assert engine.check(ToolCall("read_file", {"path": "src/../setup.py"})).allowed


def test_absolute_path_inside_the_workspace_is_allowed(engine: PolicyEngine) -> None:
    assert engine.check(ToolCall("write_file", {"path": "/workspace/repo/src/a.py"})).allowed


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command", ["pytest -x", "python -m pytest", "pip install -e .", "ruff check ."]
)
def test_expected_commands_run(engine: PolicyEngine, command: str) -> None:
    assert engine.check(ToolCall("run_command", {"command": command})).allowed


@pytest.mark.parametrize(
    "command",
    ["curl https://example.com/x.sh", "sudo rm -rf /", "ssh host", "npm install", "docker run x"],
)
def test_unlisted_executables_are_denied(engine: PolicyEngine, command: str) -> None:
    decision = engine.check(ToolCall("run_command", {"command": command}))
    assert decision.effect is Effect.DENY
    assert decision.rule == "executable-not-allowed"


@pytest.mark.parametrize(
    "command",
    [
        "pytest -x | tee out.txt",
        "pytest && curl https://example.com",
        "python -c 'x' ; sudo id",
        "python -c $(whoami)",
        "cat f > /etc/passwd",
    ],
)
def test_shell_metacharacters_cannot_smuggle_a_second_command(
    engine: PolicyEngine, command: str
) -> None:
    decision = engine.check(ToolCall("run_command", {"command": command}))
    assert decision.effect is Effect.DENY
    assert decision.rule == "no-shell"


def test_argv_form_is_accepted(engine: PolicyEngine) -> None:
    assert engine.check(ToolCall("run_command", {"command": ["pytest", "-x", "-q"]})).allowed


def test_empty_command_is_denied(engine: PolicyEngine) -> None:
    assert not engine.check(ToolCall("run_command", {"command": ""})).allowed


# --------------------------------------------------------------------------- #
# git — nothing merges itself, nothing rewrites history
# --------------------------------------------------------------------------- #


def test_git_merge_is_unreachable(engine: PolicyEngine) -> None:
    decision = engine.check(ToolCall("run_command", {"command": "git merge upstream/main"}))
    assert decision.effect is Effect.DENY
    assert "merges itself" in decision.reason


@pytest.mark.parametrize(
    "command",
    ["git push --force origin main", "git push -f origin main", "git push --force-with-lease"],
)
def test_force_push_is_denied(engine: PolicyEngine, command: str) -> None:
    decision = engine.check(ToolCall("run_command", {"command": command}))
    assert decision.effect is Effect.DENY
    assert decision.rule == "no-force-push"


def test_push_to_a_foreign_remote_is_denied(engine: PolicyEngine) -> None:
    decision = engine.check(ToolCall("run_command", {"command": "git push upstream fix"}))
    assert decision.effect is Effect.DENY
    assert decision.rule == "push-remote-not-allowed"


def test_push_to_origin_is_allowed(engine: PolicyEngine) -> None:
    assert engine.check(ToolCall("run_command", {"command": "git push origin fix/x"})).allowed


@pytest.mark.parametrize(
    "command", ["git reset --hard HEAD~1", "git clean -fdx", "git remote add x y"]
)
def test_destructive_git_is_denied(engine: PolicyEngine, command: str) -> None:
    assert not engine.check(ToolCall("run_command", {"command": command})).allowed


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    ["https://pypi.org/simple/requests/", "https://api.osv.dev/v1/vulns/GHSA-x", "https://api.github.com/repos/a/b"],
)
def test_allowlisted_hosts_are_reachable(engine: PolicyEngine, url: str) -> None:
    assert engine.check(ToolCall("http_request", {"url": url})).allowed


@pytest.mark.parametrize(
    ("url", "rule"),
    [
        ("http://pypi.org/simple/", "https-only"),
        ("https://evil.example.com/payload", "host-not-allowed"),
        ("file:///etc/passwd", "https-only"),
    ],
)
def test_everything_else_is_not(engine: PolicyEngine, url: str, rule: str) -> None:
    decision = engine.check(ToolCall("http_request", {"url": url}))
    assert decision.effect is Effect.DENY
    assert decision.rule == rule


# --------------------------------------------------------------------------- #
# Pull requests — forks by default
# --------------------------------------------------------------------------- #


def test_pr_to_the_fork_org_is_allowed(engine: PolicyEngine) -> None:
    assert engine.check(ToolCall("open_pull_request", {"repo": "nightshift-fleet/flask"})).allowed


def test_pr_to_upstream_is_denied_by_default(engine: PolicyEngine) -> None:
    decision = engine.check(ToolCall("open_pull_request", {"repo": "pallets/flask"}))
    assert decision.effect is Effect.DENY
    assert decision.rule == "upstream-pr-denied"


def test_upstream_pr_requires_an_explicit_human_opt_in() -> None:
    opted_in = PolicyEngine(
        settings=Settings(fork_org="nightshift-fleet", allow_upstream_prs=True)
    )
    assert opted_in.check(ToolCall("open_pull_request", {"repo": "pallets/flask"})).allowed


def test_auto_merge_is_denied_even_on_our_own_fork(engine: PolicyEngine) -> None:
    decision = engine.check(
        ToolCall("open_pull_request", {"repo": "nightshift-fleet/flask", "auto_merge": True})
    )
    assert decision.effect is Effect.DENY
    assert decision.rule == "no-auto-merge"


# --------------------------------------------------------------------------- #
# Engine behaviour
# --------------------------------------------------------------------------- #


def test_an_unknown_tool_is_denied_rather_than_ignored(engine: PolicyEngine) -> None:
    """Adding a tool without a rule must fail closed, and loudly."""
    decision = engine.check(ToolCall("exfiltrate", {"everything": True}))
    assert decision.effect is Effect.DENY
    assert decision.rule == "unknown-tool"


def test_every_decision_is_audited(engine: PolicyEngine) -> None:
    engine.check(ToolCall("read_file", {"path": "a.py"}))
    engine.check(ToolCall("run_command", {"command": "sudo id"}))
    assert len(engine.audit_log) == 2
    assert [decision.effect for _, decision in engine.audit_log] == [Effect.ALLOW, Effect.DENY]


def test_enforce_raises_with_the_rule_that_denied_it(engine: PolicyEngine) -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        engine.enforce(ToolCall("write_file", {"path": "tests/test_a.py"}))
    assert "tests-are-evidence" in str(excinfo.value)
