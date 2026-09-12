"""A small pill that shows the microphone is open. That is all it does.

No labels, no hints, no transcript. You already know what you said -- the only thing
worth showing is that it is listening, and that it can hear you.

Deliberately non-interactive: keyboard mode is NONE, so it can never take focus and
swallow the keystrokes we synthesise.
"""

from __future__ import annotations

import math

from ._layershell import preload

preload()  # must precede the Gtk import; see _layershell for why

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")

from gi.repository import GLib, Gtk, Gtk4LayerShell as LayerShell  # noqa: E402

BARS = 5
# Centre bars react most, so the shape reads as a voice rather than a graph.
WEIGHTS = (0.55, 0.85, 1.0, 0.85, 0.55)
FLOOR = 0.14  # resting height, so the pill never looks dead
RISE, FALL = 0.55, 0.12  # attack fast, release slow

LISTENING = (0.31, 0.77, 0.80)
WORKING = (0.85, 0.61, 0.27)
FAILED = (0.89, 0.47, 0.42)

CSS = b"""
.utter-pill {
  background: alpha(#0b0e10, 0.88);
  border-radius: 999px;
  padding: 9px 16px;
  box-shadow: 0 4px 18px alpha(black, 0.4);
}
"""


class Overlay:
    def __init__(self, position: str = "bottom", margin: int = 90) -> None:
        self.level = 0.0
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

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.window.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.area = Gtk.DrawingArea()
        self.area.set_content_width(64)
        self.area.set_content_height(20)
        self.area.set_draw_func(self._draw)

        pill = Gtk.Box()
        pill.add_css_class("utter-pill")
        pill.append(self.area)
        self.window.set_child(pill)

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
        colour = {"working": WORKING, "failed": FAILED}.get(self.state, LISTENING)
        slot = width / BARS
        bar_w = min(4.0, slot * 0.45)
        mid = height / 2

        for i in range(BARS):
            if self.state == "working":
                # A gentle travelling pulse, so waiting still looks alive.
                mag = FLOOR + 0.45 * (0.5 + 0.5 * math.sin(self._phase - i * 0.7))
            else:
                mag = FLOOR + self.level * WEIGHTS[i] * (1.0 - FLOOR)
            h = max(bar_w, mag * height)
            x = i * slot + (slot - bar_w) / 2
            ctx.set_source_rgba(*colour, 0.55 + 0.45 * mag)
            _rounded(ctx, x, mid - h / 2, bar_w, h, bar_w / 2)
            ctx.fill()

    # -- state -----------------------------------------------------------------

    def show(self) -> None:
        self._cancel_hide()
        self.level = 0.0
        self.state = "recording"
        self.window.present()
        self._start_anim()

    def set_level(self, level: float) -> None:
        # Asymmetric smoothing: jump to a loud sound, ease back down from it.
        target = min(1.0, max(0.0, level))
        k = RISE if target > self.level else FALL
        self.level += (target - self.level) * k

    def set_state(self, state: str) -> None:
        self.state = state
        self.area.queue_draw()

    def finish(self, state: str = "done", delay_ms: int = 160) -> None:
        """Get out of the way. Success needs no celebration; failure lingers briefly."""
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
        if self.state != "working":
            # Keep easing toward silence even when no audio callback arrives.
            self.level += (0.0 - self.level) * FALL * 0.5
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


def _rounded(ctx, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2, h / 2)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()
