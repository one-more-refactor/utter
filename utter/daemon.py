"""The daemon: owns the model, the microphone, the tray, and the state machine.

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

import dbus.mainloop.glib
from gi.repository import GLib

from . import asr as asr_mod  # noqa: E402
from . import inject, text as text_mod  # noqa: E402
from .audio import Recorder  # noqa: E402
from .config import Config, socket_path  # noqa: E402
from .sound import Cues  # noqa: E402
from .stream import StreamState  # noqa: E402

def _types_char(key: str) -> bool:
    from .hotkey import types_a_character

    return types_a_character(key)


IDLE, RECORDING, WORKING = "idle", "recording", "working"
TICK_MS = 60
SMOOTH_CHUNKS = 6  # ~380 ms of 64 ms chunks


class Daemon:
    def __init__(self, cfg: Config, verbose: bool = False) -> None:
        self.cfg = cfg
        self.verbose = verbose
        self.state = IDLE
        self.recorder: Recorder | None = None
        self.tray = None
        self.cues = Cues(cfg.ui.sounds, cfg.ui.sound_volume)
        self.backend = asr_mod.build(cfg)
        self._level = 0.0
        self._recent: list[float] = []
        self._tick: int | None = None
        self._guard: int | None = None
        self._sock: socket.socket | None = None
        self._sock_ino: int | None = None
        self._loop = GLib.MainLoop()
        self._stats = {"utterances": 0, "last_ms": 0, "last_text": ""}
        self.listener = None
        self._partial = ""
        self._partial_busy = False
        self._partial_timer: int | None = None
        self._silence_since: float | None = None
        self._heard_speech = False
        self._pending_backspace = 0
        self._auto_stop = cfg.audio.auto_stop
        self.stream = StreamState(agree=cfg.stream.agree, lag=cfg.stream.lag)
        self._streaming = cfg.stream.enabled and cfg.output.mode == "type"
        if self._streaming and cfg.cleanup.enabled:
            # The cleanup pass rewrites the whole transcript, which means deleting text
            # that was typed word by word over the last ten seconds -- by which time the
            # caret may have moved. Live typing wins; the polish pass stands down.
            print("utter: stream.enabled, so cleanup is disabled (it would rewrite typed text)")
            cfg.cleanup.enabled = False

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

        if self.cfg.trigger.enabled:
            self._arm_trigger()

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

    def _arm_trigger(self) -> None:
        tc = self.cfg.trigger
        from .hotkey import DoubleTapListener, HoldListener, types_a_character

        if tc.mode == "chord":
            from .hotkey import ChordListener

            self.listener = ChordListener(
                key=tc.key,
                modifiers=tc.modifiers,
                on_trigger=lambda: GLib.idle_add(self._on_trigger),
            )
            unknown = self.listener.unknown_modifiers
            if unknown:
                print(f"utter: unknown modifier(s) {', '.join(unknown)} - trigger disabled")
                self.listener = None
                return
            shape = self.listener.describe()
        elif tc.mode == "hold":
            self.listener = HoldListener(
                key=tc.key,
                hold_ms=tc.hold_ms,
                on_press=lambda: GLib.idle_add(self._on_hold_press),
                on_release=lambda _held: GLib.idle_add(self._on_hold_release),
            )
            shape = f"hold {tc.key} ({tc.hold_ms} ms to arm)"
        else:
            self.listener = DoubleTapListener(
                key=tc.key,
                window_ms=tc.double_tap_ms,
                guard_ms=tc.guard_ms,
                on_trigger=lambda: GLib.idle_add(self._on_trigger),
            )
            shape = f"double-tap {tc.key} (within {tc.double_tap_ms} ms)"

        if not self.listener.start():
            print(f"utter: trigger disabled - {self.listener.error}")
            self.listener = None
            return

        names = ", ".join(sorted({n for _p, n in self.listener.devices}))
        self.log(f"{shape} armed on: {names}")

        # Holding a key that types something inserts that character repeatedly while
        # held -- the compositor's autorepeat, which we cannot count reliably. Say so
        # rather than silently mangling the text.
        if tc.mode == "hold" and types_a_character(tc.key):
            print(
                f"utter: warning - holding {tc.key} types characters into the focused "
                f"window while held. Use an inert key (SCROLLLOCK, PAUSE, F13, MENU) "
                f"for hold mode, or mode = \"double_tap\" for {tc.key}."
            )

    def _warm_cleanup(self) -> None:
        t0 = time.monotonic()
        ok = text_mod.warm(self.cfg)
        if ok:
            self.log(f"cleanup model warm in {(time.monotonic() - t0) * 1000:.0f} ms")
        else:
            self.log("cleanup model could not be preloaded; is the runner up?")

    def cleanup(self) -> None:
        if self.listener:
            self.listener.stop()
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

    def _on_trigger(self) -> bool:
        """Double-tap fired. Start dictating, or commit if already listening."""
        if self.state == RECORDING:
            if self.cfg.trigger.tap_to_commit:
                if self.cfg.trigger.mode != "chord":
                    # The two taps landed in the target window as spaces.
                    self._pending_backspace += self.cfg.trigger.backspace
                self.stop()
        elif self.state == IDLE:
            self._pending_backspace = (
                0 if self.cfg.trigger.mode == "chord" else self.cfg.trigger.backspace
            )
            self.start()
        return GLib.SOURCE_REMOVE

    def _on_hold_press(self) -> bool:
        """Key held past the threshold: open the microphone."""
        if self.state == IDLE:
            # In hold mode the release is the stop signal, so silence must not commit
            # early -- the speaker may simply be pausing while still holding the key.
            self._pending_backspace = (
                self.cfg.trigger.backspace if _types_char(self.cfg.trigger.key) else 0
            )
            self.start(auto_stop=False)
        return GLib.SOURCE_REMOVE

    def _on_hold_release(self) -> bool:
        """Key released: commit what was said."""
        if self.state == RECORDING:
            self.stop()
        return GLib.SOURCE_REMOVE

    def toggle(self) -> bool:
        if self.state == RECORDING:
            self.stop()
        elif self.state == IDLE:
            self.start()
        return GLib.SOURCE_REMOVE

    def start(self, auto_stop: bool | None = None) -> bool:
        if self.state != IDLE:
            return GLib.SOURCE_REMOVE
        self._auto_stop = self.cfg.audio.auto_stop if auto_stop is None else auto_stop
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
        self._partial = ""
        self._silence_since = None
        self._heard_speech = False
        self._recent = []
        self.cues.play("start")
        if self.tray:
            self.tray.set_state("recording")
        self._tick = GLib.timeout_add(TICK_MS, self._on_tick)
        # Without this guard a missed stop becomes a multi-minute recording.
        self._guard = GLib.timeout_add(
            int(self.cfg.audio.max_duration_secs * 1000), self._on_max_duration
        )
        if self._streaming:
            self.stream.reset()
            self._partial_timer = GLib.timeout_add(
                self.cfg.stream.interval_ms, self._on_partial_tick
            )
        self.log("recording")
        return GLib.SOURCE_REMOVE

    def _note_level(self, level: float) -> None:
        # Read by the GTK tick; never touch widgets off-thread.
        self._level = level
        # Keep a short history so silence detection looks at a window rather than one
        # 64 ms chunk. Consonants and breaths dip low constantly; an utterance has not
        # ended until the whole window is quiet.
        self._recent.append(level)
        if len(self._recent) > SMOOTH_CHUNKS:
            del self._recent[:-SMOOTH_CHUNKS]

    def _on_tick(self) -> bool:
        if self.state != RECORDING or not self.recorder:
            return GLib.SOURCE_REMOVE
        if self._auto_stop and self._should_auto_stop():
            self.log("silence detected; committing")
            self.stop()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _should_auto_stop(self) -> bool:
        """True once the speaker has clearly stopped talking.

        Requires speech to have been heard first, so opening the mic and thinking for a
        moment does not instantly commit an empty recording.
        """
        ac = self.cfg.audio
        if not self.recorder:
            return False
        if self.recorder.duration * 1000 < ac.min_speech_ms:
            return False
        now = time.monotonic()
        window_peak = max(self._recent) if self._recent else 0.0
        if window_peak >= ac.silence_level:
            self._heard_speech = True
            self._silence_since = None
            return False
        if not self._heard_speech:
            return False
        if self._silence_since is None:
            self._silence_since = now
            return False
        return (now - self._silence_since) * 1000 >= ac.silence_ms

    def _on_partial_tick(self) -> bool:
        """Re-recognise everything captured so far, so words appear while speaking.

        Cheap because the model is resident: a full 11 s utterance costs ~200 ms. Only
        one partial is ever in flight, so a slow pass just skips a beat.
        """
        if self.state != RECORDING or not self.recorder:
            self._partial_timer = None
            return GLib.SOURCE_REMOVE
        if not self._partial_busy and self.recorder.duration >= self.cfg.stream.min_audio_secs:
            self._partial_busy = True
            pcm = self.recorder.snapshot()
            dur = self.recorder.duration
            threading.Thread(
                target=self._partial_worker, args=(pcm, dur), daemon=True
            ).start()
        return GLib.SOURCE_CONTINUE

    def _partial_worker(self, pcm: bytes, duration: float) -> None:
        wav = None
        try:
            if not self.recorder:
                return
            wav = self.recorder.write_wav(pcm)
            guess = self.backend.transcribe(wav, audio_ctx=asr_mod.audio_ctx_for(duration))
        except (asr_mod.AsrError, OSError, AttributeError):
            guess = ""
        finally:
            if wav is not None:
                wav.unlink(missing_ok=True)
            GLib.idle_add(self._partial_done, guess)

    def _partial_done(self, guess: str) -> bool:
        self._partial_busy = False
        if self.state != RECORDING:
            return GLib.SOURCE_REMOVE
        shown = text_mod.tidy(guess)
        if not shown:
            return GLib.SOURCE_REMOVE
        self._partial = shown

        if self._streaming:
            chunk = self.stream.offer(shown)
            if chunk:
                self._emit(chunk)
        return GLib.SOURCE_REMOVE

    def _emit(self, chunk: str) -> None:
        """Type a chunk that will never be revised. Append only -- never backspace.

        Injection is fire-and-forget: we cannot read the target buffer back, so any
        "delete what I typed" logic is a guess that corrupts real text the moment the
        user moves the caret or an autocomplete fires.
        """
        text = text_mod.live_clean(chunk)
        text = text_mod.apply_replacements(text, self.cfg.output.replacements)
        if not text.strip():
            return
        try:
            inject.type_text(text, self.cfg.output.type_delay_ms)
        except inject.InjectError as exc:
            self.log(f"could not type chunk: {exc}")
            return
        self._stats["last_text"] = self.stream.typed
        self.log(f"typed: {text!r}")

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
        for attr in ("_tick", "_guard", "_partial_timer"):
            handle = getattr(self, attr)
            if handle is not None:
                GLib.source_remove(handle)
                setattr(self, attr, None)

    # -- recognition + delivery ------------------------------------------------

    def _transcribe(self, wav: Path, duration: float) -> None:
        started = time.monotonic()
        deadline = started + 2.0
        while self._partial_busy and time.monotonic() < deadline:
            time.sleep(0.02)
        try:
            raw = self.backend.transcribe(wav, audio_ctx=asr_mod.audio_ctx_for(duration))
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

        if self._streaming:
            # Everything up to the last committed word is already in the window.
            tail = self.stream.finish(body)
            if tail:
                self._emit(tail)
            typed = self.stream.typed
            self.stream.reset()
            if typed:
                self._stats["utterances"] += 1
                self._stats["last_ms"] = elapsed_ms
                self._stats["last_text"] = typed
            else:
                self.log("nothing recognised; nothing typed")
            self._finish_ok()
            return GLib.SOURCE_REMOVE

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

        self._consume_backspace()
        try:
            inject.deliver(body, self.cfg.output.mode, self.cfg.output.type_delay_ms)
        except inject.InjectError as exc:
            self._fail(f"{exc} (text copied to clipboard instead)")
            return GLib.SOURCE_REMOVE

        if cleanup_wanted:
            threading.Thread(
                target=self._clean_then_replace, args=(body,), daemon=True
            ).start()
        else:
            self._finish_ok()
        return GLib.SOURCE_REMOVE

    def _consume_backspace(self) -> None:
        """Delete the trigger keystrokes that reached the focused window."""
        count, self._pending_backspace = self._pending_backspace, 0
        if count and self.cfg.output.mode == "type":
            try:
                inject.backspace(count)
            except inject.InjectError:
                pass

    def _clean_then_type(self, body: str) -> None:
        cleaned = text_mod.clean_with_llm(body, self.cfg)
        GLib.idle_add(self._type_final, cleaned)

    def _type_final(self, cleaned: str) -> bool:
        self._consume_backspace()
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
        if self.tray:
            self.tray.set_state("idle")
        self.state = IDLE

    def _fail(self, message: str) -> bool:
        print(f"utter: {message}", flush=True)
        self.cues.play("error")
        if self.tray:
            self.tray.set_state("failed")
        self.state = IDLE
        return GLib.SOURCE_REMOVE

    def _reset(self) -> None:
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
    """Nothing to initialise: there is no GUI, only a GLib main loop."""
    return None
