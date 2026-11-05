"""The daemon's state machine and IPC, with a fake GLib, fake mic and fake injector.

These are the user-facing flows: trigger -> words typed while speaking -> commit, and
`utter toggle/status` over the unix socket.
"""

from __future__ import annotations

import json
import socket
import types
import wave
from pathlib import Path

import pytest

from utter.config import Config


class FakeRecorder:
    """Stands in for pw-record capture."""

    def __init__(self, source="default", rate=16000, on_level=None):
        self.error = None
        self.running = False
        self.duration = 2.0
        self.rate = rate

    def start(self):
        self.running = True

    def snapshot(self):
        return b"\x00\x00" * 160

    def write_wav(self, pcm):
        return self._wav(pcm)

    def stop(self):
        self.running = False
        return b""

    def stop_to_wav(self):
        self.running = False
        return self._wav(b"\x00\x00" * 160), self.duration

    def _wav(self, pcm):
        import tempfile

        fd = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        fd.close()
        with wave.open(fd.name, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(pcm)
        return Path(fd.name)


class SyncThread:
    """threading.Thread that runs its target immediately, for deterministic tests."""

    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def rig(daemon_mod, monkeypatch):
    """Build a Daemon wired to fakes. Returns a factory: rig(mode=..., final=...)."""
    events: list[tuple] = []

    def fake_type(text, delay_ms=2):
        events.append(("type", text))

    def fake_backspace(count, delay_ms=1):
        events.append(("bs", count))

    monkeypatch.setattr(daemon_mod.inject, "type_text", fake_type)
    monkeypatch.setattr(daemon_mod.inject, "backspace", fake_backspace)
    monkeypatch.setattr(daemon_mod, "Recorder", FakeRecorder)
    monkeypatch.setattr(daemon_mod, "threading", types.SimpleNamespace(Thread=SyncThread))

    def build(mode="double_tap", final="", stream=True, output="type"):
        cfg = Config()
        cfg.ui.sounds = False
        cfg.ui.tray = False
        cfg.trigger.mode = mode
        cfg.stream.enabled = stream
        cfg.output.mode = output
        d = daemon_mod.Daemon(cfg)
        d.backend.transcribe = lambda wav, audio_ctx=None: final
        d.events = events
        d.glib = daemon_mod.FAKE_GLIB
        return d

    return build


def test_double_tap_streams_words_and_removes_its_own_spaces(rig):
    d = rig(mode="double_tap", final="hello there my friend how are you")
    d._on_trigger()  # first double-tap: two spaces reached the window
    assert d.state == "recording"

    d._partial_done("hello there my friend")
    d._partial_done("hello there my friend how")
    d._partial_done("hello there my friend how are")
    d._on_trigger()  # second double-tap commits (two more spaces)
    d.glib.drain()

    assert d.events == [
        ("bs", 2),
        ("type", "Hello there"),
        ("type", " my"),
        ("bs", 2),
        ("type", " friend how are you"),
    ]
    assert d.state == "idle"
    assert d._stats["utterances"] == 1
    assert d._stats["last_text"] == "Hello there my friend how are you"


def test_chord_trigger_types_nothing_to_delete(rig):
    d = rig(mode="chord", final="ask not")
    d._on_trigger()
    d._on_trigger()
    d.glib.drain()
    assert d.events == [("type", "Ask not")]


def test_hold_on_an_inert_key_commits_on_release(rig):
    d = rig(mode="hold", final="push to talk")
    d._on_hold_press()
    assert d._auto_stop is False  # pausing while holding must not commit
    d._on_hold_release()
    d.glib.drain()
    assert d.events == [("type", "Push to talk")]


def test_cancel_discards_and_cleans_up_the_trigger(rig):
    d = rig(mode="double_tap")
    d._on_trigger()
    d.cancel()
    assert d.events == [("bs", 2)]
    assert d.state == "idle"


def test_silence_types_nothing_but_still_cleans_up(rig):
    d = rig(mode="double_tap", final="[BLANK_AUDIO]", stream=False)
    d._on_trigger()
    d.stop()
    d.glib.drain()
    assert d.events == [("bs", 2)]
    assert d.state == "idle"


def test_without_streaming_the_whole_text_lands_at_once(rig):
    d = rig(mode="double_tap", final="see three do is up", stream=False)
    d.cfg.output.replacements = {"see three do": "cr3do"}
    d._on_trigger()
    d._on_trigger()
    d.glib.drain()
    assert d.events == [("bs", 4), ("type", "cr3do is up")]


def test_auto_stop_needs_speech_then_silence(rig):
    d = rig()
    d.cfg.audio.silence_ms = 0
    d.recorder = FakeRecorder()
    d._recent = [0.0]
    assert d._should_auto_stop() is False  # nothing said yet: keep listening
    d._recent = [0.5]
    assert d._should_auto_stop() is False
    d._recent = [0.0]
    assert d._should_auto_stop() is False  # silence starts now
    assert d._should_auto_stop() is True


def test_streaming_switches_the_llm_pass_off(daemon_mod):
    # The LLM pass rewrites the whole transcript, which live typing has already put
    # in the window word by word -- so with streaming on, it stands down.
    cfg = Config()
    cfg.ui.sounds = False
    cfg.cleanup.enabled = True
    assert daemon_mod.Daemon(cfg).cfg.cleanup.enabled is False


def test_dispatch(rig):
    d = rig()
    status = json.loads(d._dispatch("status"))
    assert status["state"] == "idle"
    assert status["backend"] == "whisper-server"
    assert d._dispatch("toggle") == "ok\n"
    assert d.glib.idle[-1][0] == d.toggle
    assert d._dispatch("rm -rf /").startswith("error: unknown command")


def test_socket_roundtrip(daemon_mod, runtime_dir):
    cfg = Config()
    cfg.ui.sounds = False
    d = daemon_mod.Daemon(cfg)
    path = runtime_dir / "utter.sock"

    # A stale socket left by a crashed daemon is replaced, not obeyed.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()

    d._serve()
    try:
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert json.loads(daemon_mod.send("status"))["state"] == "idle"
        assert daemon_mod.send("toggle") == "ok"

        other = daemon_mod.Daemon(cfg)
        with pytest.raises(RuntimeError, match="already listening"):
            other._serve()
    finally:
        d.cleanup()
    assert not path.exists()
    with pytest.raises(FileNotFoundError):
        daemon_mod.send("status")
