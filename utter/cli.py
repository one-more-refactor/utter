"""Command line entry point."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from .config import CONFIG_PATH, Config, socket_path

EXAMPLE_CONFIG = """# utter -- everything here is already the default.
# Delete what you do not change.

[trigger]
modifiers = ["ALT"]    # Alt+Space to start, Alt+Space again to insert
key = "SPACE"
# mode = "hold"        # or hold a key -- then use an inert one:
# key = "SCROLLLOCK"   # SCROLLLOCK, PAUSE, F13, MENU

[audio]
silence_ms = 1500      # pause this long and it inserts what you said

[asr]
language = "en"
model = "~/ai/stt/ggml-large-v3-turbo-q8_0.bin"

[output]
mode = "type"          # or "clipboard"

[ui]
sounds = true
position = "bottom"    # bottom | top | bottom-right | top-right
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
    k = sub.add_parser("keys", help="show which keyboards the trigger can read")
    k.add_argument("--watch", action="store_true", help="print each double-tap as it fires")

    args = parser.parse_args(argv)
    cmd = args.cmd or "daemon"

    if cmd == "init":
        return _init_config(args.config)

    cfg = Config.load(args.config)

    if cmd == "check":
        return _check(cfg)

    if cmd == "sources":
        return _sources()

    if cmd == "keys":
        return _keys(cfg, watch=getattr(args, "watch", False))

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
    if cfg.trigger.enabled:
        from .hotkey import keyboards, resolve_key

        code = resolve_key(cfg.trigger.key)
        found = keyboards(code) if code is not None else []
        print(f"trigger      {_trigger_desc(cfg)} "
              f"({len(found)} readable keyboard(s))")
        if not found:
            problems.append(
                "no readable keyboard for the trigger; run `utter keys`"
            )
    else:
        print("trigger      disabled")
    print(f"auto-stop    {'on, ' + str(cfg.audio.silence_ms) + ' ms of silence' if cfg.audio.auto_stop else 'off'}")
    print(f"live text    {'on' if cfg.ui.live_text else 'off'}")

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


def _trigger_desc(cfg) -> str:
    tc = cfg.trigger
    if tc.mode == "chord":
        return "+".join([*(m.upper() for m in tc.modifiers), tc.key])
    if tc.mode == "hold":
        return f"hold {tc.key}"
    return f"double-tap {tc.key} within {tc.double_tap_ms} ms"


def _keys(cfg, watch: bool = False) -> int:
    from .hotkey import DoubleTapListener, keyboards, resolve_key

    code = resolve_key(cfg.trigger.key)
    if code is None:
        print(f"utter: unknown trigger key {cfg.trigger.key!r}")
        return 1
    devices = keyboards(code)
    print(f"trigger      {_trigger_desc(cfg)} (keycode {code})")
    if not devices:
        print("readable     none")
        print("\nproblem      no readable keyboard. Add yourself to the 'input' group:")
        print("               sudo usermod -aG input $USER   (then log out and back in)")
        return 1
    for path, name in devices:
        print(f"readable     {path:22} {name}")

    if not watch:
        print("\nrun `utter keys --watch` and press the trigger to confirm it fires.")
        return 0

    import time

    hits = []
    if cfg.trigger.mode == "chord":
        from .hotkey import ChordListener

        listener = ChordListener(
            cfg.trigger.key, cfg.trigger.modifiers, on_trigger=lambda: hits.append(time.time())
        )
    else:
        listener = DoubleTapListener(
            cfg.trigger.key,
            cfg.trigger.double_tap_ms,
            cfg.trigger.guard_ms,
            on_trigger=lambda: hits.append(time.time()),
        )
    if not listener.start():
        print(f"utter: {listener.error}")
        return 1
    print(f"\nwatching for 20 s -- press {_trigger_desc(cfg)} now (Ctrl+C to stop)")
    seen = 0
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(0.1)
            while seen < len(hits):
                seen += 1
                print(f"  trigger #{seen} detected")
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
    print(f"\n{seen} trigger(s) detected.")
    return 0 if seen else 1


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
