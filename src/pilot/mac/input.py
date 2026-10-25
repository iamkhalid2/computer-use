"""Mouse and keyboard through Quartz events (points, not pixels).

No PyAutoGUI: Quartz events are faster, Retina-correct, layout-independent for
typing (unicode payloads), and support modifier-clicks and real double-clicks.
"""

from __future__ import annotations

import time

from .keys import KeyParseError, KeyStroke, parse_combo, parse_modifiers

_UNICODE_CHUNK = 16  # UTF-16 units per synthetic key event


class Input:
    def __init__(self, paste_threshold: int = 120):
        import AppKit
        import Quartz

        self._q = Quartz
        self._appkit = AppKit
        self.paste_threshold = paste_threshold
        self._source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)

    # -- low level -------------------------------------------------------------
    def _post(self, event) -> None:
        self._q.CGEventPost(self._q.kCGHIDEventTap, event)

    def position(self) -> tuple[float, float]:
        loc = self._q.CGEventGetLocation(self._q.CGEventCreate(None))
        return (loc.x, loc.y)

    def _mouse(self, kind, x: float, y: float, button, flags: int = 0, clicks: int = 1) -> None:
        q = self._q
        ev = q.CGEventCreateMouseEvent(self._source, kind, (x, y), button)
        if clicks > 1:
            q.CGEventSetIntegerValueField(ev, q.kCGMouseEventClickState, clicks)
        if flags:
            q.CGEventSetFlags(ev, flags)
        self._post(ev)

    # -- mouse -------------------------------------------------------------------
    def move(self, x: float, y: float) -> None:
        self._mouse(self._q.kCGEventMouseMoved, x, y, self._q.kCGMouseButtonLeft)

    def click(self, x: float, y: float, button: str = "left", count: int = 1, modifiers: str | None = None) -> None:
        q = self._q
        flags = parse_modifiers(modifiers)
        kinds = {
            "left": (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp, q.kCGMouseButtonLeft),
            "right": (q.kCGEventRightMouseDown, q.kCGEventRightMouseUp, q.kCGMouseButtonRight),
            "middle": (q.kCGEventOtherMouseDown, q.kCGEventOtherMouseUp, q.kCGMouseButtonCenter),
        }
        down, up, btn = kinds[button]
        self.move(x, y)
        time.sleep(0.02)
        for n in range(1, count + 1):
            self._mouse(down, x, y, btn, flags, n)
            self._mouse(up, x, y, btn, flags, n)
            if n < count:
                time.sleep(0.03)

    def mouse_down(self) -> None:
        x, y = self.position()
        self._mouse(self._q.kCGEventLeftMouseDown, x, y, self._q.kCGMouseButtonLeft)

    def mouse_up(self) -> None:
        x, y = self.position()
        self._mouse(self._q.kCGEventLeftMouseUp, x, y, self._q.kCGMouseButtonLeft)

    def drag(self, x0: float, y0: float, x1: float, y1: float, modifiers: str | None = None, duration: float = 0.35) -> None:
        q = self._q
        flags = parse_modifiers(modifiers)
        self.move(x0, y0)
        time.sleep(0.03)
        self._mouse(q.kCGEventLeftMouseDown, x0, y0, q.kCGMouseButtonLeft, flags)
        steps = max(8, int(duration / 0.015))
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)  # ease in/out so drop targets register hover
            self._mouse(q.kCGEventLeftMouseDragged, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, q.kCGMouseButtonLeft, flags)
            time.sleep(duration / steps)
        self._mouse(q.kCGEventLeftMouseUp, x1, y1, q.kCGMouseButtonLeft, flags)

    def scroll(self, x: float, y: float, direction: str, amount: int = 3, modifiers: str | None = None) -> None:
        q = self._q
        self.move(x, y)
        flags = parse_modifiers(modifiers)
        dy = {"up": 1, "down": -1}.get(direction, 0)
        dx = {"left": 1, "right": -1}.get(direction, 0)
        for _ in range(max(1, min(int(amount), 50))):
            ev = q.CGEventCreateScrollWheelEvent(self._source, q.kCGScrollEventUnitPixel, 2, dy * 60, dx * 60)
            if flags:
                q.CGEventSetFlags(ev, flags)
            self._post(ev)
            time.sleep(0.012)

    # -- keyboard ----------------------------------------------------------------
    def _key_event(self, keycode: int, down: bool, flags: int) -> None:
        ev = self._q.CGEventCreateKeyboardEvent(self._source, keycode, down)
        self._q.CGEventSetFlags(ev, flags)
        self._post(ev)

    def stroke(self, ks: KeyStroke, hold: float = 0.0) -> None:
        # Press modifiers physically too: some apps read key state, not flags.
        flags = 0
        for code in ks.modifier_keycodes:
            self._key_event(code, True, flags)
        flags = ks.flags
        self._key_event(ks.keycode, True, flags)
        if hold:
            time.sleep(hold)
        self._key_event(ks.keycode, False, flags if not ks.is_modifier_only else 0)
        for code in reversed(ks.modifier_keycodes):
            self._key_event(code, False, 0)

    def key(self, combo: str, repeat: int = 1) -> None:
        ks = parse_combo(combo)
        for i in range(max(1, min(repeat, 100))):
            self.stroke(ks)
            if i + 1 < repeat:
                time.sleep(0.02)

    def hold_key(self, combo: str, duration: float) -> None:
        self.stroke(parse_combo(combo), hold=min(max(duration, 0.0), 300.0))

    def _type_unicode(self, text: str) -> None:
        q = self._q
        # Split on UTF-16 code-unit boundaries without breaking surrogate pairs.
        chunks: list[str] = []
        buf = ""
        for ch in text:
            if len((buf + ch).encode("utf-16-le")) // 2 > _UNICODE_CHUNK:
                chunks.append(buf)
                buf = ""
            buf += ch
        if buf:
            chunks.append(buf)
        for chunk in chunks:
            for down in (True, False):
                ev = q.CGEventCreateKeyboardEvent(self._source, 0, down)
                q.CGEventSetFlags(ev, 0)
                q.CGEventKeyboardSetUnicodeString(ev, len(chunk.encode("utf-16-le")) // 2, chunk)
                self._post(ev)
            time.sleep(0.008)

    def _paste(self, text: str) -> None:
        pb = self._appkit.NSPasteboard.generalPasteboard()
        saved = pb.stringForType_(self._appkit.NSPasteboardTypeString)
        pb.clearContents()
        pb.setString_forType_(text, self._appkit.NSPasteboardTypeString)
        time.sleep(0.03)
        self.key("cmd+v")
        time.sleep(0.25)
        pb.clearContents()
        if saved is not None:
            pb.setString_forType_(saved, self._appkit.NSPasteboardTypeString)

    def type_text(self, text: str) -> str:
        """Type literal text. Long text is pasted (and the clipboard restored)."""
        if len(text) >= self.paste_threshold:
            self._paste(text)
            return "pasted"
        # One event per character, with the real keycode where there is one and the character
        # attached. Chunked unicode events are fastest but some controls ignore them: Safari's
        # date field only accepted digits pressed one key at a time (measured on PilotBench).
        for ch in text:
            if ch in "\n\t":
                self.key("Return" if ch == "\n" else "Tab")
            else:
                self._type_char(ch)
            time.sleep(0.006)
        return "typed"

    def _type_char(self, ch: str) -> None:
        q = self._q
        code, flags = 0, 0
        try:
            ks = parse_combo(ch if ch != " " else "space")
            code, flags = ks.keycode, ks.flags
        except KeyParseError:
            pass
        n = len(ch.encode("utf-16-le")) // 2
        for down in (True, False):
            ev = q.CGEventCreateKeyboardEvent(self._source, code, down)
            q.CGEventSetFlags(ev, flags)
            q.CGEventKeyboardSetUnicodeString(ev, n, ch)
            self._post(ev)
