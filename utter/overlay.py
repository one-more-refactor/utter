"""The on-screen pill: a small layer-shell window showing capture state and live level.

Deliberately non-interactive. Keyboard mode is NONE, which is not cosmetic: if this
window could take focus, the synthesised keystrokes would land in it instead of in
whatever you were dictating into.
"""

from __future__ import annotations

from ._layershell import preload

preload()  # must precede the Gtk import; see _layershell for why

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")

from gi.repository import GLib, Gtk, Gtk4LayerShell as LayerShell  # noqa: E402

BARS = 34
DECAY = 0.82  # how fast an idle meter falls back to the baseline

CSS = b"""
.utter-pill {
  background: alpha(#12161a, 0.92);
  border: 1px solid alpha(#4fc4cc, 0.28);
  border-radius: 17px;
  padding: 9px 16px;
  box-shadow: 0 6px 24px alpha(black, 0.35);
}
.utter-state {
  color: #ebeff0;
  font-family: "Archivo", "Inter", sans-serif;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
}
.utter-time {
  color: #7e8a90;
  font-family: monospace;
  font-size: 11px;
}
.utter-pill.recording { border-color: alpha(#4fc4cc, 0.85); }
.utter-pill.working   { border-color: alpha(#d99b45, 0.75); }
.utter-pill.failed    { border-color: alpha(#e2796c, 0.85); }
"""

# (label, css class, meter colour)
STATES = {
    "recording": ("LISTENING", "recording", (0.31, 0.77, 0.80)),
    "working": ("TRANSCRIBING", "working", (0.85, 0.61, 0.27)),
    "done": ("INSERTED", "", (0.31, 0.77, 0.80)),
    "failed": ("FAILED", "failed", (0.89, 0.47, 0.42)),
}


class Overlay:
    """A pill anchored to a screen edge. Call show()/set_level()/finish()/hide()."""

    def __init__(self, position: str = "bottom", margin: int = 90) -> None:
        self.levels = [0.0] * BARS
        self.state = "recording"
        self._hide_timer: int | None = None

        self.window = Gtk.Window()
        self.window.set_decorated(False)
        self.window.add_css_class("utter-root")

        LayerShell.init_for_window(self.window)
        LayerShell.set_layer(self.window, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self.window, "utter")
        # The crucial line: never accept keyboard focus.
        LayerShell.set_keyboard_mode(self.window, LayerShell.KeyboardMode.NONE)
        self._anchor(position, margin)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.window.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.pill.add_css_class("utter-pill")
        self.pill.add_css_class("recording")

        self.state_label = Gtk.Label(label="LISTENING")
        self.state_label.add_css_class("utter-state")

        self.meter = Gtk.DrawingArea()
        self.meter.set_content_width(168)
        self.meter.set_content_height(22)
        self.meter.set_draw_func(self._draw_meter)

        self.time_label = Gtk.Label(label="0.0s")
        self.time_label.add_css_class("utter-time")

        self.pill.append(self.state_label)
        self.pill.append(self.meter)
        self.pill.append(self.time_label)
        self.window.set_child(self.pill)

    def _anchor(self, position: str, margin: int) -> None:
        edges = LayerShell.Edge
        vertical = edges.TOP if position.startswith("top") else edges.BOTTOM
        LayerShell.set_anchor(self.window, vertical, True)
        LayerShell.set_margin(self.window, vertical, margin)
        if position.endswith("-right"):
            LayerShell.set_anchor(self.window, edges.RIGHT, True)
            LayerShell.set_margin(self.window, edges.RIGHT, 24)

    # -- drawing ---------------------------------------------------------------

    def _draw_meter(self, _area, ctx, width: int, height: int) -> None:
        r, g, b = STATES.get(self.state, STATES["recording"])[2]
        slot = width / BARS
        bar_w = max(1.5, slot * 0.55)
        mid = height / 2
        for i, level in enumerate(self.levels):
            # Baseline keeps a visible resting line instead of an empty box.
            mag = max(0.06, min(1.0, level))
            h = mag * (height - 3)
            x = i * slot + (slot - bar_w) / 2
            ctx.set_source_rgba(r, g, b, 0.35 + 0.6 * mag)
            _rounded(ctx, x, mid - h / 2, bar_w, h, bar_w / 2)
            ctx.fill()

    # -- state -----------------------------------------------------------------

    def show(self) -> None:
        self._cancel_hide()
        self.levels = [0.0] * BARS
        self.set_state("recording")
        self.window.present()

    def set_state(self, state: str) -> None:
        self.state = state
        label, css, _ = STATES.get(state, STATES["recording"])
        self.state_label.set_text(label)
        for name in ("recording", "working", "failed"):
            self.pill.remove_css_class(name)
        if css:
            self.pill.add_css_class(css)
        self.meter.queue_draw()

    def set_level(self, level: float) -> None:
        """Push one sample onto the scrolling meter. Safe to call from any thread."""
        self.levels = self.levels[1:] + [level]
        self.meter.queue_draw()

    def decay(self) -> None:
        """Let the meter fall while we are not capturing."""
        self.levels = self.levels[1:] + [self.levels[-1] * DECAY]
        self.meter.queue_draw()

    def set_elapsed(self, seconds: float) -> None:
        self.time_label.set_text(f"{seconds:4.1f}s")

    def set_note(self, note: str) -> None:
        self.state_label.set_text(note.upper())

    def finish(self, state: str = "done", delay_ms: int = 700) -> None:
        """Show a terminal state briefly, then hide."""
        self.set_state(state)
        self._cancel_hide()
        self._hide_timer = GLib.timeout_add(delay_ms, self._hide_now)

    def hide(self) -> None:
        self._cancel_hide()
        self._hide_now()

    def _hide_now(self) -> bool:
        self.window.set_visible(False)
        self._hide_timer = None
        return GLib.SOURCE_REMOVE

    def _cancel_hide(self) -> None:
        if self._hide_timer is not None:
            GLib.source_remove(self._hide_timer)
            self._hide_timer = None


def _rounded(ctx, x: float, y: float, w: float, h: float, r: float) -> None:
    import math

    r = min(r, w / 2, h / 2)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()
