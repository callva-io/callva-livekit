from __future__ import annotations

import logging

from callva.livekit.config import render
from callva.livekit.config.models import Variables


def test_substitution_with_and_without_spaces():
    assert render("Hello {{name}} and {{ name }}", {"name": "Anna"}) == "Hello Anna and Anna"


def test_missing_placeholder_is_left_in_place_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="callva.livekit"):
        result = render("Hello {{ name }}, you are {{ unknown }}", {"name": "Anna"})

    assert result == "Hello Anna, you are {{ unknown }}"
    assert "unknown" in caplog.text


def test_types_render_the_way_their_producer_wrote_them():
    rendered = render(
        "{{ count }} {{ ratio }} {{ vip }} {{ plain }} [{{ nothing }}]",
        {"count": 3, "ratio": 1.5, "vip": True, "plain": False, "nothing": None},
    )

    assert rendered == "3 1.5 true false []"


def test_dotted_lookup():
    assert render("{{ caller.name }}", {"caller": {"name": "Anna"}}) == "Anna"


def test_dotted_lookup_that_misses_is_left_alone():
    assert render("{{ caller.age }}", {"caller": {"name": "Anna"}}) == "{{ caller.age }}"


def test_nothing_to_do():
    assert render(None, {"a": 1}) is None
    assert render("plain text", None) == "plain text"
    assert render("", {"a": 1}) == ""


def test_rendering_never_evaluates_anything():
    assert render("{{ __import__ }}", {"x": 1}) == "{{ __import__ }}"


def test_typed_accessors():
    variables = Variables({"n": 3, "f": 1.5, "b": True, "s": "yes", "empty": None})

    assert variables.get_int("n") == 3
    assert variables.get_float("f") == 1.5
    assert variables.get_bool("b") is True
    assert variables.get_bool("s") is True
    assert variables.get_str("n") == "3"

    assert variables.get_int("missing", 7) == 7
    assert variables.get_int("empty", 7) == 7
    assert variables.get_bool("b", False) is True
    assert variables.get_int("b") is None, "a boolean is not an integer here"
