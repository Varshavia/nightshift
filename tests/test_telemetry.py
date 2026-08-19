"""Telemetry, tested for the two properties it must never lose.

**It never fails a job.** Tracing is the one subsystem whose failure is
survivable, so every entry point swallows its own errors. A repository must be
repairable on a machine with no OpenTelemetry, no exporter and no cloud project.

**It is the metric, not a picture of one.** The cost curve is a query over these
attributes. If a worker could finish a job without recording `ledger.hit` or
`tokens`, that repository would silently drop out of the headline number — a
failure with no error message, which is the kind this project is least willing
to have.
"""

from __future__ import annotations

from typing import Any

import pytest

from nightshift_core import telemetry
from nightshift_core.telemetry import (
    ATTEMPT,
    LEDGER_HIT,
    OUTCOME,
    REPO,
    TOKENS,
    SpanRecorder,
    configure,
    cost_curve,
    mean_tokens_by_hit,
    span,
)


@pytest.fixture
def recorder() -> SpanRecorder:
    return configure(recorder=SpanRecorder()) or SpanRecorder()


def _job(recorder: SpanRecorder, repo: str, hit: str, tokens: int, attempts: int) -> None:
    with span("job", **{REPO: repo}) as attributes:
        attributes[LEDGER_HIT] = hit
        attributes[TOKENS] = tokens
        attributes[ATTEMPT] = attempts
        attributes[OUTCOME] = "PATCHED_REPAIRED"


# --------------------------------------------------------------------------- #
# Never fails a job
# --------------------------------------------------------------------------- #


def test_a_span_works_before_anything_is_configured() -> None:
    """A worker that starts before `configure` must still run."""
    telemetry._TRACER = None
    telemetry._RECORDER = None
    with span("job", **{REPO: "a/b"}) as attributes:
        attributes[OUTCOME] = "PATCHED_CLEAN"
    assert cost_curve() == []


def test_an_exception_inside_a_span_is_not_swallowed(recorder: SpanRecorder) -> None:
    """Telemetry hides its own failures, never the job's."""
    with pytest.raises(ValueError, match="boom"), span("job", **{REPO: "a/b"}):
        raise ValueError("boom")


def test_the_span_is_still_recorded_when_the_body_raises(recorder: SpanRecorder) -> None:
    """A job that died is exactly the one you want the trace for."""
    with pytest.raises(ValueError), span("job", **{REPO: "a/b"}) as attributes:
        attributes[OUTCOME] = "INFRA_ERROR"
        raise ValueError("boom")
    assert recorder.named("job")[0].attributes[OUTCOME] == "INFRA_ERROR"


def test_a_broken_tracer_does_not_reach_the_caller(
    recorder: SpanRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingTracer:
        def start_as_current_span(self, name: str) -> Any:
            raise RuntimeError("tracing backend is down")

    monkeypatch.setattr(telemetry, "_TRACER", ExplodingTracer())
    with span("job", **{REPO: "a/b"}):
        pass  # the point is that this does not raise


def test_configure_survives_having_no_cloud_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NIGHTSHIFT_GCP_PROJECT", raising=False)
    assert configure(recorder=SpanRecorder()) is not None


# --------------------------------------------------------------------------- #
# Attributes are the query surface
# --------------------------------------------------------------------------- #


def test_attributes_set_at_the_end_land_on_the_same_span(recorder: SpanRecorder) -> None:
    """The outcome and the token total are only known when the job finishes."""
    _job(recorder, "a/b", "exact", 2_000, 1)
    attributes = recorder.named("job")[0].attributes
    assert attributes[REPO] == "a/b"
    assert attributes[LEDGER_HIT] == "exact"
    assert attributes[OUTCOME] == "PATCHED_REPAIRED"


def test_none_valued_attributes_are_dropped_rather_than_recorded(
    recorder: SpanRecorder,
) -> None:
    """`ledger.hit=None` in a trace reads as a tier, not as absence."""
    with span("job", **{REPO: "a/b", LEDGER_HIT: None}) as attributes:
        attributes[OUTCOME] = "PATCHED_CLEAN"
    assert LEDGER_HIT not in recorder.named("job")[0].attributes


def test_spans_nest_the_way_the_job_does(recorder: SpanRecorder) -> None:
    with span("job", **{REPO: "a/b"}) as job:
        with (
            span("phase", phase="REPAIR"),
            span("agent.turn", **{ATTEMPT: 1}),
            span("tool.call", tool="write_file"),
        ):
            pass
        job[OUTCOME] = "PATCHED_REPAIRED"
    assert [s.name for s in recorder.spans] == ["tool.call", "agent.turn", "phase", "job"]


# --------------------------------------------------------------------------- #
# The curve
# --------------------------------------------------------------------------- #


def test_the_curve_is_one_row_per_finished_job(recorder: SpanRecorder) -> None:
    _job(recorder, "org/first", "miss", 40_000, 4)
    _job(recorder, "org/second", "exact", 2_000, 1)
    rows = cost_curve(recorder)
    assert [r["repo"] for r in rows] == ["org/first", "org/second"]
    assert [r["ledger_hit"] for r in rows] == ["miss", "exact"]


def test_an_unfinished_job_is_not_in_the_curve(recorder: SpanRecorder) -> None:
    """A job with no outcome has not paid its full cost yet; including it would
    understate the tier it belongs to."""
    with span("job", **{REPO: "org/inflight", LEDGER_HIT: "exact"}):
        pass
    assert cost_curve(recorder) == []


def test_only_job_spans_are_in_the_curve(recorder: SpanRecorder) -> None:
    with span("tool.call", **{OUTCOME: "whatever"}):
        pass
    assert cost_curve(recorder) == []


def test_the_claim_of_the_whole_project_in_one_query(recorder: SpanRecorder) -> None:
    """A miss pays full price; a hit does not. This is the demo, asserted."""
    _job(recorder, "org/1", "miss", 40_000, 4)
    _job(recorder, "org/2", "near", 12_000, 2)
    _job(recorder, "org/3", "exact", 2_000, 1)
    _job(recorder, "org/4", "exact", 3_000, 1)

    means = mean_tokens_by_hit(recorder)
    assert means["miss"] == 40_000
    assert means["exact"] == 2_500
    assert means["exact"] < means["near"] < means["miss"]


def test_a_tier_nobody_hit_is_absent_rather_than_zero(recorder: SpanRecorder) -> None:
    """`exact: 0` would read as free, which is the opposite of "never happened"."""
    _job(recorder, "org/1", "miss", 40_000, 4)
    assert "exact" not in mean_tokens_by_hit(recorder)


def test_the_curve_of_an_unconfigured_process_is_empty_not_an_error() -> None:
    telemetry._RECORDER = None
    assert cost_curve() == []
    assert mean_tokens_by_hit() == {}
