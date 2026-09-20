from __future__ import annotations

import logging

import pytest

from callva.livekit.core import log
from callva.livekit.webhook import errors


@pytest.fixture(autouse=True)
def collecting() -> None:
    errors.collect()
    yield
    errors.stop()


def test_nothing_collected_is_nothing_reported():
    assert errors.drain() is None


def test_an_error_anywhere_in_the_process_is_kept():
    logging.getLogger("some.plugin").error("the model refused the session")

    collected = errors.drain()

    assert len(collected) == 1
    assert collected[0]["message"] == "the model refused the session"
    assert collected[0]["logger"] == "some.plugin"


def test_a_traceback_comes_with_it():
    try:
        raise RuntimeError("no credit on the account")
    except RuntimeError:
        logging.getLogger("some.plugin").exception("session refused")

    collected = errors.drain()

    assert "no credit on the account" in collected[0]["exception"]


def test_warnings_are_not_errors():
    logging.getLogger("some.plugin").warning("slow")

    assert errors.drain() is None


def test_the_delivery_path_does_not_report_itself():
    """A failing delivery logs an error, which would be reported, which would fail..."""
    logging.getLogger("callva.livekit.webhook").error("could not deliver call.ended")

    assert errors.drain() is None


def test_the_delivery_path_is_the_only_one_of_ours_that_is_silenced():
    """Every module in this library reports the call except the one delivering the report.

    Asserted through the real loggers rather than against the prefix tuple: reading
    DENY_PREFIXES here would pass against any value it happened to hold, and the value it
    held for a year dropped everything below into a container log and nowhere else.
    """
    log.logger.error("the stack this call runs on could not be asked how it opens")
    log.delivery.error("could not deliver call.ended")

    collected = errors.drain()

    assert [e["message"] for e in collected] == [
        "the stack this call runs on could not be asked how it opens"
    ]


def test_what_this_library_logs_about_the_call_reaches_the_report():
    """The two errors the package itself raises about a call, each through its own module."""
    logging.getLogger("callva.livekit").error("configuration is unavailable; terminating the call")
    logging.getLogger("callva.livekit.internal").error("the gemini stack could not open the call")

    collected = errors.drain()

    assert len(collected) == 2
    assert {e["logger"] for e in collected} == {"callva.livekit", "callva.livekit.internal"}


def test_a_call_that_breaks_without_stopping_is_capped():
    for n in range(errors.MAX_ERRORS + 5):
        logging.getLogger("some.plugin").error("failure %s", n)

    collected = errors.drain()

    assert len(collected) == errors.MAX_ERRORS + 1
    assert collected[-1]["message"] == "and 5 more"


def test_draining_empties_the_buffer():
    logging.getLogger("some.plugin").error("once")

    assert errors.drain() is not None
    assert errors.drain() is None


def test_collecting_twice_attaches_one_handler():
    root = logging.getLogger()
    before = len(root.handlers)

    errors.collect()

    assert len(root.handlers) == before


def test_nothing_is_collected_until_asked():
    errors.stop()
    logging.getLogger("some.plugin").error("unheard")

    assert errors.drain() is None
