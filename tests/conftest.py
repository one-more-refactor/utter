"""Shared fixtures.

Nothing here needs a microphone, a GPU, a Wayland session or a model. External tools
(wtype, pw-record, whisper-server, ...) are replaced by tiny shell stubs on PATH, and
PyGObject/dbus by an in-process fake GLib, so the suite runs on a bare CI runner.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# -- stub executables -----------------------------------------------------------------


class StubBin:
    """A directory of fake executables, each logging its argv to `<name>.log`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, name: str, body: str = "") -> Path:
        script = self.path / name
        log = self.path / f"{name}.log"
        script.write_text(
            "#!/bin/sh\n"
            f'for a in "$@"; do printf "%s\\n" "$a" >> "{log}"; done\n'
            f'printf -- "--end--\\n" >> "{log}"\n'
            f"{body}\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return script

    def calls(self, name: str) -> list[list[str]]:
        """Every invocation of `name`, as a list of argv lists."""
        log = self.path / f"{name}.log"
        if not log.exists():
            return []
        out: list[list[str]] = [[]]
        for line in log.read_text().splitlines():
            if line == "--end--":
                out.append([])
            else:
                out[-1].append(line)
        return [c for c in out[:-1]]


@pytest.fixture
def stubs(tmp_path, monkeypatch) -> StubBin:
    """Stub executables, put first on PATH."""
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ.get('PATH', '')}")
    return StubBin(d)


@pytest.fixture
def only_stubs(stubs, monkeypatch) -> StubBin:
    """Like `stubs`, but nothing else on PATH -- for testing missing tools."""
    monkeypatch.setenv("PATH", str(stubs.path))
    return stubs


@pytest.fixture
def runtime_dir(monkeypatch):
    """A short XDG_RUNTIME_DIR (unix socket paths are limited to ~108 bytes)."""
    d = Path(tempfile.mkdtemp(prefix="utter-t-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


# -- a fake GLib, so utter.daemon imports without PyGObject ------------------------------


class FakeGLib:
    SOURCE_REMOVE = False
    SOURCE_CONTINUE = True

    def __init__(self) -> None:
        self.idle: list[tuple] = []
        self.timeouts: dict[int, tuple] = {}
        self._next = 0

    def _id(self) -> int:
        self._next += 1
        return self._next

    def idle_add(self, fn, *args):
        self.idle.append((fn, args))
        return self._id()

    def timeout_add(self, ms, fn, *args):
        handle = self._id()
        self.timeouts[handle] = (ms, fn, args)
        return handle

    def source_remove(self, handle):
        self.timeouts.pop(handle, None)
        return True

    def drain(self) -> int:
        """Run queued idle callbacks (and any they queue). Returns how many ran."""
        ran = 0
        while self.idle:
            fn, args = self.idle.pop(0)
            fn(*args)
            ran += 1
        return ran

    class MainLoop:
        def run(self):
            return None

        def quit(self):
            return None


@pytest.fixture
def daemon_mod(monkeypatch):
    """`utter.daemon`, imported against a fake GLib and a fake dbus."""
    glib = FakeGLib()
    gi = types.ModuleType("gi")
    repo = types.ModuleType("gi.repository")
    repo.GLib = glib
    gi.repository = repo
    dbus = types.ModuleType("dbus")
    mainloop = types.ModuleType("dbus.mainloop")
    glib_ml = types.ModuleType("dbus.mainloop.glib")
    glib_ml.DBusGMainLoop = lambda **_kw: None
    mainloop.glib = glib_ml
    dbus.mainloop = mainloop
    for name, mod in {
        "gi": gi,
        "gi.repository": repo,
        "dbus": dbus,
        "dbus.mainloop": mainloop,
        "dbus.mainloop.glib": glib_ml,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    sys.modules.pop("utter.daemon", None)
    import utter.daemon as mod

    mod.FAKE_GLIB = glib
    yield mod
    sys.modules.pop("utter.daemon", None)
