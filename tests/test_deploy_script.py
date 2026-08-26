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


def test_the_script_does_not_switch_off_msys_path_conversion() -> None:
    """The fix that was worse than the bug.

    Git Bash rewriting `/workspace` into `C:/Program Files/Git/workspace` is
    real, and turning the rewriting off is not the answer: gcloud on Windows is
    a Python script behind a bash wrapper that hands its own `/c/Users/...`
    path back through the same conversion, so suppressing it globally stopped
    this script at its first gcloud call — `can't open file 'C:\\c\\Users\\...'`
    — before a single API had been enabled.
    """
    text = DEPLOY.read_text(encoding="utf-8")
    offenders = [
        line
        for line in lines()
        if not line.lstrip().startswith("#")
        and ("MSYS_NO_PATHCONV" in line or "MSYS2_ARG_CONV_EXCL" in line)
    ]
    assert not offenders, "suppressing MSYS conversion breaks gcloud's own wrapper"
    assert "MSYS" in text, "the reasoning has to stay, or the next person re-adds it"


def test_the_script_passes_no_absolute_posix_path_to_gcloud() -> None:
    """Nothing to rewrite is the only defence that survives a Windows shell.

    Every value this script sends is a project id, a region, a resource name or
    a secret reference — none of which look like a path. The one that did,
    `NIGHTSHIFT_WORKSPACE_ROOT=/workspace`, now lives in the worker image.
    """
    offenders = [
        (number, line)
        for number, line in enumerate(lines(), start=1)
        if not line.lstrip().startswith("#") and re.search(r"=/[A-Za-z]", line)
    ]
    assert not offenders, (
        "a POSIX path is passed at "
        + ", ".join(f"line {n}" for n, _ in offenders)
        + " — Git Bash will rewrite it; put it in the Dockerfile instead"
    )


def test_the_script_fills_gaps_from_dotenv_without_overriding_the_environment() -> None:
    """The rule the Python side already follows, asserted for the shell side.

    A new terminal has no NIGHTSHIFT_GCP_PROJECT, so the deployment stopped
    before it started and the workaround was to paste exports — which is how a
    value ends up in shell history and eventually wrong. The file is read, but
    only into gaps: a variable that is already set outranks it, or a stale
    `.env` would quietly outvote what CI or a colleague's shell had chosen.
    """
    text = DEPLOY.read_text(encoding="utf-8")
    assert 'env_file="$(dirname "$0")/../.env"' in text
    assert '[[ -n "${!name:-}" ]] || export' in text, "a set variable must win"


def test_the_dotenv_file_is_read_and_never_executed() -> None:
    """`source .env` would run whatever a stray backtick in it said.

    It is a data file that people paste tokens into, not a script, and nothing
    about editing it suggests you are writing code that will run.
    """
    offenders = [
        (number, line)
        for number, line in enumerate(lines(), start=1)
        if not line.lstrip().startswith("#")
        and re.search(r"(^|\s)(source|\.)\s+\S*\.env", line)
    ]
    assert not offenders, "read the dotenv file line by line; never source it"


def test_every_job_states_its_own_cpu_and_memory() -> None:
    """Cloud Run's default is 512Mi, and the worker needs several times that.

    Building somebody else's Python project means pip compiling wheels, and at
    the default the worker was killed 97 seconds in — before the first
    repository had finished installing. It arrives as "the configured memory
    limit was reached" alongside exit code 0, which nobody reads as out of
    memory on the first pass.
    """
    text = DEPLOY.read_text(encoding="utf-8")
    assert '--cpu "$cpu" --memory "$memory"' in text
    for job, memory in (("scanner", "1Gi"), ("worker", "4Gi")):
        assert re.search(rf"deploy_job\s+{job}\s+\S+\s+\S+\s+\S+\s+\d+\s+{memory}", text), (
            f"{job} must state its memory; the default is not enough for what it does"
        )


def test_the_worker_is_given_more_memory_than_the_scanner() -> None:
    """One reads two small files per repository; the other builds a project from
    source. Sizing them alike means either paying for the scanner or killing the
    worker, and it was the worker."""
    text = DEPLOY.read_text(encoding="utf-8")
    sizes = {}
    for job in ("scanner", "worker"):
        match = re.search(rf"deploy_job\s+{job}\s+\S+\s+\S+\s+\S+\s+\d+\s+(\d+)Gi", text)
        assert match is not None
        sizes[job] = int(match.group(1))
    assert sizes["worker"] > sizes["scanner"]


def test_the_worker_image_declares_its_own_workspace_root() -> None:
    """The other half of the same fix: the value did not vanish, it moved."""
    dockerfile = DEPLOY.parent.parent / "services" / "worker" / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    assert "ENV NIGHTSHIFT_WORKSPACE_ROOT=/workspace" in text
    assert "mkdir -p /workspace" in text, "the root has to exist in the image that names it"
