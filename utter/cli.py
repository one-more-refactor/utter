"""Command line entry point."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from .config import CONFIG_PATH, Config, socket_path

EXAMPLE_CONFIG = """# utter configuration
# Every value here is a default -- delete what you do not need to change.

[audio]
source = "default"            # never point this at a *.monitor node
max_duration_secs = 60.0      # hard stop, so a missed stop cannot run for minutes

[asr]
backend = "whisper-server"    # resident, ~220 ms/utterance
model = "~/ai/stt/ggml-large-v3-turbo-q8_0.bin"
# backend = "parakeet-cli"    # more accurate English, ~700 ms (reloads every time)
parakeet_model = "~/ai/stt/ggml-parakeet-v3-q8.bin"
language = "en"

[output]
mode = "type"                 # or "clipboard"
type_delay_ms = 2             # 0 drops characters on some compositors
two_stage = true              # type raw immediately, replace once cleanup returns

[output.replacements]
# Deterministic fixes, applied before the LLM sees anything.
# "see three do" = "cr3do"

[cleanup]
enabled = false               # set true once plain dictation feels good
model = "huihui_ai/gemma-4-abliterated:e4b"
intensity = "light"           # off | light | heavy
vocabulary = []               # ["Authentik", "Proxmox", "niri"]

[ui]
overlay = true
tray = true
sounds = true
position = "bottom"           # bottom | top | bottom-right | top-right
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="utter",
        description="Local push-to-talk dictation for Wayland.",
    )
    parser.add_argument("-c", "--config", type=Path, help="path to config.toml")
    sub = parser.add_subparsers(dest="cmd")

    run = sub.add_parser("daemon", help="run the dictation daemon")
    run.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("toggle", help="start or stop dictation")
    sub.add_parser("start", help="start dictation")
    sub.add_parser("stop", help="stop dictation and insert the text")
    sub.add_parser("cancel", help="stop dictation and discard")
    sub.add_parser("status", help="print daemon state as JSON")
    sub.add_parser("quit", help="shut the daemon down")
    sub.add_parser("check", help="verify models, binaries and audio setup")
    sub.add_parser("init", help="write an example config file")
    sub.add_parser("sources", help="list usable microphone sources")

    args = parser.parse_args(argv)
    cmd = args.cmd or "daemon"

    if cmd == "init":
        return _init_config(args.config)

    cfg = Config.load(args.config)

    if cmd == "check":
        return _check(cfg)

    if cmd == "sources":
        return _sources()

    if cmd == "daemon":
        # Must happen before anything imports Gtk.
        from ._layershell import preload

        preload()
        from .daemon import Daemon, ensure_gtk_init

        ensure_gtk_init()
        return Daemon(cfg, verbose=getattr(args, "verbose", False)).run()

    from .daemon import send

    try:
        print(send(cmd))
    except FileNotFoundError:
        print("utter: daemon is not running (start it with `utter daemon`)", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"utter: {exc}", file=sys.stderr)
        return 1
    return 0


def _init_config(path: Path | None) -> int:
    target = path or CONFIG_PATH
    if target.exists():
        print(f"utter: {target} already exists; not overwriting")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG)
    print(f"utter: wrote {target}")
    return 0


def _check(cfg: Config) -> int:
    problems = cfg.check()
    print(f"backend      {cfg.asr.backend}")
    print(f"model        {Path(cfg.resolved_model()).expanduser()}")
    print(f"output       {cfg.output.mode} (wtype delay {cfg.output.type_delay_ms} ms)")
    print(f"cleanup      {'on, ' + cfg.cleanup.model if cfg.cleanup.enabled else 'off'}")
    print(f"socket       {socket_path()}")

    source = _default_source()
    print(f"mic          {source}")
    if source.endswith(".monitor"):
        problems.append(
            "default source is a .monitor node -- that records system output, not your voice"
        )

    for tool in ("pw-record", "wtype", "paplay"):
        if not shutil.which(tool):
            print(f"missing      {tool}")

    if problems:
        print()
        for p in problems:
            print(f"problem      {p}")
        return 1
    print("\nall good.")
    return 0


def _sources() -> int:
    from .audio import list_sources

    rows = list_sources()
    if not rows:
        print("utter: could not read sources (is pw-dump available?)")
        return 1
    default = _default_source()
    print(f"{'node.name':<62} {'serial':>7}  description")
    for name, serial, desc in rows:
        mark = " *" if name == default else "  "
        print(f"{name:<60}{mark} {serial:>7}  {desc}")
    print("\n* current default. Use a node.name, or a serial for a monitor node.")
    return 0


def _default_source() -> str:
    try:
        out = subprocess.run(
            ["pactl", "get-default-source"], capture_output=True, text=True, timeout=3
        )
        return out.stdout.strip() or "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
