"""The daemon: owns the model, the mic, the overlay, the tray, and the state machine.

Why a daemon at all: loading the model is the single largest cost in naive dictation
scripts -- 243 ms of the 486 ms a one-shot whisper-cli run takes. Keeping it resident
is what gets an utterance down to ~200 ms.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")

import dbus.mainloop.glib  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

from . import asr as asr_mod  # noqa: E402
from . import inject, text as text_mod  # noqa: E402
from .audio import Recorder  # noqa: E402
from .config import Config, socket_path  # noqa: E402
from .sound import Cues  # noqa: E402

IDLE, RECORDING, WORKING = "idle", "recording", "working"
TICK_MS = 60


class Daemon:
    def __init__(self, cfg: Config, verbose: bool = False) -> None:
        self.cfg = cfg
        self.verbose = verbose
        self.state = IDLE
        self.recorder: Recorder | None = None
        self.overlay = None
        self.tray = None
        self.cues = Cues(cfg.ui.sounds, cfg.ui.sound_volume)
        self.backend = asr_mod.build(cfg)
        self._level = 0.0
        self._tick: int | None = None
        self._guard: int | None = None
        self._sock: socket.socket | None = None
        self._sock_ino: int | None = None
        self._loop = GLib.MainLoop()
        self._stats = {"utterances": 0, "last_ms": 0, "last_text": ""}

    # -- lifecycle -------------------------------------------------------------

    def run(self) -> int:
        problems = self.cfg.check()
        if problems:
            for p in problems:
                print(f"utter: {p}")
            return 1

        self.log(f"starting {self.backend.name}")
        t0 = time.monotonic()
        try:
            self.backend.start()
        except asr_mod.AsrError as exc:
            print(f"utter: {exc}")
            return 1
        self.log(f"model ready in {(time.monotonic() - t0) * 1000:.0f} ms")

        if self.cfg.ui.overlay:
            try:
                from .overlay import Overlay

                self.overlay = Overlay(self.cfg.ui.position, self.cfg.ui.margin)
            except Exception as exc:  # a missing layer-shell must not be fatal
                self.log(f"overlay unavailable: {exc}")

        if self.cfg.ui.tray:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            try:
                from .tray import Tray

                self.tray = Tray(
                    on_toggle=lambda: GLib.idle_add(self.toggle),
                    on_quit=lambda: GLib.idle_add(self.quit),
                )
                if not self.tray.register():
                    self.log("no tray host found; continuing without an indicator")
            except Exception as exc:
                self.log(f"tray unavailable: {exc}")

        try:
            self._serve()
        except (RuntimeError, OSError) as exc:
            print(f"utter: {exc}")
            self.backend.stop()
            return 1
        if self.cfg.cleanup.enabled and self.cfg.cleanup.warm_on_start:
            threading.Thread(target=self._warm_cleanup, daemon=True).start()

        print(f"utter: ready ({self.backend.name}); socket {socket_path()}")
        try:
            self._loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.cleanup()
        return 0

    def _warm_cleanup(self) -> None:
        t0 = time.monotonic()
        ok = text_mod.warm(self.cfg)
        if ok:
            self.log(f"cleanup model warm in {(time.monotonic() - t0) * 1000:.0f} ms")
        else:
            self.log("cleanup model could not be preloaded; is the runner up?")

    def cleanup(self) -> None:
        if self.recorder and self.recorder.running:
            self.recorder.stop()
        self.backend.stop()
        if self._sock:
            self._sock.close()
        self._unlink_own_socket()

    def _unlink_own_socket(self) -> None:
        """Remove the socket file only if it is still the one we bound."""
        if self._sock_ino is None:
            return
        path = socket_path()
        try:
            if path.stat().st_ino == self._sock_ino:
                path.unlink()
        except OSError:
            pass

    def quit(self) -> bool:
        self._loop.quit()
        return GLib.SOURCE_REMOVE

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"utter: {msg}", flush=True)

    # -- IPC -------------------------------------------------------------------

    def _serve(self) -> None:
        path = socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if _socket_is_live(path):
                raise RuntimeError(f"another utter daemon is already listening on {path}")
            path.unlink()  # stale socket from a daemon that did not clean up
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        os.chmod(path, 0o600)
        # Remember which socket file is ours. A previous daemon shutting down must not
        # delete the socket a new daemon has just bound.
        self._sock_ino = path.stat().st_ino
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        assert self._sock
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # socket closed: we are shutting down
            # One bad client must never take the accept loop down with it. A liveness
            # probe, for instance, connects and closes without reading our reply.
            try:
                with conn:
                    conn.settimeout(5.0)
                    cmd = conn.recv(256).decode("utf-8", "replace").strip()
                    if not cmd:
                        continue
                    conn.sendall(self._dispatch(cmd).encode())
            except OSError:
                continue

    def _dispatch(self, cmd: str) -> str:
        if cmd == "status":
            return json.dumps(
                {
                    "state": self.state,
                    "backend": self.backend.name,
                    "cleanup": self.cfg.cleanup.enabled,
                    **self._stats,
                }
            ) + "\n"
        actions = {
            "toggle": self.toggle,
            "start": self.start,
            "stop": self.stop,
            "cancel": self.cancel,
            "quit": self.quit,
        }
        action = actions.get(cmd)
        if not action:
            return f"error: unknown command {cmd!r}\n"
        GLib.idle_add(action)
        return "ok\n"

    # -- state machine ---------------------------------------------------------

    def toggle(self) -> bool:
        if self.state == RECORDING:
            self.stop()
        elif self.state == IDLE:
            self.start()
        return GLib.SOURCE_REMOVE

    def start(self) -> bool:
        if self.state != IDLE:
            return GLib.SOURCE_REMOVE
        self.recorder = Recorder(
            source=self.cfg.audio.source,
            rate=self.cfg.audio.rate,
            on_level=self._note_level,
        )
        self.recorder.start()
        if self.recorder.error:
            self._fail(self.recorder.error)
            return GLib.SOURCE_REMOVE

        self.state = RECORDING
        self.cues.play("start")
        if self.overlay:
            self.overlay.show()
        if self.tray:
            self.tray.set_state("recording")
        self._tick = GLib.timeout_add(TICK_MS, self._on_tick)
        # Without this guard a missed stop becomes a multi-minute recording.
        self._guard = GLib.timeout_add(
            int(self.cfg.audio.max_duration_secs * 1000), self._on_max_duration
        )
        self.log("recording")
        return GLib.SOURCE_REMOVE

    def _note_level(self, level: float) -> None:
        self._level = level  # read by the GTK tick; never touch widgets off-thread

    def _on_tick(self) -> bool:
        if self.state != RECORDING or not self.recorder:
            return GLib.SOURCE_REMOVE
        if self.overlay:
            self.overlay.set_level(self._level)
            self.overlay.set_elapsed(self.recorder.duration)
        return GLib.SOURCE_CONTINUE

    def _on_max_duration(self) -> bool:
        self.log("hit max_duration_secs; stopping")
        self._guard = None
        self.stop()
        return GLib.SOURCE_REMOVE

    def stop(self) -> bool:
        if self.state != RECORDING or not self.recorder:
            return GLib.SOURCE_REMOVE
        self._clear_timers()
        wav, duration = self.recorder.stop_to_wav()
        self.recorder = None
        self.cues.play("stop")

        if duration < self.cfg.audio.min_duration_secs:
            self.log(f"discarded {duration:.2f}s (below min_duration_secs)")
            wav.unlink(missing_ok=True)
            self._reset()
            return GLib.SOURCE_REMOVE

        self.state = WORKING
        if self.overlay:
            self.overlay.set_state("working")
        if self.tray:
            self.tray.set_state("working")
        threading.Thread(target=self._transcribe, args=(wav, duration), daemon=True).start()
        return GLib.SOURCE_REMOVE

    def cancel(self) -> bool:
        if self.state == RECORDING and self.recorder:
            self._clear_timers()
            self.recorder.stop()
            self.recorder = None
            self.log("cancelled")
        self._reset()
        return GLib.SOURCE_REMOVE

    def _clear_timers(self) -> None:
        for attr in ("_tick", "_guard"):
            handle = getattr(self, attr)
            if handle is not None:
                GLib.source_remove(handle)
                setattr(self, attr, None)

    # -- recognition + delivery ------------------------------------------------

    def _transcribe(self, wav: Path, duration: float) -> None:
        started = time.monotonic()
        try:
            raw = self.backend.transcribe(wav)
        except asr_mod.AsrError as exc:
            GLib.idle_add(self._fail, str(exc))
            return
        finally:
            wav.unlink(missing_ok=True)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self.log(f"transcribed {duration:.1f}s of audio in {elapsed_ms} ms")
        GLib.idle_add(self._deliver, raw, elapsed_ms)

    def _deliver(self, raw: str, elapsed_ms: int) -> bool:
        body = text_mod.tidy(raw)
        body = text_mod.apply_replacements(body, self.cfg.output.replacements)
        if not body:
            self.log("empty transcript (silence or hallucination); nothing typed")
            self._reset()
            return GLib.SOURCE_REMOVE

        self._stats["utterances"] += 1
        self._stats["last_ms"] = elapsed_ms
        self._stats["last_text"] = body

        cleanup_wanted = self.cfg.cleanup.enabled and self.cfg.cleanup.intensity != "off"
        # Two-stage: the raw text lands now, the cleaned version replaces it shortly.
        # A ~1.2 s pipeline then feels like a ~0.2 s one.
        if cleanup_wanted and not self.cfg.output.two_stage:
            threading.Thread(target=self._clean_then_type, args=(body,), daemon=True).start()
            return GLib.SOURCE_REMOVE

        try:
            inject.deliver(body, self.cfg.output.mode, self.cfg.output.type_delay_ms)
        except inject.InjectError as exc:
            self._fail(f"{exc} (text copied to clipboard instead)")
            return GLib.SOURCE_REMOVE

        if cleanup_wanted:
            if self.overlay:
                self.overlay.set_note("polishing")
            threading.Thread(target=self._clean_then_replace, args=(body,), daemon=True).start()
        else:
            self._finish_ok()
        return GLib.SOURCE_REMOVE

    def _clean_then_type(self, body: str) -> None:
        cleaned = text_mod.clean_with_llm(body, self.cfg)
        GLib.idle_add(self._type_final, cleaned)

    def _type_final(self, cleaned: str) -> bool:
        try:
            inject.deliver(cleaned, self.cfg.output.mode, self.cfg.output.type_delay_ms)
        except inject.InjectError as exc:
            self._fail(str(exc))
            return GLib.SOURCE_REMOVE
        self._finish_ok()
        return GLib.SOURCE_REMOVE

    def _clean_then_replace(self, body: str) -> None:
        cleaned = text_mod.clean_with_llm(body, self.cfg)
        GLib.idle_add(self._replace_with, body, cleaned)

    def _replace_with(self, body: str, cleaned: str) -> bool:
        if cleaned != body and self.cfg.output.mode == "type":
            try:
                inject.replace_text(body, cleaned, self.cfg.output.type_delay_ms)
            except inject.InjectError as exc:
                self.log(f"could not apply cleanup: {exc}")
        self._stats["last_text"] = cleaned
        self._finish_ok()
        return GLib.SOURCE_REMOVE

    def _finish_ok(self) -> None:
        if self.overlay:
            self.overlay.finish("done")
        if self.tray:
            self.tray.set_state("idle")
        self.state = IDLE

    def _fail(self, message: str) -> bool:
        print(f"utter: {message}", flush=True)
        self.cues.play("error")
        if self.overlay:
            self.overlay.finish("failed", delay_ms=1400)
        if self.tray:
            self.tray.set_state("failed")
        self.state = IDLE
        return GLib.SOURCE_REMOVE

    def _reset(self) -> None:
        if self.overlay:
            self.overlay.hide()
        if self.tray:
            self.tray.set_state("idle")
        self.state = IDLE


def _socket_is_live(path: Path) -> bool:
    """True if something is actually accepting on this socket path."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            return False
    return True


def send(cmd: str, timeout: float = 5.0) -> str:
    """Client side: send one command to a running daemon."""
    path = socket_path()
    if not path.exists():
        raise FileNotFoundError("utter daemon is not running")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(cmd.encode())
        return sock.recv(65536).decode("utf-8", "replace").strip()


def ensure_gtk_init() -> None:
    Gtk.init()
