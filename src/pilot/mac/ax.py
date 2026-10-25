"""The macOS Accessibility (AX) layer: the agent's semantic view of the screen.

A screenshot tells a model what the screen looks like; the AX tree tells it what
every control *is* (role, label, value, enabled, focused) and exactly where it
is. Acting on AX elements (AXPress, set value, menu items) is deterministic,
works on occluded windows, and needs no pixel guessing - which is what lets a
fast, cheap model drive the Mac reliably.

Fetching is separated from formatting: `Node`/`Snapshot` are plain data, so the
outline, search and scoring logic in `pilot.outline` is unit-testable anywhere.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

ATTRS = [
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPosition", "AXSize",
    "AXEnabled", "AXFocused", "AXChildren", "AXPlaceholderValue", "AXIdentifier", "AXHelp",
    "AXSelected",
]

INTERACTIVE = {
    "AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXComboBox",
    "AXTextField", "AXTextArea", "AXLink", "AXSlider", "AXIncrementor", "AXDisclosureTriangle",
    "AXCell", "AXRow", "AXMenuItem", "AXMenuBarItem", "AXColorWell", "AXDateField", "AXTab",
    "AXSegmentedControl", "AXDockItem", "AXSwitch", "AXStepper", "AXOutlineRow",
}
TEXTUAL = {"AXStaticText", "AXHeading", "AXImage", "AXValueIndicator"}
CONTAINERS_TO_LABEL = {"AXRow", "AXCell", "AXButton", "AXLink", "AXMenuItem", "AXRadioButton", "AXCheckBox"}
SKIP_SUBTREE = {"AXMenuBar", "AXMenu"}  # menus are reached through the `menu` tool
CHROMIUM_BUNDLES = {
    "com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.canary", "com.microsoft.edgemac", "com.microsoft.edgemac.Beta",
    "com.brave.Browser", "company.thebrowser.Browser", "com.vivaldi.Vivaldi", "com.operasoftware.Opera", "org.chromium.Chromium",
}
SUBROLE_LABELS = {
    "AXCloseButton": "close window", "AXMinimizeButton": "minimize window", "AXFullScreenButton": "full screen",
    "AXZoomButton": "zoom window", "AXToolbarButton": "toolbar",
}


@dataclass
class Node:
    role: str
    subrole: str | None = None
    label: str = ""
    value: str | None = None
    frame: tuple[float, float, float, float] | None = None  # points: x, y, w, h
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    depth: int = 0
    identifier: str | None = None
    ref: Any = field(default=None, repr=False, compare=False)
    id: int = 0

    @property
    def center(self) -> tuple[float, float] | None:
        if not self.frame:
            return None
        x, y, w, h = self.frame
        return (x + w / 2, y + h / 2)

    @property
    def interactive(self) -> bool:
        return self.role in INTERACTIVE or (self.subrole or "") in {"AXSearchField", "AXSecureTextField", "AXTabButton", "AXToggle", "AXSwitch"}

    @property
    def secure(self) -> bool:
        return self.subrole == "AXSecureTextField"


@dataclass
class Snapshot:
    app: str
    pid: int
    bundle_id: str | None
    window: str
    nodes: list[Node]
    focused: Node | None = None
    truncated: bool = False
    elapsed_ms: float = 0.0
    error: str | None = None


def _s(v: Any, limit: int = 160) -> str:
    if v is None:
        return ""
    try:
        text = str(v)
    except Exception:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AX:
    """Thin, defensive wrapper over the pyobjc HIServices AX functions."""

    def __init__(self, messaging_timeout: float = 0.4):
        import ApplicationServices as AS
        import AppKit

        self.AS = AS
        self.AppKit = AppKit
        self.timeout = messaging_timeout
        self.system = AS.AXUIElementCreateSystemWide()
        AS.AXUIElementSetMessagingTimeout(self.system, messaging_timeout)
        self._web_enabled: set[int] = set()

    # -- permission ----------------------------------------------------------------
    def trusted(self, prompt: bool = False) -> bool:
        AS = self.AS
        if prompt:
            return bool(AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True}))
        return bool(AS.AXIsProcessTrusted())

    # -- primitive attribute access ---------------------------------------------------
    def attr(self, el: Any, name: str) -> Any:
        try:
            err, value = self.AS.AXUIElementCopyAttributeValue(el, name, None)
        except Exception:
            return None
        return self._unwrap(value) if err == 0 else None

    def attrs(self, el: Any, names: list[str]) -> dict[str, Any]:
        try:
            err, values = self.AS.AXUIElementCopyMultipleAttributeValues(el, names, 0, None)
        except Exception:
            err, values = -1, None
        if err != 0 or values is None:
            return {n: self.attr(el, n) for n in names}
        return {n: self._unwrap(v) for n, v in zip(names, values)}

    def _unwrap(self, v: Any) -> Any:
        if v is None:
            return None
        if "AXValue" in type(v).__name__:
            AS = self.AS
            try:
                kind = AS.AXValueGetType(v)
                if kind == AS.kAXValueCGPointType:
                    ok, p = AS.AXValueGetValue(v, kind, None)
                    return (p.x, p.y) if ok else None
                if kind == AS.kAXValueCGSizeType:
                    ok, s = AS.AXValueGetValue(v, kind, None)
                    return (s.width, s.height) if ok else None
                if kind == AS.kAXValueCGRectType:
                    ok, r = AS.AXValueGetValue(v, kind, None)
                    return (r.origin.x, r.origin.y, r.size.width, r.size.height) if ok else None
            except Exception:
                return None
            return None  # AXError placeholder from CopyMultipleAttributeValues
        return v

    def pid_of(self, el: Any) -> int | None:
        try:
            err, pid = self.AS.AXUIElementGetPid(el, None)
            return pid if err == 0 else None
        except Exception:
            return None

    def actions_of(self, el: Any) -> list[str]:
        try:
            err, names = self.AS.AXUIElementCopyActionNames(el, None)
            return list(names) if err == 0 and names else []
        except Exception:
            return []

    # -- apps -------------------------------------------------------------------------
    def frontmost(self) -> tuple[int, str, str | None]:
        """(pid, name, bundle id) of the frontmost app."""
        AppKit = self.AppKit
        app_el = self.attr(self.system, "AXFocusedApplication")
        pid = self.pid_of(app_el) if app_el is not None else None
        if pid is None:
            # Spin the run loop so NSWorkspace refreshes in a CLI process.
            AppKit.NSRunLoop.currentRunLoop().runUntilDate_(AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.01))
            ws = AppKit.NSWorkspace.sharedWorkspace()
            # The menu-bar owner is the app that receives keyboard input.
            app = ws.menuBarOwningApplication() or ws.frontmostApplication()
            return (int(app.processIdentifier()), str(app.localizedName()), app.bundleIdentifier())
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        name = str(app.localizedName()) if app else f"pid {pid}"
        return (int(pid), name, app.bundleIdentifier() if app else None)

    def app_element(self, pid: int) -> Any:
        el = self.AS.AXUIElementCreateApplication(pid)
        self.AS.AXUIElementSetMessagingTimeout(el, self.timeout)
        if pid not in self._web_enabled:
            # Electron builds its web AX tree for AXManualAccessibility; Chromium browsers only
            # for AXEnhancedUserInterface (what VoiceOver sets) - measured on Edge: page content
            # appeared a few seconds after the latter, never after the former.
            try:
                self.AS.AXUIElementSetAttributeValue(el, "AXManualAccessibility", True)
                app = self.AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if app is not None and (app.bundleIdentifier() or "") in CHROMIUM_BUNDLES:
                    self.AS.AXUIElementSetAttributeValue(el, "AXEnhancedUserInterface", True)
            except Exception:
                pass
            self._web_enabled.add(pid)
        return el

    # -- tree walk ----------------------------------------------------------------------
    def _node(self, el: Any, a: dict[str, Any], depth: int) -> Node:
        role = _s(a.get("AXRole")) or "AXUnknown"
        value = a.get("AXValue")
        label = _s(a.get("AXTitle")) or _s(a.get("AXDescription"))
        if not label and role in TEXTUAL:
            label = _s(value, 200)
            value = None
        if not label:
            label = _s(a.get("AXPlaceholderValue")) or _s(a.get("AXHelp"), 80)
        pos, size = a.get("AXPosition"), a.get("AXSize")
        frame = (pos[0], pos[1], size[0], size[1]) if pos and size else None
        subrole = _s(a.get("AXSubrole")) or None
        if not label and subrole in SUBROLE_LABELS:
            label = SUBROLE_LABELS[subrole]
        val_s: str | None
        if subrole == "AXSecureTextField":
            val_s = "•••" if value else ""
        elif isinstance(value, bool):
            val_s = "on" if value else "off"
        elif isinstance(value, (int, float)):
            val_s = str(value)
        else:
            val_s = _s(value, 120) if value is not None else None
        return Node(
            role=role,
            subrole=subrole,
            label=label,
            value=val_s,
            frame=frame,
            enabled=a.get("AXEnabled") is not False,
            focused=bool(a.get("AXFocused")),
            selected=bool(a.get("AXSelected")),
            depth=depth,
            identifier=_s(a.get("AXIdentifier")) or None,
            ref=el,
        )

    def walk(
        self,
        root: Any,
        screen: tuple[float, float],
        max_nodes: int = 1800,
        time_budget: float = 1.2,
        max_depth: int = 40,
        keep_all: bool = False,
    ) -> tuple[list[Node], bool]:
        """Depth-first walk in visual order; returns meaningful nodes."""
        out: list[Node] = []
        visited = 0
        deadline = time.monotonic() + time_budget
        truncated = False
        sw, sh = screen
        stack: list[tuple[Any, int, Node | None]] = [(root, 0, None)]
        while stack:
            if visited >= max_nodes or time.monotonic() > deadline:
                truncated = True
                break
            el, depth, labelled_parent = stack.pop()
            visited += 1
            a = self.attrs(el, ATTRS)
            node = self._node(el, a, depth)
            if node.role in SKIP_SUBTREE:
                continue
            if node.frame:
                x, y, w, h = node.frame
                if w <= 0 or h <= 0 or x > sw or y > sh or x + w < 0 or y + h < 0:
                    continue  # off-screen subtree (scrolled away, hidden)
            meaningful = keep_all or node.interactive or (node.role in TEXTUAL and node.label)
            parent_for_children = labelled_parent
            if meaningful:
                out.append(node)
                if node.role in CONTAINERS_TO_LABEL and not node.label:
                    parent_for_children = node
            elif labelled_parent is not None and node.label and not labelled_parent.label:
                labelled_parent.label = node.label
            if node.role in TEXTUAL and labelled_parent is not None and not labelled_parent.label and node.label:
                labelled_parent.label = node.label
            if depth >= max_depth:
                continue
            children = a.get("AXChildren") or []
            for child in reversed(list(children)[:400]):
                stack.append((child, depth + 1, parent_for_children))
        return out, truncated

    def snapshot(self, screen: tuple[float, float], max_nodes: int = 1800, time_budget: float = 1.2) -> Snapshot:
        t0 = time.monotonic()
        pid, name, bundle = self.frontmost()
        app = self.app_element(pid)
        window = self.attr(app, "AXFocusedWindow") or self.attr(app, "AXMainWindow")
        title = _s(self.attr(window, "AXTitle")) if window is not None else ""
        roots = [window] if window is not None else (self.attr(app, "AXChildren") or [])
        nodes: list[Node] = []
        truncated = False
        for r in roots:
            got, trunc = self.walk(r, screen, max_nodes=max_nodes, time_budget=time_budget)
            nodes.extend(got)
            truncated = truncated or trunc
        focused = None
        fe = self.attr(app, "AXFocusedUIElement")
        if fe is not None:
            focused = self._node(fe, self.attrs(fe, ATTRS), 0)
        return Snapshot(name, pid, bundle, title, nodes, focused, truncated, (time.monotonic() - t0) * 1000)

    def search_roots(self, pid: int) -> list[Any]:
        app = self.app_element(pid)
        return list(self.attr(app, "AXWindows") or []) or list(self.attr(app, "AXChildren") or [])

    def element_at(self, x: float, y: float) -> Node | None:
        try:
            err, el = self.AS.AXUIElementCopyElementAtPosition(self.system, x, y, None)
        except Exception:
            return None
        if err != 0 or el is None:
            return None
        node = self._node(el, self.attrs(el, ATTRS), 0)
        if not node.label:
            parent = self.attr(el, "AXParent")
            if parent is not None:
                p = self._node(parent, self.attrs(parent, ATTRS), 0)
                node.label = p.label
        return node

    # -- actions ------------------------------------------------------------------------
    def perform(self, el: Any, action: str) -> int:
        try:
            return int(self.AS.AXUIElementPerformAction(el, action))
        except Exception:
            return -1

    def set_attr(self, el: Any, name: str, value: Any) -> int:
        try:
            return int(self.AS.AXUIElementSetAttributeValue(el, name, value))
        except Exception:
            return -1

    # -- menu bar -----------------------------------------------------------------------
    def menu_bar_items(self, pid: int) -> list[tuple[str, Any]]:
        app = self.app_element(pid)
        bar = self.attr(app, "AXMenuBar")
        if bar is None:
            return []
        return [(_s(self.attr(c, "AXTitle")), c) for c in (self.attr(bar, "AXChildren") or [])]

    def menu_children(self, item: Any) -> list[tuple[str, Any, bool, str]]:
        """(title, element, enabled, shortcut) for the items of a menu or menu item."""
        out = []
        for menu in self.attr(item, "AXChildren") or []:
            for child in self.attr(menu, "AXChildren") or []:
                a = self.attrs(child, ["AXTitle", "AXEnabled", "AXMenuItemCmdChar", "AXMenuItemCmdModifiers"])
                title = _s(a.get("AXTitle"))
                if not title:
                    continue
                out.append((title, child, a.get("AXEnabled") is not False, _shortcut(a.get("AXMenuItemCmdChar"), a.get("AXMenuItemCmdModifiers"))))
        return out


def _shortcut(char: Any, mods: Any) -> str:
    if not char:
        return ""
    m = int(mods or 0)
    # kAXMenuItemModifier: 1 shift, 2 option, 4 control, 8 = no command
    parts = []
    if m & 4:
        parts.append("ctrl")
    if m & 2:
        parts.append("alt")
    if m & 1:
        parts.append("shift")
    if not m & 8:
        parts.append("cmd")
    c = str(char)
    names = {"\r": "Return", "\x7f": "Backspace", "\t": "Tab", "\x1b": "Escape", " ": "space"}
    return "+".join(parts + [names.get(c, c.lower() if len(c) == 1 else c)])
