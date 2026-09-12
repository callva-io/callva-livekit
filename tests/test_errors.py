from __future__ import annotations

import logging

import pytest

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


def test_our_own_errors_are_not_collected():
    """A failing delivery logs an error, which would be reported, which would fail..."""
    logging.getLogger("callva.livekit.webhook").error("could not deliver call.ended")

    assert errors.drain() is None


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
