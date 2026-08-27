"""Opening the pull request. Forks by default; nothing merges itself."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from services.worker.pull_request import IDENTITY, PullRequestBlocked, open_pr
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
    # No `git config user.*`. The fixture used to write an identity into the
    # clone and so tested a repository that had one — which is the single thing
    # a freshly cloned repository in a container does not have. The suite was
    # green while every pull request the fleet earned died on "Author identity
    # unknown", fifty-one of them in one night.
    #
    # The developer's own global config would put the identity back, so it is
    # pointed at a file that does not exist. What is left is what the worker
    # actually gets.
    absent = root.parent / "there-is-no-git-config-here"
    bare = {"GIT_CONFIG_GLOBAL": str(absent), "GIT_CONFIG_SYSTEM": str(absent)}
    env = {**os.environ, **bare}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True, env=env)
    (root / "app.py").write_text("from jinja2 import Markup\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", *IDENTITY, "commit", "-qm", "initial"],
        cwd=root, check=True, capture_output=True, env=env,
    )
    (root / "app.py").write_text("from markupsafe import Markup\n", encoding="utf-8")
    sandbox = Sandbox(repo_path=root, python=Path("/usr/bin/python3"))
    sandbox.env.update(bare)
    return sandbox


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


def test_the_commit_is_authored_without_any_identity_being_configured(
    sandbox: Sandbox,
) -> None:
    """The line that lost a night's work.

    `git commit` refuses to run without an author, and a container has no
    identity to offer it. Fifty-one repositories were cloned, built, measured,
    upgraded and tested — all of it paid for — and then died here, one command
    short of the pull request they had earned. The fleet reported them as
    infrastructure errors and put them back on the queue to do it all again.

    The clone this runs against has no configured identity and cannot see one,
    so the only place the author can come from is the command itself.
    """
    open_pr(
        make_job(), sandbox, engine_for(sandbox, LOCAL), LOCAL, FakeGitHub(),
        baseline_green=True, model="gemini-3.5-flash",
    )
    author = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"], cwd=sandbox.repo_path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert author == "Nightshift <nightshift@users.noreply.github.com>"


def test_the_identity_never_touches_the_repository_it_commits_to(
    sandbox: Sandbox,
) -> None:
    """A worker builds many clones in one container. An identity written into a
    clone is state the next repository inherits without asking, and the reason
    for `-c` over `git config`."""
    open_pr(
        make_job(), sandbox, engine_for(sandbox, LOCAL), LOCAL, FakeGitHub(),
        baseline_green=True, model="gemini-3.5-flash",
    )
    configured = subprocess.run(
        ["git", "config", "--local", "--get", "user.email"],
        cwd=sandbox.repo_path, capture_output=True, text=True, check=False,
    )

    assert configured.returncode != 0, "the clone must be left as it was found"


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
