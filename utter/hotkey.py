"""Global key detection by reading evdev directly.

Wayland deliberately has no global key listener -- a client cannot ask the compositor
"tell me when space is pressed anywhere". So for a trigger like double-tapping space we
read the kernel's input devices instead, which sits below the compositor entirely.

Two consequences worth being explicit about:

  * This is a PASSIVE read, not a grab. The keys still reach whatever you are typing
    into, so a double-tap of space also inserts two spaces. The daemon removes them
    afterwards (see `trigger_backspace`). A grab would avoid that but would also mean
    intercepting every keystroke on the system, which is a much bigger thing to be.
  * Synthesised keystrokes do NOT appear here. wtype speaks the Wayland
    virtual-keyboard protocol rather than creating a kernel device, so utter typing its
    own output can never retrigger itself. That is a happy accident of the design, and
    the reason no echo-suppression logic is needed.

Reading input devices usually requires the `input` group, but systemd-logind grants the
active seat's user an ACL on local keyboards, so it often works with no setup at all.
`utter keys` reports which devices are actually readable.
"""

from __future__ import annotations

import glob
import os
import select
import struct
import threading
import time
from collections.abc import Callable

# struct input_event on 64-bit Linux: struct timeval (2x long), __u16, __u16, __s32
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

EV_KEY = 0x01
PRESS = 1

# Common trigger keys, by Linux keycode. Names match `linux/input-event-codes.h`.
KEY_CODES = {
    "SPACE": 57,
    "LEFTCTRL": 29,
    "RIGHTCTRL": 97,
    "LEFTALT": 56,
    "RIGHTALT": 100,
    "LEFTSHIFT": 42,
    "RIGHTSHIFT": 54,
    "CAPSLOCK": 58,
    "SCROLLLOCK": 70,
    "PAUSE": 119,
    "F13": 183,
    "F14": 184,
    "F15": 185,
    "F16": 186,
    "INSERT": 110,
    "MENU": 127,
}

_EVIOCGNAME = 0x80FF4506  # EVIOCGNAME(255)
RESCAN_SECS = 3.0


def _device_name(path: str) -> str | None:
    import fcntl

    buf = bytearray(255)
    try:
        with open(path, "rb") as fh:
            fcntl.ioctl(fh, _EVIOCGNAME, buf)
    except (OSError, PermissionError):
        return None
    return buf.split(b"\x00")[0].decode("utf-8", "replace")


def _reports_key(path: str, code: int) -> bool:
    """True if the device advertises the given key in its EV_KEY bitmap."""
    import fcntl

    buf = bytearray(96)  # enough for KEY_MAX/8
    request = 0x80000000 | (len(buf) << 16) | (ord("E") << 8) | (0x20 + EV_KEY)
    try:
        with open(path, "rb") as fh:
            fcntl.ioctl(fh, request, buf)
    except (OSError, PermissionError):
        return False
    index, bit = code // 8, code % 8
    return index < len(buf) and bool(buf[index] & (1 << bit))


def keyboards(code: int) -> list[tuple[str, str]]:
    """Readable devices that report `code`. Returns (path, name) pairs."""
    found: list[tuple[str, str]] = []
    for path in sorted(
        glob.glob("/dev/input/event*"),
        key=lambda p: int(p.rsplit("event", 1)[1]) if p.rsplit("event", 1)[1].isdigit() else 0,
    ):
        if not os.access(path, os.R_OK):
            continue
        name = _device_name(path)
        if name is None:
            continue
        if _reports_key(path, code):
            found.append((path, name))
    return found


def resolve_key(key: str) -> int | None:
    key = key.strip().upper()
    if key.isdigit():
        return int(key)
    return KEY_CODES.get(key)


# Keys that insert no character, so holding one is safe for push-to-talk.
INERT_KEYS = {"SCROLLLOCK", "PAUSE", "F13", "F14", "F15", "F16", "MENU",
              "LEFTCTRL", "RIGHTCTRL", "LEFTALT", "RIGHTALT"}


def types_a_character(key: str) -> bool:
    """True if holding this key would insert text into the focused window."""
    return key.strip().upper() not in INERT_KEYS


class _Listener:
    """Shared evdev plumbing: find keyboards, poll them, hand off key events."""

    def __init__(self, key: str) -> None:
        self.key = key.strip().upper()
        self.code = resolve_key(key)
        self.devices: list[tuple[str, str]] = []
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _on_key(self, value: int) -> None:
        raise NotImplementedError

    def _on_other_key(self) -> None:
        """Any key other than the trigger was pressed."""
        return None

    def available(self) -> bool:
        if self.code is None:
            self.error = "unknown trigger key"
            return False
        self.devices = keyboards(self.code)
        if not self.devices:
            self.error = (
                "no readable keyboard device. Add yourself to the 'input' group "
                "(sudo usermod -aG input $USER) and log back in."
            )
            return False
        return True

    def start(self) -> bool:
        if not self.available():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _loop(self) -> None:
        handles: dict[int, str] = {}
        last_scan = 0.0
        try:
            while not self._stop.is_set():
                # Rescan periodically so a keyboard plugged in later starts working,
                # and one unplugged stops being polled.
                if time.monotonic() - last_scan > RESCAN_SECS:
                    last_scan = time.monotonic()
                    self._rescan(handles)
                if not handles:
                    self._stop.wait(0.5)
                    continue
                try:
                    ready, _, _ = select.select(list(handles), [], [], 0.25)
                except OSError:
                    self._rescan(handles, force=True)
                    continue
                for fd in ready:
                    if self._drain(fd) is False:
                        self._close(handles, fd)
        finally:
            for fd in list(handles):
                self._close(handles, fd)

    def _rescan(self, handles: dict[int, str], force: bool = False) -> None:
        wanted = {path for path, _name in keyboards(self.code)}
        if force:
            for fd in list(handles):
                self._close(handles, fd)
        have = set(handles.values())
        for path in wanted - have:
            try:
                handles[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
            except OSError:
                continue
        for fd, path in list(handles.items()):
            if path not in wanted:
                self._close(handles, fd)
        self.devices = [(p, _device_name(p) or "?") for p in sorted(wanted)]

    @staticmethod
    def _close(handles: dict[int, str], fd: int) -> None:
        handles.pop(fd, None)
        try:
            os.close(fd)
        except OSError:
            pass

    def _drain(self, fd: int) -> bool | None:
        """Read pending events. Returns False if the device went away."""
        try:
            data = os.read(fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return None
        except OSError:
            return False  # device unplugged
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _sec, _usec, etype, code, value = struct.unpack_from(
                EVENT_FORMAT, data, offset
            )
            if etype != EV_KEY:
                continue
            if code == self.code:
                self._on_key(value)
            elif value == PRESS:
                self._on_other_key()
        return None


class DoubleTapListener(_Listener):
    """Fires once when `key` is pressed twice within `window_ms`.

    Ignores autorepeat, so holding the key does nothing.
    """

    def __init__(
        self,
        key: str = "SPACE",
        window_ms: int = 320,
        guard_ms: int = 500,
        on_trigger: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(key)
        self.window = window_ms / 1000.0
        # Double-spacing is something people genuinely type. Requiring a quiet moment
        # before the first tap is what separates "I want to dictate" from "I am in the
        # middle of a sentence" -- while typing, some other key was pressed moments ago;
        # when reaching for dictation, your hands have paused.
        self.guard = guard_ms / 1000.0
        self.on_trigger = on_trigger
        self._last_press = 0.0
        self._last_other = 0.0

    def _on_other_key(self) -> None:
        self._last_other = time.monotonic()
        self._last_press = 0.0  # a key in between means this was typing, not a tap

    def _on_key(self, value: int) -> None:
        if value != PRESS:
            return
        now = time.monotonic()
        if now - self._last_press <= self.window:
            self._last_press = 0.0  # consumed: a third tap starts over
            if self.on_trigger:
                self.on_trigger()
            return
        # Only let a tap open a sequence if it was not part of active typing.
        self._last_press = now if (now - self._last_other) >= self.guard else 0.0


class HoldListener(_Listener):
    """Push-to-talk: fires on press once held past `hold_ms`, and again on release.

    The hold threshold exists so a quick accidental tap does not open the microphone,
    and so that a key which also types something (space) produces one character rather
    than a dictation session.

    Autorepeat is ignored entirely -- the kernel repeats a held key, but the press and
    release edges are all that matter here.
    """

    def __init__(
        self,
        key: str = "SCROLLLOCK",
        hold_ms: int = 220,
        on_press: Callable[[], None] | None = None,
        on_release: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(key)
        self.hold = hold_ms / 1000.0
        self.on_press = on_press
        self.on_release = on_release
        self._down_at: float | None = None
        self._armed = False
        self._timer: threading.Timer | None = None

    def _on_key(self, value: int) -> None:
        if value == PRESS and self._down_at is None:
            self._down_at = time.monotonic()
            # Arm on a timer rather than waiting for the release, so the microphone
            # opens while the key is still down -- that is the whole point of hold.
            self._timer = threading.Timer(self.hold, self._arm)
            self._timer.daemon = True
            self._timer.start()
        elif value == 0 and self._down_at is not None:
            held = time.monotonic() - self._down_at
            self._down_at = None
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if self._armed:
                self._armed = False
                if self.on_release:
                    self.on_release(held)

    def _arm(self) -> None:
        if self._down_at is None:
            return
        self._armed = True
        if self.on_press:
            self.on_press()

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        super().stop()
