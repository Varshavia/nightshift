"""Manifest parsing, tested against the shapes real repositories actually use.

The rule under test throughout: an exact pin becomes a Dependency, and anything
that is not an exact pin is skipped rather than guessed at. A scanner that
guesses produces confident nonsense.
"""

from __future__ import annotations

import pytest

from nightshift_core.manifests import (
    declared_extras,
    dependency_group_specs,
    parse_manifest,
    parse_pyproject,
    parse_requirements,
    rewrite_pin,
)

MESSY = """\
# Runtime dependencies
requests==2.19.0
Flask==1.0.2  # pinned deliberately
urllib3[secure,socks]==1.24.1
django==3.2.1 ; python_version >= "3.8"

# Not pins — must be skipped, not resolved
pyyaml>=5.1
click~=8.0
numpy
celery==5.2.*

# Not requirements at all
-r base.txt
--index-url https://example.invalid/simple
-e .
git+https://github.com/psf/requests.git@main#egg=requests
"""


def test_only_exact_pins_are_read() -> None:
    found = parse_requirements(MESSY)
    assert [(d.name, d.version) for d in found] == [
        ("requests", "2.19.0"),
        ("flask", "1.0.2"),
        ("urllib3", "1.24.1"),
        ("django", "3.2.1"),
    ]


def test_names_are_canonicalised_the_way_pypi_and_osv_expect() -> None:
    found = parse_requirements("Zope.Interface==5.4.0\nruamel_yaml==0.17.21\n")
    assert [d.name for d in found] == ["zope-interface", "ruamel-yaml"]


def test_a_wildcard_pin_is_a_range_wearing_a_pins_clothes() -> None:
    assert parse_requirements("celery==5.2.*") == []


def test_hashes_do_not_confuse_the_parser() -> None:
    text = "requests==2.19.0 --hash=sha256:deadbeef \\\n    --hash=sha256:cafebabe\n"
    found = parse_requirements(text)
    assert len(found) == 1
    assert found[0].version == "2.19.0"


def test_manifest_path_is_carried_so_the_upgrade_knows_what_to_rewrite() -> None:
    found = parse_requirements("requests==2.19.0", path="requirements/test.txt")
    assert found[0].manifest_path == "requirements/test.txt"


PYPROJECT = """\
[project]
name = "example"
dependencies = [
    "requests==2.19.0",
    "flask>=1.0",
    "urllib3[secure]==1.24.1",
]

[project.optional-dependencies]
test = ["pytest==8.0.0"]
docs = ["sphinx>=7"]

[tool.poetry.dependencies]
python = "^3.11"
jinja2 = "==3.0.0"
pandas = "^2.0"
"""


def test_pyproject_reads_project_optional_and_poetry_pins() -> None:
    found = parse_pyproject(PYPROJECT)
    assert {(d.name, d.version) for d in found} == {
        ("requests", "2.19.0"),
        ("urllib3", "1.24.1"),
        ("pytest", "8.0.0"),
        ("jinja2", "3.0.0"),
    }


def test_a_broken_toml_yields_nothing_rather_than_exploding() -> None:
    """One malformed manifest must not take down a scan of the whole fleet."""
    assert parse_pyproject("[project\nname = ") == []


def test_dispatch_is_by_filename() -> None:
    assert parse_manifest(PYPROJECT, "pyproject.toml")
    assert parse_manifest("requests==2.19.0", "requirements.txt")


# --------------------------------------------------------------------------- #
# Rewriting
# --------------------------------------------------------------------------- #


def test_rewrite_touches_the_version_and_nothing_else() -> None:
    """The upgrade must read as a one-token diff to whoever reviews the PR."""
    text = "urllib3[secure,socks]==1.24.1  # keep the extras\nrequests==2.19.0\n"
    result = rewrite_pin(text, "urllib3", "1.26.5", "requirements.txt")
    assert result == "urllib3[secure,socks]==1.26.5  # keep the extras\nrequests==2.19.0\n"


def test_rewrite_preserves_markers_and_indentation() -> None:
    text = '    "django==3.2.1 ; python_version >= \\"3.8\\"",\n'
    result = rewrite_pin(text, "django", "3.2.13", "pyproject.toml")
    assert result.startswith("    ")
    assert "3.2.13" in result
    assert 'python_version' in result


def test_rewrite_matches_regardless_of_how_the_name_was_spelled() -> None:
    result = rewrite_pin("Zope.Interface==5.4.0\n", "zope-interface", "5.5.0", "r.txt")
    assert result == "Zope.Interface==5.5.0\n"


def test_rewriting_a_package_that_is_not_there_is_an_error_not_a_no_op() -> None:
    """A silent no-op would be reported as a successful upgrade."""
    with pytest.raises(LookupError, match="not pinned"):
        rewrite_pin("requests==2.19.0\n", "flask", "2.0.0", "requirements.txt")


def test_a_comment_mentioning_the_package_is_not_a_pin() -> None:
    with pytest.raises(LookupError):
        rewrite_pin("# flask==1.0.2 was removed\nrequests==2.19.0\n", "flask", "2.0", "r.txt")


def test_round_trip_parse_rewrite_parse() -> None:
    upgraded = rewrite_pin(MESSY, "requests", "2.20.0", "requirements.txt")
    reparsed = {d.name: d.version for d in parse_requirements(upgraded)}
    assert reparsed["requests"] == "2.20.0"
    assert reparsed["flask"] == "1.0.2"


# --------------------------------------------------------------------------- #
# Where projects actually declare their test dependencies
# --------------------------------------------------------------------------- #

MODERN = """\
[project]
name = "example"
dependencies = ["requests==2.19.0"]

[project.optional-dependencies]
docs = ["sphinx"]

[dependency-groups]
tests = ["freezegun", "pytest"]
dev = ["ruff", {include-group = "tests"}]
cyclic = [{include-group = "cyclic"}]
"""


def test_declared_extras_are_read_rather_than_guessed() -> None:
    """`pip install .[test]` exits zero when the extra does not exist.

    Guessing extra names and trusting that exit code installs nothing, the suite
    then fails at import, and we record BASELINE_RED for breakage we caused.
    """
    assert declared_extras(MODERN) == ("docs",)
    assert "test" not in declared_extras(MODERN)


def test_pep_735_dependency_groups_are_found() -> None:
    """itsdangerous and loguru both keep their test deps here, not in an extra."""
    assert dependency_group_specs(MODERN, "tests") == ["freezegun", "pytest"]


def test_include_group_is_resolved() -> None:
    assert dependency_group_specs(MODERN, "dev") == ["ruff", "freezegun", "pytest"]


def test_a_cyclic_include_terminates() -> None:
    assert dependency_group_specs(MODERN, "cyclic") == []


def test_a_missing_group_is_empty_not_an_error() -> None:
    assert dependency_group_specs(MODERN, "nope") == []
    assert declared_extras("[project\nbroken") == ()
