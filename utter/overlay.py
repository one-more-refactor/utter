"""The on-screen panel: capture state, a live level meter, and words as you speak.

Deliberately non-interactive. Keyboard mode is NONE, which is not cosmetic: if this
window could take focus, the synthesised keystrokes would land in it instead of in
whatever you were dictating into.
"""

from __future__ import annotations

import math

from ._layershell import preload

preload()  # must precede the Gtk import; see _layershell for why

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")

from gi.repository import GLib, Gtk, Gtk4LayerShell as LayerShell, Pango  # noqa: E402

BARS = 48
IDLE_LEVEL = 0.05

CSS = b"""
.utter-panel {
  background: alpha(#0e1214, 0.93);
  border: 1px solid alpha(#4fc4cc, 0.30);
  border-radius: 20px;
  padding: 14px 20px;
  box-shadow: 0 10px 40px alpha(black, 0.45);
}
.utter-panel.recording { border-color: alpha(#4fc4cc, 0.9); }
.utter-panel.working   { border-color: alpha(#d99b45, 0.85); }
.utter-panel.done      { border-color: alpha(#4fc4cc, 0.45); }
.utter-panel.failed    { border-color: alpha(#e2796c, 0.9); }

.utter-state {
  color: #ebeff0;
  font-family: "Archivo", "Inter", sans-serif;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.14em;
}
.utter-state.working { color: #d99b45; }
.utter-state.failed  { color: #e2796c; }
.utter-time {
  color: #7e8a90;
  font-family: monospace;
  font-size: 11px;
}
.utter-text {
  color: #ebeff0;
  font-size: 15px;
}
.utter-text.partial { color: #b3bdc2; font-style: italic; }
.utter-hint {
  color: #5d686e;
  font-family: monospace;
  font-size: 10px;
  letter-spacing: 0.05em;
}
"""

# (label, css class, meter colour)
STATES = {
    "recording": ("LISTENING", "recording", (0.31, 0.77, 0.80)),
    "working": ("TRANSCRIBING", "working", (0.85, 0.61, 0.27)),
    "polishing": ("POLISHING", "working", (0.85, 0.61, 0.27)),
    "done": ("INSERTED", "done", (0.31, 0.77, 0.80)),
    "failed": ("FAILED", "failed", (0.89, 0.47, 0.42)),
}


class Overlay:
    """A panel anchored to a screen edge. Driven entirely from the GTK main thread."""

    def __init__(self, position: str = "bottom", margin: int = 90) -> None:
        self.levels = [0.0] * BARS
        self.state = "recording"
        self._hide_timer: int | None = None
        self._phase = 0.0
        self._anim: int | None = None

        self.window = Gtk.Window()
        self.window.set_decorated(False)

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

        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.panel.add_css_class("utter-panel")
        self.panel.add_css_class("recording")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.state_label = Gtk.Label(label="LISTENING")
        self.state_label.add_css_class("utter-state")
        self.meter = Gtk.DrawingArea()
        self.meter.set_content_width(240)
        self.meter.set_content_height(26)
        self.meter.set_hexpand(True)
        self.meter.set_draw_func(self._draw_meter)
        self.time_label = Gtk.Label(label="0.0s")
        self.time_label.add_css_class("utter-time")
        header.append(self.state_label)
        header.append(self.meter)
        header.append(self.time_label)

        self.text_label = Gtk.Label(label="")
        self.text_label.add_css_class("utter-text")
        self.text_label.add_css_class("partial")
        self.text_label.set_wrap(True)
        self.text_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.text_label.set_max_width_chars(58)
        self.text_label.set_xalign(0.0)
        self.text_label.set_visible(False)

        self.hint = Gtk.Label(label="double-tap space to commit  ·  stop talking to finish")
        self.hint.add_css_class("utter-hint")
        self.hint.set_xalign(0.0)

        self.panel.append(header)
        self.panel.append(self.text_label)
        self.panel.append(self.hint)
        self.window.set_child(self.panel)

    def _anchor(self, position: str, margin: int) -> None:
        edges = LayerShell.Edge
        vertical = edges.TOP if position.startswith("top") else edges.BOTTOM
        LayerShell.set_anchor(self.window, vertical, True)
        LayerShell.set_margin(self.window, vertical, margin)
        if position.endswith("-right"):
            LayerShell.set_anchor(self.window, edges.RIGHT, True)
            LayerShell.set_margin(self.window, edges.RIGHT, 28)

    # -- drawing ---------------------------------------------------------------

    def _draw_meter(self, _area, ctx, width: int, height: int) -> None:
        r, g, b = STATES.get(self.state, STATES["recording"])[2]
        slot = width / BARS
        bar_w = max(1.5, slot * 0.5)
        mid = height / 2
        working = self.state in ("working", "polishing")
        for i, level in enumerate(self.levels):
            if working:
                # A travelling wave while we wait, so the panel never looks frozen.
                mag = IDLE_LEVEL + 0.4 * max(0.0, math.sin(self._phase - i * 0.33))
            else:
                mag = max(IDLE_LEVEL, min(1.0, level))
            h = mag * (height - 4)
            x = i * slot + (slot - bar_w) / 2
            ctx.set_source_rgba(r, g, b, 0.3 + 0.65 * mag)
            _rounded(ctx, x, mid - h / 2, bar_w, h, bar_w / 2)
            ctx.fill()

    # -- state -----------------------------------------------------------------

    def show(self) -> None:
        self._cancel_hide()
        self.levels = [0.0] * BARS
        self.text_label.set_text("")
        self.text_label.set_visible(False)
        self.hint.set_visible(True)
        self.set_state("recording")
        self.window.present()

    def set_state(self, state: str) -> None:
        self.state = state
        label, css, _ = STATES.get(state, STATES["recording"])
        self.state_label.set_text(label)
        for name in ("recording", "working", "done", "failed"):
            self.panel.remove_css_class(name)
            self.state_label.remove_css_class(name)
        if css:
            self.panel.add_css_class(css)
            self.state_label.add_css_class(css)
        if state in ("working", "polishing"):
            self._start_anim()
        else:
            self._stop_anim()
        self.meter.queue_draw()

    def _start_anim(self) -> None:
        if self._anim is not None:
            return
        self._anim = GLib.timeout_add(55, self._advance)

    def _advance(self) -> bool:
        self._phase += 0.42
        self.meter.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _stop_anim(self) -> None:
        if self._anim is not None:
            GLib.source_remove(self._anim)
            self._anim = None

    def set_level(self, level: float) -> None:
        self.levels = self.levels[1:] + [level]
        self.meter.queue_draw()

    def set_hint(self, text: str) -> None:
        self.hint.set_text(text)
        self.hint.set_visible(bool(text))

    def set_elapsed(self, seconds: float) -> None:
        self.time_label.set_text(f"{seconds:4.1f}s")

    def set_text(self, text: str, partial: bool = True) -> None:
        """Show the transcript so far. Partial text is dimmed and italic."""
        if not text:
            self.text_label.set_visible(False)
            return
        self.text_label.set_text(text)
        if partial:
            self.text_label.add_css_class("partial")
        else:
            self.text_label.remove_css_class("partial")
        self.text_label.set_visible(True)

    def finish(self, state: str = "done", delay_ms: int = 850) -> None:
        self.set_state(state)
        self.hint.set_visible(False)
        self._cancel_hide()
        self._hide_timer = GLib.timeout_add(delay_ms, self._hide_now)

    def hide(self) -> None:
        self._cancel_hide()
        self._hide_now()

    def _hide_now(self) -> bool:
        self._stop_anim()
        self.window.set_visible(False)
        self._hide_timer = None
        return GLib.SOURCE_REMOVE

    def _cancel_hide(self) -> None:
        if self._hide_timer is not None:
            GLib.source_remove(self._hide_timer)
            self._hide_timer = None


def _rounded(ctx, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2, h / 2)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()
