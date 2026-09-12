"""Microphone capture via pw-record, with live level metering.

One `pw-record` process streams raw s16 mono to stdout. We read it in small chunks so
the overlay can show a live meter, and accumulate the same bytes for the WAV we hand
to the recogniser. One capture, two consumers, no second process.
"""

from __future__ import annotations

import array
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

CHUNK_SAMPLES = 1024  # ~64 ms at 16 kHz -- fast enough for a smooth meter


class Recorder:
    """Streams from the mic until stopped. Thread-safe start/stop."""

    def __init__(
        self,
        source: str = "default",
        rate: int = 16000,
        on_level: Callable[[float], None] | None = None,
    ) -> None:
        self.source = source
        self.rate = rate
        self.on_level = on_level
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._running = False
        self.started_at = 0.0
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def duration(self) -> float:
        if not self.started_at:
            return 0.0
        with self._lock:
            frames = len(self._buf) // 2
        return frames / float(self.rate)

    def start(self) -> None:
        if self._running:
            return
        cmd = [
            "pw-record",
            "--rate", str(self.rate),
            "--channels", "1",
            "--format", "s16",
            "--raw",
        ]
        if self.source and self.source != "default":
            cmd += ["--target", self.source]
        cmd.append("-")

        self.error = None
        with self._lock:
            self._buf = bytearray()
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            self.error = "pw-record not found"
            return

        self._running = True
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self._proc and self._proc.stdout
        want = CHUNK_SAMPLES * 2
        while self._running:
            chunk = self._proc.stdout.read(want)
            if not chunk:
                break
            with self._lock:
                self._buf += chunk
            if self.on_level:
                self.on_level(_peak(chunk))

    def stop(self) -> bytes:
        """Stop capture and return the raw PCM collected."""
        self._running = False
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        with self._lock:
            return bytes(self._buf)

    def stop_to_wav(self) -> tuple[Path, float]:
        """Stop and write a 16-bit mono WAV. Returns (path, duration_secs)."""
        pcm = self.stop()
        duration = (len(pcm) // 2) / float(self.rate)
        fd = tempfile.NamedTemporaryFile(prefix="utter-", suffix=".wav", delete=False)
        path = Path(fd.name)
        fd.close()
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.rate)
            wav.writeframes(pcm)
        return path, duration


def _peak(chunk: bytes) -> float:
    """Peak amplitude of an s16 chunk, 0.0-1.0."""
    if len(chunk) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
    if not samples:
        return 0.0
    return min(1.0, max(abs(s) for s in samples) / 32768.0)


def list_sources() -> list[tuple[str, str, str]]:
    """Return (node_name, serial, description) for every capture source.

    `pw-record --target` accepts a PipeWire node.name or a numeric object serial -- and
    nothing else. A sink's monitor generally has no usable node.name of its own, so
    monitors must be addressed by serial. Worth surfacing, because the PulseAudio-style
    names people copy out of `pactl` only work when they happen to equal node.name.
    """
    import json

    try:
        dump = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5
        ).stdout
        objects = json.loads(dump)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []

    out: list[tuple[str, str, str]] = []
    for obj in objects:
        props = ((obj.get("info") or {}).get("props")) or {}
        if props.get("media.class") not in ("Audio/Source", "Audio/Source/Virtual"):
            continue
        out.append(
            (
                str(props.get("node.name", "?")),
                str(props.get("object.serial", "?")),
                str(props.get("node.description", "")),
            )
        )
    return out
