"""Configuration loading. TOML at ~/.config/utter/config.toml, with defaults."""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "utter"
CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "utter"
CONFIG_PATH = CONFIG_HOME / "config.toml"

DEFAULT_MODEL_DIR = Path.home() / "ai" / "stt"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) if base else Path(f"/tmp/utter-{os.getuid()}")


def socket_path() -> Path:
    return runtime_dir() / "utter.sock"


@dataclass
class AudioConfig:
    # PipeWire/PulseAudio source name. "default" follows the system default source.
    # Never point this at a `.monitor` node -- that records your speakers, not your voice.
    source: str = "default"
    rate: int = 16000
    # Hard ceiling on a single utterance. Without this, a missed stop turns into a
    # multi-minute recording of near-silence, which ASR models happily hallucinate over.
    max_duration_secs: float = 60.0
    # Drop recordings shorter than this -- almost always an accidental double-tap.
    min_duration_secs: float = 0.35
    # Stop on your own silence, so dictation ends by itself. This is what makes it feel
    # like a voice mode rather than a recorder you have to switch off.
    auto_stop: bool = True
    # Peak level (0.0-1.0) below which audio counts as silence.
    silence_level: float = 0.02
    # How long that silence must last before committing.
    silence_ms: int = 1500
    # Never auto-stop before this much audio exists, so a slow start is not cut off.
    min_speech_ms: int = 500


@dataclass
class AsrConfig:
    # "whisper-server" keeps the model resident (~220 ms per utterance, measured on a
    # 9070 XT). "parakeet-cli" is more accurate but reloads the model every time (~700 ms).
    backend: str = "whisper-server"
    model: str = str(DEFAULT_MODEL_DIR / "ggml-large-v3-turbo-q8_0.bin")
    parakeet_model: str = str(DEFAULT_MODEL_DIR / "ggml-parakeet-v3-q8.bin")
    whisper_server_bin: str = "whisper-server"
    parakeet_cli_bin: str = "parakeet-cli"
    port: int = 18124
    language: str = "en"
    threads: int = 0  # 0 = let the backend decide
    # Prepended to the model's context. Keep it a short fluent sentence to pin the output
    # language -- do NOT put a vocabulary list here, it measurably raises overall WER.
    initial_prompt: str = ""
    # Extra LD_LIBRARY_PATH, for running an uninstalled/extracted whisper.cpp build.
    ld_library_path: str = ""


@dataclass
class CleanupConfig:
    """Optional local LLM pass: strips fillers, fixes punctuation, keeps meaning."""

    enabled: bool = False
    endpoint: str = "http://127.0.0.1:11434/api/chat"
    model: str = "huihui_ai/gemma-4-abliterated:e4b"
    # "light" only removes fillers and fixes punctuation. "off" skips the pass entirely.
    intensity: str = "light"
    timeout_secs: float = 15.0
    # Ask the runner to keep the model resident. A cold load costs ~7 s; warm is ~1 s.
    keep_alive: str = "30m"
    # Preload the model when the daemon starts, so the first dictation is not the slow one.
    warm_on_start: bool = True
    # Spelling authority for jargon the recogniser mangles. This belongs here, in the
    # LLM stage -- not in the ASR prompt.
    vocabulary: list[str] = field(default_factory=list)


@dataclass
class TriggerConfig:
    """Global key trigger, read straight from evdev -- see hotkey.py."""

    enabled: bool = True
    # "double_tap": tap the key twice to start, again to commit (or stop talking).
    # "hold":       hold the key to talk, release to commit -- classic push-to-talk.
    mode: str = "double_tap"
    # The key. Name from KEY_CODES, or a raw keycode.
    # For "hold", prefer a key that types nothing: SCROLLLOCK, PAUSE, F13, MENU.
    key: str = "SPACE"
    double_tap_ms: int = 320
    # How long the key must be held before the microphone opens, in "hold" mode. Stops
    # an accidental brush from starting a dictation.
    hold_ms: int = 220
    # The trigger keys still reach the focused window (this is a passive read, not a
    # grab), so two stray spaces get typed. Delete them before inserting the transcript.
    backspace: int = 2
    # Double-tap again while dictating to commit early.
    tap_to_commit: bool = True


@dataclass
class OutputConfig:
    # "type" synthesises keystrokes via wtype. "clipboard" copies and leaves pasting to you.
    mode: str = "type"
    # Milliseconds between synthesised keystrokes. 0 drops characters on some compositors.
    type_delay_ms: int = 2
    # Paste the raw transcript immediately, then correct it once cleanup returns.
    # Makes a ~1.2 s pipeline feel like ~0.2 s.
    two_stage: bool = True
    # Deterministic find/replace, applied before the LLM ever sees the text.
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass
class UiConfig:
    overlay: bool = True
    tray: bool = True
    sounds: bool = True
    # Show words in the overlay as you speak, by re-recognising the audio so far.
    live_text: bool = True
    # How often to refresh that partial transcript.
    live_interval_ms: int = 700
    # Overlay position: "bottom", "top", "bottom-right", "top-right".
    position: str = "bottom"
    margin: int = 90
    sound_volume: float = 0.25


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG_PATH
        raw: dict = {}
        if path.exists():
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
        cfg = cls()
        for f in fields(cls):
            section = raw.get(f.name)
            if not isinstance(section, dict):
                continue
            target = getattr(cfg, f.name)
            known = {sf.name for sf in fields(target)}
            for key, value in section.items():
                if key in known:
                    setattr(target, key, value)
        return cfg

    def resolved_model(self) -> str:
        return self.asr.parakeet_model if self.asr.backend == "parakeet-cli" else self.asr.model

    def check(self) -> list[str]:
        """Return human-readable problems. Empty list means good to go."""
        problems: list[str] = []

        model = Path(self.resolved_model()).expanduser()
        if not model.exists():
            problems.append(f"model not found: {model}")

        if self.asr.backend == "whisper-server":
            binary = self.asr.whisper_server_bin
        elif self.asr.backend == "parakeet-cli":
            binary = self.asr.parakeet_cli_bin
        else:
            problems.append(f"unknown asr.backend: {self.asr.backend!r}")
            binary = None
        if binary and not (Path(binary).is_file() or shutil.which(binary)):
            problems.append(f"{binary} not on PATH (pacman -S whisper-cpp ggml-vulkan)")

        if not shutil.which("pw-record"):
            problems.append("pw-record not found (install pipewire-audio / pipewire-tools)")

        if self.output.mode == "type" and not shutil.which("wtype"):
            problems.append("wtype not found (pacman -S wtype)")
        if self.output.mode == "clipboard" and not shutil.which("wl-copy"):
            problems.append("wl-copy not found (pacman -S wl-clipboard)")

        return problems
