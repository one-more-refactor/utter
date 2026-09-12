"""Audio cues.

Generated once into the cache directory rather than shipped as binary assets, so the
repo stays text-only. Two short tones: rising when capture opens, falling when it
closes. A muted thud when something went wrong.

Cues matter more than they look: with push-to-talk you need to know capture opened
*before* you start speaking, and the overlay is often not where you are looking.
"""

from __future__ import annotations

import math
import struct
import subprocess
import wave
from pathlib import Path

from .config import CACHE_HOME

RATE = 48000


def _tone(path: Path, steps: list[tuple[float, float]], volume: float) -> None:
    """Write a WAV built from (frequency_hz, duration_secs) steps."""
    frames = bytearray()
    for freq, dur in steps:
        count = int(RATE * dur)
        for i in range(count):
            t = i / RATE
            # Short attack and release, so the cue never clicks.
            env = min(1.0, t / 0.006, (dur - t) / 0.02 if dur > t else 0.0)
            sample = math.sin(2 * math.pi * freq * t) * env * volume
            frames += struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(bytes(frames))


def ensure_cues(volume: float = 0.25) -> dict[str, Path]:
    CACHE_HOME.mkdir(parents=True, exist_ok=True)
    cues = {
        "start": (CACHE_HOME / "start.wav", [(660.0, 0.055), (990.0, 0.075)]),
        "stop": (CACHE_HOME / "stop.wav", [(880.0, 0.05), (587.0, 0.085)]),
        "error": (CACHE_HOME / "error.wav", [(320.0, 0.09), (240.0, 0.13)]),
    }
    out: dict[str, Path] = {}
    for name, (path, steps) in cues.items():
        if not path.exists():
            _tone(path, steps, volume)
        out[name] = path
    return out


class Cues:
    def __init__(self, enabled: bool = True, volume: float = 0.25) -> None:
        self.enabled = enabled
        self.paths: dict[str, Path] = {}
        if enabled:
            try:
                self.paths = ensure_cues(volume)
            except OSError:
                self.enabled = False

    def play(self, name: str) -> None:
        """Fire and forget -- a cue must never delay capture or delivery."""
        if not self.enabled:
            return
        path = self.paths.get(name)
        if not path or not path.exists():
            return
        try:
            subprocess.Popen(
                ["paplay", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.enabled = False
