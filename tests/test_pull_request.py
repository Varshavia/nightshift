"""Opening the pull request. Forks by default; nothing merges itself."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from services.worker.pull_request import PullRequestBlocked, open_pr
from services.worker.toolchain import Sandbox

from nightshift_core.config import Settings
from nightshift_core.models import RepairAttempt, RepoJob, Severity, Vulnerability
from nightshift_core.policy import PolicyEngine


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create_pull_request(self, repo: str, head: str, title: str, body: str) -> str:
        self.calls.append({"repo": repo, "head": head, "title": title, "body": body})
        return f"https://github.com/{repo}/pull/1"


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    root = tmp_path / "repo"
    root.mkdir()
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "fleet@example.com"],
        ["git", "config", "user.name", "Nightshift"],
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    (root / "app.py").write_text("from jinja2 import Markup\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True, capture_output=True)
    (root / "app.py").write_text("from markupsafe import Markup\n", encoding="utf-8")
    return Sandbox(repo_path=root, python=Path("/usr/bin/python3"))


def make_job(repo: str = "nightshift-fleet/example") -> RepoJob:
    job = RepoJob(
        job_id="run1:" + repo,
        repo=repo,
        vulnerabilities=[
            Vulnerability(
                osv_id="GHSA-abcd",
                package="jinja2",
                installed_version="2.11.3",
                fixed_version="3.1.2",
                severity=Severity.HIGH,
                summary="Sandbox escape",
            )
        ],
    )
    job.record_attempt(
        RepairAttempt(
            attempt=1, failing_output="ImportError", diff="-a\n+b\n",
            rationale="moved to markupsafe", tests_passed=True, tokens_used=10,
        )
    )
    return job


def engine_for(sandbox: Sandbox, settings: Settings) -> PolicyEngine:
    return PolicyEngine(settings=settings, workspace=sandbox.repo_path.as_posix())


#: No token means the local-development path: commit but do not push.
LOCAL = Settings(fork_org="nightshift-fleet", github_token=None)


def test_a_pull_request_to_our_own_fork_is_opened(sandbox: Sandbox) -> None:
    client = FakeGitHub()
    url = open_pr(
        make_job(), sandbox, engine_for(sandbox, LOCAL), LOCAL, client,
        baseline_green=True, model="gemini-3.5-flash",
    )
    assert url == "https://github.com/nightshift-fleet/example/pull/1"
    assert "jinja2" in client.calls[0]["title"]


def test_an_upstream_pull_request_is_blocked_by_default(sandbox: Sandbox) -> None:
    """ALLOW_UPSTREAM_PRS is false and the fleet does not decide otherwise."""
    settings = Settings(fork_org="nightshift-fleet", allow_upstream_prs=False, github_token=None)
    client = FakeGitHub()
    with pytest.raises(PullRequestBlocked) as caught:
        open_pr(
            make_job("someone-else/library"), sandbox, engine_for(sandbox, settings),
            settings, client, baseline_green=True, model="gemini-3.5-flash",
        )
    assert caught.value.decision.rule == "upstream-pr-denied"
    assert client.calls == [], "nothing was opened"


def test_nothing_is_committed_when_the_policy_blocks(sandbox: Sandbox) -> None:
    """A denial must not leave a branch behind that no pull request references."""
    settings = Settings(fork_org="nightshift-fleet", allow_upstream_prs=False, github_token=None)
    with pytest.raises(PullRequestBlocked):
        open_pr(
            make_job("someone-else/library"), sandbox, engine_for(sandbox, settings),
            settings, FakeGitHub(), baseline_green=True, model="gemini-3.5-flash",
        )
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=sandbox.repo_path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert not branch.startswith("nightshift/")


def test_the_branch_is_created_and_the_change_committed(sandbox: Sandbox) -> None:
    open_pr(
        make_job(), sandbox, engine_for(sandbox, LOCAL), LOCAL, FakeGitHub(),
        baseline_green=True, model="gemini-3.5-flash",
    )
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=sandbox.repo_path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch.startswith("nightshift/")
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=sandbox.repo_path,
        capture_output=True, text=True, check=True,
    ).stdout
    assert status.strip() == "", "the repair should be committed, not left dirty"


def test_the_body_reaches_github_with_the_disclosure(sandbox: Sandbox) -> None:
    client = FakeGitHub()
    open_pr(
        make_job(), sandbox, engine_for(sandbox, LOCAL), LOCAL, client,
        baseline_green=True, model="gemini-3.5-flash",
    )
    assert "written by an AI agent" in client.calls[0]["body"]


def test_auto_merge_is_not_reachable(sandbox: Sandbox) -> None:
    """There is no flag for it, and the engine denies it if one ever appears."""
    from nightshift_core.policy import ToolCall

    decision = engine_for(sandbox, LOCAL).check(
        ToolCall("open_pull_request", {"repo": "nightshift-fleet/example", "auto_merge": True})
    )
    assert decision.rule == "no-auto-merge"
