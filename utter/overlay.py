"""Five dots at the bottom of the screen. They move when you talk, and fade when you
don't. That is the entire interface.

No background, no border, no text. Deliberately non-interactive: keyboard mode is NONE,
so it can never take focus and swallow the keystrokes we synthesise.
"""

from __future__ import annotations

import math

from ._layershell import preload

preload()  # must precede the Gtk import; see _layershell for why

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")

from gi.repository import GLib, Gtk, Gtk4LayerShell as LayerShell  # noqa: E402

DOTS = 5
WEIGHTS = (0.5, 0.8, 1.0, 0.8, 0.5)  # centre reacts most, so it reads as a voice
DOT = 3.0  # radius at rest
SPACING = 11.0
HEIGHT = 18

RISE, FALL = 0.55, 0.12  # level: snap up, ease down
FADE_IN, FADE_OUT = 0.40, 0.06  # opacity: appear at once, leave gently
QUIET = 0.03  # below this you are not talking, so the dots go

LISTENING = (0.85, 0.90, 0.92)
WORKING = (0.85, 0.61, 0.27)
FAILED = (0.89, 0.47, 0.42)


class Overlay:
    def __init__(self, position: str = "bottom", margin: int = 22) -> None:
        self.level = 0.0
        self.alpha = 0.0
        self.state = "recording"
        self._phase = 0.0
        self._anim: int | None = None
        self._hide_timer: int | None = None

        self.window = Gtk.Window()
        self.window.set_decorated(False)

        LayerShell.init_for_window(self.window)
        LayerShell.set_layer(self.window, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self.window, "utter")
        LayerShell.set_keyboard_mode(self.window, LayerShell.KeyboardMode.NONE)
        self._anchor(position, margin)

        self.area = Gtk.DrawingArea()
        self.area.set_content_width(int(DOTS * SPACING))
        self.area.set_content_height(HEIGHT)
        self.area.set_draw_func(self._draw)
        self.window.set_child(self.area)

    def _anchor(self, position: str, margin: int) -> None:
        edges = LayerShell.Edge
        vertical = edges.TOP if position.startswith("top") else edges.BOTTOM
        LayerShell.set_anchor(self.window, vertical, True)
        LayerShell.set_margin(self.window, vertical, margin)
        if position.endswith("-right"):
            LayerShell.set_anchor(self.window, edges.RIGHT, True)
            LayerShell.set_margin(self.window, edges.RIGHT, 28)

    # -- drawing ---------------------------------------------------------------

    def _draw(self, _area, ctx, width: int, height: int) -> None:
        if self.alpha <= 0.01:
            return
        colour = {"working": WORKING, "failed": FAILED}.get(self.state, LISTENING)
        mid_y = height / 2
        start_x = (width - (DOTS - 1) * SPACING) / 2

        for i in range(DOTS):
            if self.state == "working":
                mag = 0.5 + 0.5 * math.sin(self._phase - i * 0.7)
            else:
                mag = self.level * WEIGHTS[i]
            r = DOT + mag * (height / 2 - DOT)
            ctx.set_source_rgba(*colour, self.alpha * (0.45 + 0.55 * mag))
            ctx.arc(start_x + i * SPACING, mid_y, r, 0, 2 * math.pi)
            ctx.fill()

    # -- state -----------------------------------------------------------------

    def show(self) -> None:
        self._cancel_hide()
        self.level = 0.0
        self.alpha = 0.0  # stay invisible until there is something to hear
        self.state = "recording"
        self.window.present()
        self._start_anim()

    def set_level(self, level: float) -> None:
        target = min(1.0, max(0.0, level))
        self.level += (target - self.level) * (RISE if target > self.level else FALL)

    def set_state(self, state: str) -> None:
        self.state = state
        self.area.queue_draw()

    def finish(self, state: str = "done", delay_ms: int = 120) -> None:
        if state == "failed":
            self.set_state("failed")
            delay_ms = 900
        self._cancel_hide()
        self._hide_timer = GLib.timeout_add(delay_ms, self._hide_now)

    def hide(self) -> None:
        self._cancel_hide()
        self._hide_now()

    # -- animation -------------------------------------------------------------

    def _start_anim(self) -> None:
        if self._anim is None:
            self._anim = GLib.timeout_add(33, self._tick)  # ~30 fps

    def _tick(self) -> bool:
        self._phase += 0.22
        if self.state == "working":
            target_alpha = 1.0
        else:
            # Quiet means gone. Speaking brings them straight back.
            self.level += (0.0 - self.level) * FALL * 0.5
            target_alpha = 1.0 if self.level > QUIET else 0.0
        k = FADE_IN if target_alpha > self.alpha else FADE_OUT
        self.alpha += (target_alpha - self.alpha) * k
        self.area.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _stop_anim(self) -> None:
        if self._anim is not None:
            GLib.source_remove(self._anim)
            self._anim = None

    def _hide_now(self) -> bool:
        self._stop_anim()
        self.window.set_visible(False)
        self._hide_timer = None
        return GLib.SOURCE_REMOVE

    def _cancel_hide(self) -> None:
        if self._hide_timer is not None:
            GLib.source_remove(self._hide_timer)
            self._hide_timer = None

    # Kept so the daemon can call them unconditionally; this UI shows neither.
    def set_text(self, text: str, partial: bool = True) -> None:
        return None

    def set_hint(self, text: str) -> None:
        return None

    def set_elapsed(self, seconds: float) -> None:
        return None
