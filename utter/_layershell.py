"""Preload libgtk4-layer-shell before GTK touches libwayland-client.

gtk4-layer-shell works by interposing on `wl_display_connect`, so it has to be in the
process before GTK initialises Wayland. A C program gets this from link order; Python
has to do it by hand, and the symptom of skipping it is a window that looks normal but
warns "GtkWindow is not a layer surface" and then behaves like an ordinary toplevel --
which for us would mean an overlay that steals keyboard focus and swallows the
keystrokes we are trying to synthesise.

Deliberately free of any gi import, so callers can invoke it before importing Gtk.
"""

from __future__ import annotations

import ctypes

SONAMES = (
    "libgtk4-layer-shell.so.0",
    "libgtk4-layer-shell.so",
)

_loaded: bool | None = None


def preload() -> bool:
    """Load the library into the global symbol namespace. Safe to call repeatedly."""
    global _loaded
    if _loaded is not None:
        return _loaded
    for soname in SONAMES:
        try:
            ctypes.CDLL(soname, mode=ctypes.RTLD_GLOBAL)
            _loaded = True
            return True
        except OSError:
            continue
    _loaded = False
    return False
