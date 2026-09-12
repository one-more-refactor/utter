"""Tray indicator via StatusNotifierItem.

Wayland has no tray protocol. What bars actually implement is the KDE/freedesktop
StatusNotifierItem DBus interface, so that is what this speaks -- which means it shows
up in Waybar, niri bars like Noctalia, Plasma, and anything else hosting an SNI watcher.

Left-click toggles dictation. The right-click menu is a minimal DBusMenu, because most
hosts will not display an item that advertises a menu it cannot fetch.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import dbus
import dbus.service

SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"

ICONS = {
    "idle": "audio-input-microphone",
    "recording": "media-record",
    "working": "content-loading-symbolic",
    "failed": "dialog-error",
    "disabled": "microphone-sensitivity-muted",
}

LABELS = {
    "idle": "utter - ready",
    "recording": "utter - listening",
    "working": "utter - transcribing",
    "failed": "utter - last dictation failed",
    "disabled": "utter - unavailable",
}


class Tray(dbus.service.Object):
    """An SNI item plus the small menu hosts expect alongside it."""

    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_quit: Callable[[], None],
        bus: dbus.Bus | None = None,
    ) -> None:
        self.on_toggle = on_toggle
        self.on_quit = on_quit
        self.state = "idle"
        self.registered = False

        self.bus = bus or dbus.SessionBus()
        self.service_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        # Hand the BusName to Object.__init__ rather than holding it ourselves: the base
        # class stores it in `self._name`, so a second reference of our own would be
        # overwritten, collected, and the bus name silently released.
        bus_name = dbus.service.BusName(self.service_name, self.bus)
        super().__init__(self.bus, "/StatusNotifierItem", bus_name)
        self._menu = _Menu(self.bus, on_toggle, on_quit)

    def register(self) -> bool:
        """Ask the host to adopt this item. False if no tray host is running."""
        try:
            watcher = self.bus.get_object(WATCHER_NAME, WATCHER_PATH)
            watcher.RegisterStatusNotifierItem(
                self.service_name, dbus_interface=WATCHER_NAME
            )
            self.registered = True
        except dbus.DBusException:
            self.registered = False
        return self.registered

    def set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        try:
            self.NewIcon()
            self.NewToolTip()
        except dbus.DBusException:
            pass

    # -- properties ------------------------------------------------------------

    def _props(self) -> dict:
        return {
            "Category": "ApplicationStatus",
            "Id": "utter",
            "Title": "utter",
            "Status": "Active",
            "IconName": ICONS.get(self.state, ICONS["idle"]),
            "OverlayIconName": "",
            "AttentionIconName": "",
            "ToolTip": (
                ICONS.get(self.state, ICONS["idle"]),
                dbus.Array([], signature="(iiay)"),
                LABELS.get(self.state, "utter"),
                "Click to start or stop dictation",
            ),
            "ItemIsMenu": False,  # left-click should toggle, not open the menu
            "Menu": dbus.ObjectPath("/MenuBar"),
        }

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, _interface, prop):  # noqa: N802
        value = self._props().get(prop, "")
        if prop == "ToolTip":
            return dbus.Struct(value, signature="sa(iiay)ss")
        return value

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, _interface):  # noqa: N802
        props = self._props()
        props["ToolTip"] = dbus.Struct(props["ToolTip"], signature="sa(iiay)ss")
        return dbus.Dictionary(props, signature="sv")

    # -- methods hosts call ----------------------------------------------------

    @dbus.service.method(SNI_IFACE, in_signature="ii")
    def Activate(self, x, y):  # noqa: N802, ARG002
        self.on_toggle()

    @dbus.service.method(SNI_IFACE, in_signature="ii")
    def SecondaryActivate(self, x, y):  # noqa: N802, ARG002
        self.on_toggle()

    @dbus.service.method(SNI_IFACE, in_signature="is")
    def Scroll(self, delta, orientation):  # noqa: N802, ARG002
        return None

    @dbus.service.method(SNI_IFACE, in_signature="ii")
    def ContextMenu(self, x, y):  # noqa: N802, ARG002
        return None

    @dbus.service.signal(SNI_IFACE)
    def NewIcon(self):  # noqa: N802
        pass

    @dbus.service.signal(SNI_IFACE)
    def NewToolTip(self):  # noqa: N802
        pass

    @dbus.service.signal(SNI_IFACE)
    def NewStatus(self, status):  # noqa: N802
        pass


class _Menu(dbus.service.Object):
    """The smallest DBusMenu that real tray hosts accept."""

    ITEMS = ((1, "Start / stop dictation"), (2, "Quit utter"))

    def __init__(self, bus, on_toggle, on_quit) -> None:
        super().__init__(bus, "/MenuBar")
        self.on_toggle = on_toggle
        self.on_quit = on_quit
        self.revision = 1

    def _item(self, ident: int, label: str):
        return dbus.Struct(
            (
                dbus.Int32(ident),
                dbus.Dictionary(
                    {"label": label, "enabled": True, "visible": True}, signature="sv"
                ),
                dbus.Array([], signature="v"),
            ),
            signature="ia{sv}av",
        )

    @dbus.service.method(MENU_IFACE, in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent_id, recursion_depth, property_names):  # noqa: N802, ARG002
        children = dbus.Array(
            [self._item(i, label) for i, label in self.ITEMS], signature="v"
        )
        root = dbus.Struct(
            (
                dbus.Int32(0),
                dbus.Dictionary({"children-display": "submenu"}, signature="sv"),
                children,
            ),
            signature="ia{sv}av",
        )
        return dbus.UInt32(self.revision), root

    @dbus.service.method(MENU_IFACE, in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, property_names):  # noqa: N802, ARG002
        out = [
            dbus.Struct(
                (dbus.Int32(i), dbus.Dictionary({"label": label}, signature="sv")),
                signature="ia{sv}",
            )
            for i, label in self.ITEMS
            if not ids or i in ids
        ]
        return dbus.Array(out, signature="(ia{sv})")

    @dbus.service.method(MENU_IFACE, in_signature="isvu")
    def Event(self, ident, event_id, data, timestamp):  # noqa: N802, ARG002
        if event_id != "clicked":
            return
        if int(ident) == 1:
            self.on_toggle()
        elif int(ident) == 2:
            self.on_quit()

    @dbus.service.method(MENU_IFACE, in_signature="i", out_signature="b")
    def AboutToShow(self, ident):  # noqa: N802, ARG002
        return False

    @dbus.service.signal(MENU_IFACE, signature="ui")
    def LayoutUpdated(self, revision, parent):  # noqa: N802
        pass
