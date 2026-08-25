"""Shape checks on the deployment script that `bash -n` cannot make.

The script is not importable and not unit-testable, and it has now produced two
failures that were valid bash and wrong anyway. These assertions are the cheap
half of the lesson: the expensive half is that the only real test of a
deployment script is running it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "infra" / "deploy.sh"


def lines() -> list[str]:
    return DEPLOY.read_text(encoding="utf-8").splitlines()


def test_no_comment_interrupts_a_continued_command() -> None:
    """A `#` line between two `\\`-continued lines silently ends the command.

    It is valid bash and it is never what anyone meant. The API deployment lost
    `--allow-unauthenticated`, `--set-env-vars` and `--set-secrets` to exactly
    this, reported "deployed and is serving 100 percent of traffic", and then
    tried to run `--allow-unauthenticated` as a command.
    """
    offenders = [
        (number, line)
        for number, (previous, line) in enumerate(zip(lines(), lines()[1:], strict=False), start=2)
        if previous.rstrip().endswith("\\") and line.strip().startswith("#")
    ]
    assert not offenders, (
        "a comment interrupts a line continuation at "
        + ", ".join(f"line {n}" for n, _ in offenders)
        + " — move it above the command"
    )


def test_every_service_account_is_granted_before_it_is_used() -> None:
    """Cloud Run refuses to deploy a job whose account cannot read its secret.

    That is how the first deployment failed, and it is a good failure: the
    platform checked something the script only asserted in a comment.
    """
    text = DEPLOY.read_text(encoding="utf-8")
    granted = text.index("grant nightshift-worker roles/secretmanager.secretAccessor")
    used = text.index("--set-secrets")
    assert granted < used, "the grant must come before the deployment that needs it"


@pytest.mark.parametrize("flag", ["--set-secrets", "--clear-secrets"])
def test_the_secret_binding_is_always_stated(flag: str) -> None:
    """`gcloud run jobs deploy` updates in place, and omitting a flag leaves the
    old value rather than removing it. A script that only ever adds is not
    idempotent however clearly its comments claim otherwise."""
    assert flag in DEPLOY.read_text(encoding="utf-8")


def test_the_fleet_never_deploys_with_upstream_prs_enabled() -> None:
    """RESPONSIBLE_USE.md's central promise, asserted where it is configured."""
    assert "ALLOW_UPSTREAM_PRS=false" in DEPLOY.read_text(encoding="utf-8")
    assert not re.search(r"ALLOW_UPSTREAM_PRS=(true|1|yes)", DEPLOY.read_text(encoding="utf-8"))


def test_the_script_disables_msys_path_conversion() -> None:
    """Git Bash rewrites POSIX-looking arguments into Windows paths.

    `NIGHTSHIFT_WORKSPACE_ROOT=/workspace` reached Cloud Run as
    `C:/Program Files/Git/workspace`. gcloud accepted it, the deployment
    reported success, and every job in the queue then failed inside a Linux
    container trying to create a directory under a drive letter.

    Nothing in bash's own syntax can catch this and nothing on Linux reproduces
    it, so the guard is that the script says so out loud.
    """
    text = DEPLOY.read_text(encoding="utf-8")
    assert "MSYS_NO_PATHCONV=1" in text
    assert 'MSYS2_ARG_CONV_EXCL="*"' in text


def test_the_workspace_root_is_an_absolute_posix_path() -> None:
    """The value that was silently rewritten. Asserted so a future edit that
    quotes it differently, or drops the leading slash to dodge the conversion,
    has to be deliberate."""
    assert "NIGHTSHIFT_WORKSPACE_ROOT=/workspace" in DEPLOY.read_text(encoding="utf-8")
