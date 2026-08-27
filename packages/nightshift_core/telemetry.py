"""OpenTelemetry spans. The telemetry *is* the metric, not a picture of one.

The headline number of this project is a curve: cost per repository falling as
the Ledger fills. That curve is a query over span attributes in Cloud Trace, not
a spreadsheet somebody kept alongside the run. A judge can open repository #12's
trace and read ``ledger.hit=exact`` next to a one-turn repair. If the numbers in
the write-up and the numbers in the traces could disagree, we would have built
an illustration instead of a measurement.

The span tree mirrors the job:

    job ─▶ phase ─▶ agent.turn ─▶ tool.call

**Telemetry never fails a job.** Every entry point here swallows its own errors.
A tracing outage, a missing exporter, or OpenTelemetry not being installed at all
degrades the fleet to running blind — it must never stop a repository being
repaired. That is why the module works with no SDK present and why
:func:`configure` is optional.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AGENT_NAME",
    "AGENT_VERSION",
    "ATTEMPT",
    "JOB_ID",
    "LEDGER_HIT",
    "OUTCOME",
    "POLICY_RULE",
    "REPO",
    "TOKENS",
    "SpanRecorder",
    "configure",
    "cost_curve",
    "record",
    "span",
]

log = logging.getLogger("nightshift.telemetry")

# --------------------------------------------------------------------------- #
# Attribute names
#
# Constants rather than string literals at call sites, because these names are
# the query surface. A typo in one worker would not fail anything — it would
# quietly drop that repository out of the curve, which is worse.
# --------------------------------------------------------------------------- #

JOB_ID = "nightshift.job_id"
REPO = "nightshift.repo"
AGENT_NAME = "agent.name"
AGENT_VERSION = "agent.version"
LEDGER_HIT = "ledger.hit"
POLICY_RULE = "policy.rule"
TOKENS = "tokens"
OUTCOME = "outcome"
ATTEMPT = "attempt"

_TRACER: Any | None = None
_RECORDER: SpanRecorder | None = None
#: OpenTelemetry permits one provider per process and warns on every later
#: attempt. Configure is idempotent so a worker may call it defensively.
_PROVIDER_SET = False


@dataclass(frozen=True, slots=True)
class FinishedSpan:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpanRecorder:
    """In-process record of every span, for tests and for ``make run-local``.

    Cloud Trace is the production surface, but a curve you can only read in a
    console is a curve nobody checks in CI. This gives the same series locally,
    computed from the same attributes, so the query in the write-up can be
    asserted rather than trusted.
    """

    spans: list[FinishedSpan] = field(default_factory=list)

    def add(self, name: str, attributes: Mapping[str, Any]) -> None:
        self.spans.append(FinishedSpan(name=name, attributes=dict(attributes)))

    def named(self, name: str) -> list[FinishedSpan]:
        return [s for s in self.spans if s.name == name]

    def attribute(self, key: str) -> list[Any]:
        return [s.attributes[key] for s in self.spans if key in s.attributes]

    def clear(self) -> None:
        self.spans.clear()


def configure(
    *, service_name: str = "nightshift", recorder: SpanRecorder | None = None
) -> SpanRecorder | None:
    """Set up tracing. Safe to call more than once, safe never to call.

    When ``NIGHTSHIFT_GCP_PROJECT`` is set and the Cloud Trace exporter is
    installed, spans go to Cloud Trace. Otherwise they go nowhere, and a
    :class:`SpanRecorder` keeps them in process so the local run still produces
    the curve.
    """
    global _TRACER, _RECORDER
    _RECORDER = recorder if recorder is not None else SpanRecorder()

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError:
        log.info("opentelemetry not installed; recording spans in process only")
        _TRACER = None
        return _RECORDER

    global _PROVIDER_SET
    try:
        if not _PROVIDER_SET:
            provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
            project = os.environ.get("NIGHTSHIFT_GCP_PROJECT", "").strip()
            if project:
                _install_cloud_exporter(provider, project)
            trace.set_tracer_provider(provider)
            _PROVIDER_SET = True
        _TRACER = trace.get_tracer("nightshift")
    except Exception:  # pragma: no cover - defensive
        # Blind is survivable. Refusing to run is not.
        log.warning("tracing setup failed; continuing without it", exc_info=True)
        _TRACER = None
    return _RECORDER


def _install_cloud_exporter(provider: Any, project: str) -> None:
    try:
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.info("cloud trace exporter not installed; spans stay in process")
        return
    exporter = CloudTraceSpanExporter(project_id=project)  # type: ignore[no-untyped-call]
    provider.add_span_processor(BatchSpanProcessor(exporter))


def recorder() -> SpanRecorder | None:
    """The in-process record, if tracing was configured."""
    return _RECORDER


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[dict[str, Any]]:
    """Open a span. Yields a dict that later code can add attributes to.

    Yielding the attribute dict rather than the OpenTelemetry span keeps call
    sites free of the SDK: a worker sets ``attrs[OUTCOME] = ...`` without
    importing anything, and the same code runs whether or not tracing exists.

    Attributes are written to the real span on exit, so a value that is only
    known at the end — the outcome, the token total — lands on the same span as
    the ones known at the start.

    The tracer is entered and exited around the yield rather than wrapping it in
    a ``try``. Wrapping would put the caller's exception inside telemetry's own
    error handling, and this module is allowed to hide its failures, never the
    job's. Failing to *open* a span degrades to running untraced; failing inside
    the body propagates untouched.
    """
    live: dict[str, Any] = {k: v for k, v in attributes.items() if v is not None}

    manager: Any = None
    otel_span: Any = None
    if _TRACER is not None:
        try:
            manager = _TRACER.start_as_current_span(name)
            otel_span = manager.__enter__()
        except Exception:
            log.warning("could not open span %s; continuing untraced", name, exc_info=True)
            manager = otel_span = None

    error: BaseException | None = None
    try:
        yield live
    except BaseException as exc:
        # Held so it can be attached to the span, then re-raised untouched. A job
        # that died is exactly the one somebody will open the trace for.
        error = exc
        raise
    finally:
        if otel_span is not None:
            _finish(otel_span, live)
        if manager is not None:
            try:
                manager.__exit__(
                    type(error) if error else None,
                    error,
                    error.__traceback__ if error else None,
                )
            except Exception:  # pragma: no cover - defensive
                log.warning("could not close span %s", name, exc_info=True)
        _record(name, live)


def _finish(otel_span: Any, attributes: Mapping[str, Any]) -> None:
    try:
        for key, value in attributes.items():
            if value is not None:
                otel_span.set_attribute(key, value)
    except Exception:  # pragma: no cover - defensive
        log.warning("could not set span attributes", exc_info=True)


def _record(name: str, attributes: Mapping[str, Any]) -> None:
    if _RECORDER is not None:
        _RECORDER.add(name, attributes)


def record(**attributes: Any) -> None:
    """Add attributes to the span currently open, if there is one."""
    if _TRACER is None:
        return
    try:
        from opentelemetry import trace

        current = trace.get_current_span()
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
    except Exception:  # pragma: no cover - defensive
        log.warning("could not record attributes", exc_info=True)


# --------------------------------------------------------------------------- #
# The curve
# --------------------------------------------------------------------------- #


def cost_curve(spans: SpanRecorder | None = None) -> list[dict[str, Any]]:
    """One row per finished job: ``ledger.hit`` against what it cost.

    This is the query, written once. In production the same shape is a Cloud
    Trace filter over ``name="job"``; here it runs over recorded spans so the
    number in the write-up can be asserted in CI rather than transcribed by hand.
    """
    source = spans if spans is not None else _RECORDER
    if source is None:
        return []
    rows: list[dict[str, Any]] = []
    for finished in source.named("job"):
        attributes = finished.attributes
        if OUTCOME not in attributes:
            continue
        rows.append(
            {
                "repo": attributes.get(REPO, ""),
                "ledger_hit": attributes.get(LEDGER_HIT, "miss"),
                "tokens": int(attributes.get(TOKENS, 0)),
                "attempts": int(attributes.get(ATTEMPT, 0)),
                "outcome": attributes.get(OUTCOME, ""),
            }
        )
    return rows


def mean_tokens_by_hit(spans: SpanRecorder | None = None) -> dict[str, float]:
    """Average job cost per retrieval tier. The claim, in one number each.

    An empty tier is absent rather than zero: reporting ``exact: 0`` for a run
    where the Ledger was never hit would read as "free", which is the opposite
    of what it means.
    """
    totals: dict[str, list[int]] = {}
    for row in cost_curve(spans):
        totals.setdefault(str(row["ledger_hit"]), []).append(int(row["tokens"]))
    return {hit: sum(values) / len(values) for hit, values in totals.items() if values}
