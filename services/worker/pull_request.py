"""Rendering and opening the pull request.

The body is rendered by a pure function so that a formatting mistake is caught
by a unit test rather than by a maintainer reading a broken pull request. The
AI-authorship disclosure lives in the template and is asserted in the tests — it
is not something a future refactor gets to drop quietly.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Protocol

from nightshift_core.config import Settings
from nightshift_core.models import RepoJob
from nightshift_core.policy import Decision, PolicyEngine, ToolCall
from services.worker.toolchain import Sandbox, diff_stats

__all__ = [
    "IDENTITY",
    "PR_TEMPLATE_PATH",
    "GitHubClient",
    "PullRequestBlocked",
    "PyGithubClient",
    "open_pr",
    "open_pull_for",
    "rejected_as_behind",
    "render_pr_body",
]

log = logging.getLogger("nightshift.pr")

GIT_TIMEOUT = 300

PR_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "pr_body.md"

#: How much of the failing output goes into the body. The tail carries the
#: traceback; the head is collection noise nobody needs in a pull request.
EXCERPT_CHARS = 2000


def render_pr_body(
    job: RepoJob,
    *,
    baseline_green: bool,
    test_command: str,
    model: str,
    max_attempts: int | None = None,
    template: str | None = None,
) -> str:
    """Fill ``templates/pr_body.md`` from a finished job."""
    text = template if template is not None else PR_TEMPLATE_PATH.read_text(encoding="utf-8")

    vulnerability = job.vulnerabilities[0]
    attempts = job.repair_attempts
    last = attempts[-1] if attempts else None
    diff = last.diff if last else ""
    stats = diff_stats(diff)
    excerpt = (last.failing_output if last else "")[-EXCERPT_CHARS:]
    cve = vulnerability.cve
    run_id, _, _ = job.job_id.partition(":")

    return text.format(
        package=vulnerability.package,
        from_version=vulnerability.installed_version,
        to_version=vulnerability.fixed_version or "unknown",
        advisory_id=vulnerability.osv_id,
        cve_suffix=f" ({cve})" if cve else "",
        severity=str(vulnerability.severity),
        advisory_summary=vulnerability.summary or "No summary published.",
        failing_test_count=stats.files,
        failing_excerpt=excerpt,
        repair_explanation=last.rationale if last else "",
        changed_file_count=stats.files,
        added_lines=stats.added,
        removed_lines=stats.removed,
        repair_diff=diff,
        baseline_status="passing" if baseline_green else "failing",
        final_status="passing",
        attempts=len(attempts),
        max_attempts=max_attempts if max_attempts is not None else len(attempts),
        test_command=test_command,
        run_id=run_id,
        job_id=job.job_id,
        model=model,
    )


# --------------------------------------------------------------------------- #
# Opening it
# --------------------------------------------------------------------------- #


class PullRequestBlocked(RuntimeError):
    """The policy engine refused to open this pull request."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(f"[{decision.rule}] {decision.reason}")
        self.decision = decision


class GitHubClient(Protocol):
    """What opening a pull request needs, and nothing more."""

    def create_pull_request(self, repo: str, head: str, title: str, body: str) -> str: ...


class PyGithubClient:
    """The real client. Constructed lazily so importing this module needs no token."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._github: Any | None = None

    @property
    def github(self) -> Any:
        if self._github is None:
            from github import Auth, Github

            self._github = Github(auth=Auth.Token(self._token))
        return self._github

    def create_pull_request(self, repo: str, head: str, title: str, body: str) -> str:
        repository = self.github.get_repo(repo)
        try:
            pull = repository.create_pull(
                title=title, body=body, head=head, base=repository.default_branch
            )
        except Exception:
            # GitHub refuses a second pull request for the same head, and a job
            # that is redelivered arrives at exactly that. The pull request it
            # opened the first time is the answer to this job, not an error: the
            # work is done and the url exists. Raising instead would put the job
            # back on the queue to fail the same way for ever.
            existing = open_pull_for(repository, head)
            if existing is None:
                raise
            log.info("a pull request for %s is already open; keeping it", head)
            return existing
        return str(pull.html_url)


#: git's several ways of saying "the remote has something you do not".
_REJECTED = re.compile(r"fetch first|non-fast-forward|Updates were rejected", re.IGNORECASE)


def rejected_as_behind(stderr: str) -> bool:
    """Was the push refused because the branch is already on the remote?"""
    return bool(_REJECTED.search(stderr))


def open_pull_for(repository: Any, head: str) -> str | None:
    """The url of the open pull request for ``head``, if there is one."""
    owner = str(repository.owner.login)
    for pull in repository.get_pulls(state="open", head=f"{owner}:{head}"):
        return str(pull.html_url)
    return None


#: Who the fleet commits as, supplied on the command rather than written into
#: the clone or the image.
#:
#: `git commit` refuses to run without an identity, and a container has none:
#: fifty-one repositories in one night were cloned, built, measured, upgraded,
#: tested and then lost on this line — "Author identity unknown" — after all the
#: expensive work was already done. Every one of them was a pull request the
#: fleet had earned.
#:
#: `-c` rather than `git config`, because a worker builds many clones in one
#: container and a configured identity is shared state the next repository
#: inherits without asking. Rather than the image, because the image is not
#: where anyone looks when a commit has no author, and a test cannot assert it.
IDENTITY = (
    "-c",
    "user.name=Nightshift",
    "-c",
    "user.email=nightshift@users.noreply.github.com",
)


def open_pr(
    job: RepoJob,
    sandbox: Sandbox,
    policy: PolicyEngine,
    settings: Settings,
    client: GitHubClient,
    *,
    baseline_green: bool,
    model: str,
    test_command: str = "pytest -q",
) -> str:
    """Branch, commit, push and open. Returns the pull request url.

    The policy check happens *before* anything is branched or committed: a
    denial must not leave a branch behind that no pull request will ever
    reference.
    """
    vulnerability = job.vulnerabilities[0]
    decision = policy.check(
        ToolCall("open_pull_request", {"repo": job.repo, "auto_merge": False})
    )
    if not decision.allowed:
        raise PullRequestBlocked(decision)

    run_id, _, _ = job.job_id.partition(":")
    branch = f"nightshift/{vulnerability.package}-{vulnerability.fixed_version}-{run_id}"
    title = (
        f"Security: {vulnerability.package} "
        f"{vulnerability.installed_version} → {vulnerability.fixed_version}"
    )
    body = render_pr_body(
        job,
        baseline_green=baseline_green,
        test_command=test_command,
        model=model,
        max_attempts=settings.ceilings.max_repair_attempts,
    )

    for argv in (
        ["git", "checkout", "-b", branch],
        ["git", "add", "-A"],
        [
            "git",
            *IDENTITY,
            "commit",
            "-m",
            title,
            "-m",
            "Opened by Nightshift, an autonomous agent fleet.",
        ],
    ):
        result = sandbox.run(argv, timeout=GIT_TIMEOUT)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(argv)} failed: {result.stderr[-500:]}")

    if settings.github_token:
        pushed = sandbox.run(["git", "push", "origin", branch], timeout=GIT_TIMEOUT)
        if pushed.returncode != 0 and rejected_as_behind(pushed.stderr):
            # The branch is ours by construction: our prefix, the package, the
            # fixed version and this run's id. Nobody else writes that name, so
            # what is on the remote is an earlier attempt at this same job that
            # pushed and then died before it could record an outcome — four of
            # them in one night, each one blocking the retry that would have
            # finished the work.
            #
            # Not `main`, and not a branch anyone is working on. Replacing our
            # own leftover is the only way a redelivered job can ever finish.
            log.warning("%s is already on the remote; replacing our earlier attempt", branch)
            pushed = sandbox.run(
                ["git", "push", "--force", "origin", branch], timeout=GIT_TIMEOUT
            )
        if pushed.returncode != 0:
            raise RuntimeError(f"push failed: {pushed.stderr[-500:]}")
    else:
        # The local-development path. `make run-local` against a scratch clone
        # has no remote to push to, and failing there would make the loop
        # untestable without a credential.
        log.warning("no GITHUB_TOKEN; branch %s committed but not pushed", branch)

    url = client.create_pull_request(repo=job.repo, head=branch, title=title, body=body)
    log.info("job %s opened %s", job.job_id, url)
    return url
