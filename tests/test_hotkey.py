"""Trigger logic, fed synthetic key events (no /dev/input needed)."""

import os
import struct
import time

import pytest

from utter import hotkey
from utter.hotkey import (
    EVENT_FORMAT,
    EV_KEY,
    ChordListener,
    DoubleTapListener,
    HoldListener,
    resolve_key,
    types_a_character,
)

SPACE, ALT, ALTGR, A, RCTRL = 57, 56, 100, 30, 97
DOWN, UP, REPEAT = 1, 0, 2


def tap(listener, code):
    listener._on_event(code, DOWN)
    listener._on_event(code, UP)


def test_resolve_key():
    assert resolve_key("space") == SPACE
    assert resolve_key(" RightCtrl ") == RCTRL
    assert resolve_key("183") == 183
    assert resolve_key("NOPE") is None


@pytest.mark.parametrize("key, types", [("SPACE", True), ("TAB", True), ("RIGHTCTRL", False),
                                        ("scrolllock", False), ("F13", False)])
def test_types_a_character(key, types):
    assert types_a_character(key) is types


class TestDoubleTap:
    def make(self, **kw):
        hits = []
        listener = DoubleTapListener("SPACE", on_trigger=lambda: hits.append(1), **kw)
        return listener, hits

    def test_two_quick_taps_fire_once(self):
        listener, hits = self.make()
        tap(listener, SPACE)
        tap(listener, SPACE)
        assert hits == [1]

    def test_a_third_tap_starts_over(self):
        listener, hits = self.make()
        for _ in range(3):
            tap(listener, SPACE)
        assert hits == [1]
        tap(listener, SPACE)
        assert hits == [1, 1]

    def test_autorepeat_is_not_a_second_tap(self):
        listener, hits = self.make()
        listener._on_event(SPACE, DOWN)
        listener._on_event(SPACE, REPEAT)
        listener._on_event(SPACE, REPEAT)
        assert hits == []

    def test_double_space_while_typing_is_ignored(self):
        listener, hits = self.make(guard_ms=500)
        tap(listener, A)
        tap(listener, SPACE)
        tap(listener, SPACE)
        assert hits == []

    def test_slow_taps_do_not_fire(self):
        listener, hits = self.make(window_ms=20)
        tap(listener, SPACE)
        time.sleep(0.05)
        tap(listener, SPACE)
        assert hits == []


class TestChord:
    def test_alt_space_fires(self):
        hits = []
        listener = ChordListener("SPACE", ["ALT"], on_trigger=lambda: hits.append(1))
        listener._on_event(ALT, DOWN)
        listener._on_event(SPACE, DOWN)
        assert hits == [1]

    def test_space_alone_or_after_release_does_not(self):
        hits = []
        listener = ChordListener("SPACE", ["ALT"], on_trigger=lambda: hits.append(1))
        tap(listener, SPACE)
        listener._on_event(ALT, DOWN)
        listener._on_event(ALT, UP)
        tap(listener, SPACE)
        assert hits == []

    def test_alt_means_left_alt_only(self):
        hits = []
        listener = ChordListener("SPACE", ["ALT"], on_trigger=lambda: hits.append(1))
        listener._on_event(ALTGR, DOWN)
        listener._on_event(SPACE, DOWN)
        assert hits == []

    def test_unknown_modifiers_are_reported(self):
        listener = ChordListener("SPACE", ["ALT", "hyper"])
        assert listener.unknown_modifiers == ["HYPER"]
        assert listener.describe() == "ALT+HYPER+SPACE"


class TestHold:
    def test_press_opens_after_threshold_and_release_commits(self):
        events = []
        listener = HoldListener(
            "RIGHTCTRL",
            hold_ms=10,
            on_press=lambda: events.append("press"),
            on_release=lambda held: events.append(("release", held >= 0.01)),
        )
        listener._on_event(RCTRL, DOWN)
        deadline = time.monotonic() + 2
        while "press" not in events and time.monotonic() < deadline:
            time.sleep(0.01)
        listener._on_event(RCTRL, REPEAT)
        listener._on_event(RCTRL, UP)
        assert events == ["press", ("release", True)]

    def test_a_brief_brush_does_nothing(self):
        events = []
        listener = HoldListener(
            "RIGHTCTRL",
            hold_ms=300,
            on_press=lambda: events.append("press"),
            on_release=lambda _held: events.append("release"),
        )
        tap(listener, RCTRL)
        time.sleep(0.4)
        assert events == []


def test_drain_parses_kernel_input_events():
    """The binary struct input_event path, through a real file descriptor."""
    hits = []
    listener = ChordListener("SPACE", ["ALT"], on_trigger=lambda: hits.append(1))
    r, w = os.pipe()
    try:
        for etype, code, value in [(EV_KEY, ALT, DOWN), (0, 0, 0), (EV_KEY, SPACE, DOWN)]:
            os.write(w, struct.pack(EVENT_FORMAT, 0, 0, etype, code, value))
        os.set_blocking(r, False)
        assert listener._drain(r) is None
    finally:
        os.close(r)
        os.close(w)
    assert hits == [1]


def test_no_readable_keyboard_is_explained(monkeypatch):
    monkeypatch.setattr(hotkey, "keyboards", lambda _code: [])
    listener = DoubleTapListener("SPACE")
    assert listener.start() is False
    assert "input" in listener.error
    assert DoubleTapListener("NOPE").start() is False
