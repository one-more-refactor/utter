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
    # "chord":      a modifier combination, e.g. Alt+Space. Cannot fire by accident
    #               and types no character of its own.
    # "double_tap": tap the key twice to start, again to commit (or stop talking).
    # "hold":       hold the key to talk, release to commit -- classic push-to-talk.
    mode: str = "hold"
    modifiers: list[str] = field(default_factory=lambda: ["ALT"])
    # The key. Name from KEY_CODES, or a raw keycode.
    # For "hold", prefer a key that types nothing: SCROLLLOCK, PAUSE, F13, MENU.
    key: str = "RIGHTCTRL"
    double_tap_ms: int = 320
    # Ignore taps that happen while you are actively typing: another key pressed within
    # this window before the first tap cancels it.
    guard_ms: int = 500
    # How long the key must be held before the microphone opens, in "hold" mode. Stops
    # an accidental brush from starting a dictation.
    hold_ms: int = 220
    # The trigger keys still reach the focused window (this is a passive read, not a
    # grab), so a double-tap's two taps land there as spaces and are deleted again.
    # A chord or an inert hold key types nothing, so nothing has to be deleted.
    backspace: int = 2
    # Double-tap again while dictating to commit early.
    tap_to_commit: bool = True


@dataclass
class StreamConfig:
    """Type words into the focused window while you are still speaking."""

    enabled: bool = True
    # How often to re-recognise the audio so far. The recogniser takes ~110 ms on a
    # short buffer, so 400 ms leaves headroom and keeps the commit lag low.
    interval_ms: int = 400
    # Consecutive passes that must agree before a word is typed (LocalAgreement-2).
    agree: int = 2
    # Settled words held back anyway. The trailing edge of a partial carries whisper's
    # invented punctuation; 2 is where that stops being committed.
    lag: int = 2
    # Do not start recognising until there is at least this much audio.
    min_audio_secs: float = 0.6


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
    # There is no on-screen UI. The words appearing as you speak are the feedback,
    # and a sound tells you the microphone opened. A tray icon shows state at a glance.
    tray: bool = True
    sounds: bool = True
    sound_volume: float = 0.25


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    def __post_init__(self) -> None:
        # Keys in the file that utter does not know. Reported, never fatal: a config
        # written for an older or newer version should still start.
        self.unknown: list[str] = []

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Read `path` (default ~/.config/utter/config.toml) over the defaults.

        Raises ConfigError with a readable message when the file is not valid TOML.
        """
        path = path or CONFIG_PATH
        raw: dict = {}
        if path.exists():
            try:
                with path.open("rb") as fh:
                    raw = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{path}: {exc}") from exc
        cfg = cls()
        sections = {f.name for f in fields(cls)}
        for name, section in raw.items():
            if name not in sections:
                cfg.unknown.append(f"[{name}]")
                continue
            if not isinstance(section, dict):
                cfg.unknown.append(name)
                continue
            target = getattr(cfg, name)
            known = {sf.name for sf in fields(target)}
            for key, value in section.items():
                if key in known:
                    setattr(target, key, value)
                else:
                    cfg.unknown.append(f"{name}.{key}")
        return cfg

    def resolved_model(self) -> str:
        return self.asr.parakeet_model if self.asr.backend == "parakeet-cli" else self.asr.model

    def validate(self) -> list[str]:
        """Problems with the values themselves -- wrong types, unknown choices.

        Pure: touches neither the filesystem nor PATH, so it is cheap to test.
        """
        problems: list[str] = []
        for section in fields(self):
            target = getattr(self, section.name)
            defaults = type(target)()
            for f in fields(target):
                value, default = getattr(target, f.name), getattr(defaults, f.name)
                if not _same_kind(value, default):
                    problems.append(
                        f"{section.name}.{f.name} should be {_kind(default)}, got {value!r}"
                    )
        if problems:
            return problems  # the checks below assume the types are right

        for where, value, allowed in (
            ("trigger.mode", self.trigger.mode, TRIGGER_MODES),
            ("output.mode", self.output.mode, OUTPUT_MODES),
            ("cleanup.intensity", self.cleanup.intensity, CLEANUP_INTENSITIES),
            ("asr.backend", self.asr.backend, ASR_BACKENDS),
        ):
            if value not in allowed:
                choices = ", ".join(repr(a) for a in allowed)
                problems.append(f"{where} = {value!r} is not one of {choices}")

        if self.trigger.enabled:
            from .hotkey import MODIFIER_GROUPS, resolve_key

            if resolve_key(self.trigger.key) is None:
                problems.append(
                    f"trigger.key = {self.trigger.key!r} is not a known key name "
                    "(use a name like RIGHTCTRL or SCROLLLOCK, or a raw keycode)"
                )
            if self.trigger.mode == "chord":
                bad = [m for m in self.trigger.modifiers if m.strip().upper() not in MODIFIER_GROUPS]
                if bad:
                    problems.append(f"trigger.modifiers: unknown modifier(s) {', '.join(bad)}")

        if not 0.0 <= self.audio.silence_level <= 1.0:
            problems.append("audio.silence_level must be between 0.0 and 1.0")
        if self.audio.max_duration_secs <= 0:
            problems.append("audio.max_duration_secs must be positive")
        if self.stream.interval_ms < 50:
            problems.append("stream.interval_ms below 50 would recognise continuously")
        return problems

    def check(self) -> list[str]:
        """Return human-readable problems. Empty list means good to go."""
        problems: list[str] = self.validate()
        if any(p.startswith("asr.backend") for p in problems):
            return problems

        model = Path(self.resolved_model()).expanduser()
        if not model.exists():
            problems.append(f"model not found: {model}")

        if self.asr.backend == "parakeet-cli":
            binary = self.asr.parakeet_cli_bin
        else:
            binary = self.asr.whisper_server_bin
        if not (Path(binary).is_file() or shutil.which(binary)):
            problems.append(f"{binary} not on PATH (pacman -S whisper-cpp ggml-vulkan)")

        if not shutil.which("pw-record"):
            problems.append("pw-record not found (install pipewire-audio / pipewire-tools)")

        if self.output.mode == "type" and not shutil.which("wtype"):
            problems.append("wtype not found (pacman -S wtype)")
        if self.output.mode == "clipboard" and not shutil.which("wl-copy"):
            problems.append("wl-copy not found (pacman -S wl-clipboard)")

        return problems


class ConfigError(ValueError):
    """The config file could not be read."""


TRIGGER_MODES = ("hold", "chord", "double_tap")
OUTPUT_MODES = ("type", "clipboard")
CLEANUP_INTENSITIES = ("off", "light", "heavy")
ASR_BACKENDS = ("whisper-server", "parakeet-cli")


def _same_kind(value: object, default: object) -> bool:
    """Whether a value read from TOML has the same shape as the field's default."""
    if isinstance(default, bool) or isinstance(value, bool):
        return type(value) is type(default)
    if isinstance(default, float):
        return isinstance(value, (int, float))
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if isinstance(default, dict):
        return isinstance(value, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        )
    return isinstance(value, type(default))


def _kind(default: object) -> str:
    return {
        bool: "true or false",
        int: "a whole number",
        float: "a number",
        str: "a string",
        list: "a list of strings",
        dict: "a table of strings",
    }.get(type(default), type(default).__name__)
