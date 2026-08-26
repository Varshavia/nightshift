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
from nightshift_core.config import Settings, get_settings, load_env_file


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


def test_a_dashboard_needs_a_project_and_no_opinion_about_forks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control tower reads. It never forks, so it must not demand a fork org.

    The combined check made a read-only dashboard fail in production with
    "missing NIGHTSHIFT_FORK_ORG" — true, unhelpful, and about a capability the
    service does not have. The symptom was a 500 on every page.
    """
    monkeypatch.delenv("NIGHTSHIFT_FORK_ORG", raising=False)
    settings = Settings(gcp_project="nightshift-506519", fork_org="")

    settings.require_project()  # must not raise

    with pytest.raises(RuntimeError, match="NIGHTSHIFT_FORK_ORG"):
        settings.require_cloud()


def test_a_service_that_opens_pull_requests_must_know_where_they_go() -> None:
    """A worker that learns this at PR time has already spent the tokens."""
    with pytest.raises(RuntimeError, match="NIGHTSHIFT_GCP_PROJECT"):
        Settings(gcp_project="", fork_org="Varshavia").require_cloud()

    Settings(gcp_project="p", fork_org="Varshavia").require_cloud()


def test_the_model_backend_setting_actually_routes_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A setting nobody reads is a setting that does nothing.

    ``NIGHTSHIFT_MODEL_BACKEND`` was documented in ``.env.example``, defaulted to
    ``vertex``, and consumed by no line of code. ADK takes the backend from the
    process environment, so the first real repair attempt would have gone to the
    public Gemini API with no key — and the failure would have looked like the
    model refusing the work rather than like a wiring bug.
    """
    from services.worker.agent import configure_backend

    for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(name, raising=False)

    configure_backend(Settings(gcp_project="nightshift-506519", gcp_region="us-central1"))

    assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "true"
    assert os.environ["GOOGLE_CLOUD_PROJECT"] == "nightshift-506519"
    # Where the model is served, not where the fleet runs.
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "global"


def test_an_explicit_backend_choice_in_the_environment_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same rule as the dotenv reader: what is already set outranks the default.

    Someone pointing at the Gemini API for an afternoon should not have to edit
    the project to do it.
    """
    from services.worker.agent import configure_backend

    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    configure_backend(Settings(gcp_project="p", gcp_region="us-central1"))

    assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "false"


def test_a_non_vertex_backend_sets_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Choosing the Gemini API and being handed Vertex anyway is the bug this
    whole function exists to prevent, arrived at from the other side."""
    from services.worker.agent import configure_backend

    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    configure_backend(Settings(gcp_project="p", model_backend="api"))

    assert "GOOGLE_GENAI_USE_VERTEXAI" not in os.environ


def test_the_model_is_looked_for_where_it_is_served_not_where_we_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertex serves new Gemini versions on `global` before any named region.

    Passing the compute region to the SDK sent every repair attempt to
    us-central1 and came back 404: the model "was not found or your project does
    not have access to it". That sentence reads like an entitlement problem and
    was a geography one — the same model answered on `global` immediately.
    """
    from services.worker.agent import configure_backend

    for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(name, raising=False)

    configure_backend(Settings(gcp_project="p", gcp_region="europe-west4"))

    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "global"


def test_the_model_location_is_configurable_for_when_it_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`global` is today's answer, not a permanent one — models land in named
    regions later, and a project may be told to use one."""
    for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NIGHTSHIFT_MODEL_LOCATION", "us-central1")

    from services.worker.agent import configure_backend

    configure_backend(get_settings())

    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "us-central1"
