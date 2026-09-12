"""Putting text into the focused window.

Uses wtype, which speaks the `zwp_virtual_keyboard_v1` Wayland protocol. Two reasons
it is the right choice over ydotool on a wlroots compositor:

  * wtype uploads its own keymap, so it is immune to your keyboard layout. ydotool
    emits raw US scancodes -- on a German QWERTZ layout that swaps y/z and mangles
    every punctuation mark, which is fatal when dictating into a terminal.
  * It needs no uinput access, no daemon, and no `input` group membership.

The one hazard is inherent to the approach and worth stating plainly: synthesised
keystrokes go to whatever *actually* has focus, which is not always the window you
think. Never synthesise Return or Ctrl+D on a hunch.
"""

from __future__ import annotations

import shutil
import subprocess


class InjectError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("wtype") is not None


def type_text(text: str, delay_ms: int = 2) -> None:
    """Synthesise keystrokes for `text` into the focused window."""
    if not text:
        return
    if not available():
        raise InjectError("wtype not found")
    # `--` guards against a transcript that begins with a dash.
    cmd = ["wtype", "-d", str(max(0, delay_ms)), "--", text]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise InjectError((proc.stderr or "wtype failed").strip())


def backspace(count: int, delay_ms: int = 1) -> None:
    """Delete `count` characters to the left of the cursor."""
    if count <= 0:
        return
    cmd = ["wtype", "-d", str(max(0, delay_ms))]
    for _ in range(count):
        cmd += ["-k", "BackSpace"]
    subprocess.run(cmd, capture_output=True, text=True)


def replace_text(previous: str, new: str, delay_ms: int = 2) -> None:
    """Swap already-typed text for a corrected version.

    This is what makes the two-stage paste work: the raw transcript lands in ~200 ms,
    then the cleaned version replaces it once the LLM returns. If the two differ only
    by trailing characters we avoid retyping the shared prefix.
    """
    if previous == new:
        return
    shared = 0
    for a, b in zip(previous, new):
        if a != b:
            break
        shared += 1
    backspace(len(previous) - shared)
    type_text(new[shared:], delay_ms=delay_ms)


def copy_to_clipboard(text: str) -> None:
    if not shutil.which("wl-copy"):
        raise InjectError("wl-copy not found")
    # wl-copy daemonises to stay the clipboard owner. If it inherits a captured pipe,
    # that pipe never reaches EOF and waiting on it hangs forever -- so hand it
    # /dev/null and let the detached child live.
    subprocess.run(
        ["wl-copy", "--", text],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )


def deliver(text: str, mode: str = "type", delay_ms: int = 2) -> None:
    """Deliver text, falling back to the clipboard if typing is impossible."""
    if mode == "clipboard":
        copy_to_clipboard(text)
        return
    try:
        type_text(text, delay_ms=delay_ms)
    except InjectError:
        copy_to_clipboard(text)
        raise
