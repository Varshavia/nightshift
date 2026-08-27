"""How the fleet drives the model, and why it is driven only one way.

The fleet asked a model about a real repository exactly once. `Varshavia/f2`
was the only repository in fifty-seven to reach the upgrade with a usable
baseline and a broken suite — the whole point of the project — and the answer
came back empty. Underneath it, in the container log, was

    RuntimeError: cannot schedule new futures after interpreter shutdown

raised while the SDK was fetching its access token. Two event loops in one
long-lived process: ADK's synchronous ``Runner.run`` starts one on a thread of
its own, and the session was created in another through ``asyncio.run``. The
SDK reaches for credentials via ``asyncio.to_thread``, which needs a live
executor, and after the first job of a task there was not one.

None of that surfaced as an error. ADK logs it and yields events anyway, so it
reached the fleet as "no answer, no tokens" and was recorded as INFRA_ERROR —
an outage filed as though the agent had been asked and had nothing to say.
"""

from __future__ import annotations

import sys
import types as pytypes
from pathlib import Path
from typing import Any

import pytest

AGENT = Path(__file__).resolve().parent.parent / "services" / "worker" / "agent.py"
LIBRARIAN = Path(__file__).resolve().parent.parent / "services" / "worker" / "librarian.py"


# --------------------------------------------------------------------------- #
# The rule, asserted at the source, because it is a rule about how the SDK is
# called rather than about what it returns.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("module", [AGENT, LIBRARIAN], ids=["agent", "librarian"])
def test_nothing_uses_the_synchronous_runner_wrapper(module: Path) -> None:
    """`Runner.run` is the notebook convenience. It spawns a loop of its own,
    and a worker that handles many jobs in one process is the case it was never
    meant for."""
    text = module.read_text(encoding="utf-8")

    assert "runner.run(" not in text, "use run_async inside a single loop"


@pytest.mark.parametrize("module", [AGENT, LIBRARIAN], ids=["agent", "librarian"])
def test_only_the_shared_helper_starts_an_event_loop(module: Path) -> None:
    """One `asyncio.run` in the worker, in one function.

    The same rule implemented twice drifts apart — the reason the worker and the
    probe once disagreed about what a red baseline meant. The librarian runs in
    the same process as the repair agent and immediately after it, so a second
    copy of this logic would fail in exactly the same way and be found the same
    slow way.
    """
    text = module.read_text(encoding="utf-8")
    starts = text.count("asyncio.run(")

    assert starts == (1 if module is AGENT else 0), (
        "the model is driven through collect_events and nowhere else"
    )


def test_the_session_is_opened_in_the_same_loop_that_generates() -> None:
    """Creating the session elsewhere is what produced the second loop. The
    session service holds objects bound to the loop that made them."""
    body = AGENT.read_text(encoding="utf-8").split("def collect_events")[1].split("\ndef ")[0]

    assert "await runner.session_service.create_session" in body
    assert "async for event in runner.run_async" in body


# --------------------------------------------------------------------------- #
# The behaviour, with a fake runner, so the regression can be reproduced without
# a model, a network or the SDK installed.
# --------------------------------------------------------------------------- #


class _Sessions:
    def __init__(self) -> None:
        self.opened: list[str] = []

    async def create_session(self, **kwargs: Any) -> None:
        self.opened.append(str(kwargs.get("session_id")))


class FakeRunner:
    """An ADK runner reduced to the two calls the helper makes."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.session_service = _Sessions()
        self.prompts: list[str] = []

    async def run_async(self, **kwargs: Any) -> Any:
        message = kwargs["new_message"]
        self.prompts.append(message.parts[0].text)
        for event in self._events:
            yield event


@pytest.fixture
def _genai(monkeypatch: pytest.MonkeyPatch) -> None:
    """`google.genai.types` reduced to the two constructors used here.

    Stubbed rather than skipped: this test is about our loop, and making it
    depend on the SDK being installed would mean the one bug it guards against
    goes unwatched everywhere the SDK is absent — which is most places the suite
    runs.
    """
    if "google.genai" in sys.modules:  # pragma: no cover - the SDK is installed
        return
    genai = pytypes.ModuleType("google.genai")
    genai.types = pytypes.SimpleNamespace(  # type: ignore[attr-defined]
        Content=lambda role, parts: pytypes.SimpleNamespace(role=role, parts=parts),
        Part=lambda text: pytypes.SimpleNamespace(text=text),
    )
    google = sys.modules.setdefault("google", pytypes.ModuleType("google"))
    monkeypatch.setattr(google, "genai", genai, raising=False)
    monkeypatch.setitem(sys.modules, "google.genai", genai)


@pytest.mark.usefixtures("_genai")
def test_the_events_come_back_in_order() -> None:
    from services.worker.agent import collect_events

    runner = FakeRunner(["first", "second"])

    assert collect_events(runner, session_id="s", prompt="hello") == ["first", "second"]
    assert runner.session_service.opened == ["s"], "the session is opened exactly once"
    assert runner.prompts == ["hello"]


@pytest.mark.usefixtures("_genai")
def test_a_second_call_in_the_same_process_still_works() -> None:
    """The regression itself.

    A worker task takes job after job without restarting, and the failure was
    never on the first one. Any future change that leaves a loop or an executor
    behind will fail here rather than three hours into a fleet run, against the
    one repository that got far enough to matter.
    """
    for turn in range(3):
        runner = FakeRunner([f"turn {turn}"])
        assert collect(runner, turn) == [f"turn {turn}"]


def collect(runner: FakeRunner, turn: int) -> list[Any]:
    from services.worker.agent import collect_events

    return collect_events(runner, session_id=f"s{turn}", prompt="p")


@pytest.mark.usefixtures("_genai")
def test_a_runner_that_raises_is_reported_as_the_model_being_unreachable() -> None:
    """The distinction the whole outcome enum rests on: a model that could not
    be reached is not an agent that failed to repair."""
    from services.worker.agent import ModelUnreachable, collect_events

    class Exploding:
        """Iteration itself fails, which is where the real one failed: the
        request was already in flight when the executor was found to be gone."""

        def __aiter__(self) -> Exploding:
            return self

        async def __anext__(self) -> Any:
            raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    class Broken(FakeRunner):
        def run_async(self, **kwargs: Any) -> Exploding:
            return Exploding()

    with pytest.raises(ModelUnreachable, match="RuntimeError"):
        collect_events(Broken([]), session_id="s", prompt="p")
