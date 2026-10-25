"""Tools the executor can call, and the dispatcher that runs them on the Mac.

Ordered by how much they reduce steps and grounding errors:
  ui / find / menu   - act on real UI elements by id or label (no pixel guessing)
  open / applescript / shell - one call instead of many GUI steps
  wait_for           - wait on a condition instead of sleeping and re-looking
  computer           - pixel-level mouse/keyboard/screenshot (the universal fallback)
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

from . import outline as ol
from .config import Settings
from .env import MacEnv
from .mac import apps
from .mac.ax import Node
from .providers.jev import Jev
from .safety import Aborted, Policy, Verdict, classify_key, classify_label, classify_script, classify_shell, kill_switch_engaged
from .types import ToolCall, ToolResult, ToolSpec

COMPUTER_ACTIONS = [
    "screenshot", "left_click", "right_click", "middle_click", "double_click", "triple_click", "left_click_drag",
    "mouse_move", "scroll", "type", "key", "hold_key", "wait", "zoom", "cursor_position", "left_mouse_down", "left_mouse_up",
]
UI_ACTIONS = ["press", "set_value", "focus", "show_menu", "increment", "decrement", "confirm", "cancel", "raise", "scroll_to_visible"]
MUTATING = {
    "write_file", "left_click", "right_click", "middle_click", "double_click", "triple_click", "left_click_drag", "scroll", "type", "key",
    "hold_key", "left_mouse_down", "left_mouse_up", "ui", "menu", "open", "applescript", "shell",
}
CONTROL = {"done", "escalate"}


TRUE, FALSE = {"true", "yes", "1"}, {"false", "no", "0"}


def coerce(value: Any, schema: dict[str, Any]) -> Any:
    """Bring a value to its schema type. Providers without schema-enforced tool calls (the
    subscription CLI passes free-form JSON) send "false" or "3"; the string "false" once
    counted as a truthy success flag."""
    kind = schema.get("type")
    if kind == "boolean" and isinstance(value, str) and value.strip().lower() in TRUE | FALSE:
        return value.strip().lower() in TRUE
    if kind == "integer" and isinstance(value, (str, float)):
        try:
            return int(float(value))
        except ValueError:
            return value
    if kind == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    if kind == "array" and isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [coerce(v, schema["items"]) for v in value]
    return value


def coerce_args(args: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    props = (schema or {}).get("properties") or {}
    return {k: coerce(v, props[k]) if k in props else v for k, v in args.items()}


def _coord_doc(space: str, w: int, h: int) -> str:
    if space == "norm1000":
        return "Coordinates are normalised 0-1000 on both axes: [0,0] is the top-left of the screenshot, [1000,1000] the bottom-right."
    return f"Coordinates are pixels in the latest screenshot ({w}x{h}), origin top-left."


def build_specs(settings: Settings, coord_space: str, size: tuple[int, int], planner_mode: bool, ax: bool = True) -> list[ToolSpec]:
    w, h = size
    coords = _coord_doc(coord_space, w, h)
    pair = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}
    specs = [
        ToolSpec(
            "computer",
            "Mouse, keyboard and screen on macOS. " + coords + " `key` takes xdotool-style names joined with '+' "
            "(cmd+c, cmd+shift+t, Return, Escape, Tab, Page_Down); on a Mac copy/paste/save use cmd. "
            "`text` on clicks/scroll holds modifier keys (shift, cmd, alt, ctrl). Prefer the `ui` tool when the target is listed.",
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": COMPUTER_ACTIONS},
                    "coordinate": {**pair, "description": "[x, y] target for clicks, mouse_move, scroll; end point for left_click_drag"},
                    "start_coordinate": {**pair, "description": "[x, y] start for left_click_drag"},
                    "text": {"type": "string", "description": "text to type, key combo for key/hold_key, or modifiers for clicks/scroll"},
                    "scroll_direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "scroll_amount": {"type": "integer", "description": "wheel clicks, default 3"},
                    "duration": {"type": "number", "description": "seconds for wait/hold_key"},
                    "region": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4, "description": "[x0, y0, x1, y1] for zoom"},
                    "repeat": {"type": "integer", "description": "times to press the key"},
                },
                "required": ["action"],
            },
        ),
        ToolSpec(
            "ui",
            "Act on an accessibility element from the latest element list, by `id`, or by `target` (its visible label, "
            "e.g. \"Save\" or \"Search field\"). Deterministic and works even if the element is covered: the preferred "
            "way to press buttons, links, checkboxes, menu buttons, to fill text fields, and to choose an option in a popup "
            "or dropdown (action set_value with the option's text).",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "element id from the latest list"},
                    "target": {"type": "string", "description": "label to match when you have no id"},
                    "action": {"type": "string", "enum": UI_ACTIONS, "description": "default press"},
                    "value": {"type": "string", "description": "text for set_value"},
                },
            },
        ),
        ToolSpec(
            "find",
            "Search the whole accessibility tree of the frontmost app (all windows, including elements the list "
            "omitted) for elements matching text. Returns ids you can use with `ui`.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "role": {"type": "string", "description": "optional: button, field, link, checkbox, row, cell, text, menuitem, popup, tab"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        ),
        ToolSpec(
            "menu",
            "Use the frontmost app's menu bar. `path` [] lists menus; [\"File\"] lists File's items with keyboard "
            "shortcuts; [\"File\", \"Export as PDF…\"] chooses that item (ellipses and case don't matter).",
            {"type": "object", "properties": {"path": {"type": "array", "items": {"type": "string"}}}, "required": ["path"]},
        ),
        ToolSpec(
            "open",
            "Open and bring to front an app (\"Safari\"), a URL (\"https://...\") or a file/folder path (\"~/Downloads\"). "
            "Optional `app` opens the target with that app.",
            {"type": "object", "properties": {"target": {"type": "string"}, "app": {"type": "string"}}, "required": ["target"]},
        ),
        ToolSpec(
            "wait_for",
            "Wait until text appears (or disappears with gone=true) in the frontmost app's UI, or until `app` is "
            "frontmost. Faster and more reliable than fixed waits.",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "app": {"type": "string"},
                    "gone": {"type": "boolean"},
                    "timeout": {"type": "number", "description": "seconds, default 8"},
                },
            },
        ),
    ]
    if not ax:
        # Without Accessibility permission only pixel actions and app launching work.
        specs = [sp for sp in specs if sp.name in {"computer", "open"}]
    if not settings.gui_only:
        specs += [
            ToolSpec(
                "applescript",
                "Run AppleScript via osascript. Best for scriptable apps (Finder, Safari, Mail, Notes, Music, "
                "Reminders, Calendar, System Events) when it replaces several GUI steps. Returns the result.",
                {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
            ),
            ToolSpec(
                "write_file",
                "Write text to a file, creating parent folders. Use this (not shell redirection or an editor) when the "
                "task just needs a file with certain contents. Files outside the allowed workspace may need approval.",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "text": {"type": "string"}, "append": {"type": "boolean"}},
                    "required": ["path", "text"],
                },
            ),
            ToolSpec(
                "shell",
                "Run a zsh command (30 s timeout) and get its output. For reading files, finding paths (mdfind), "
                "checking state. Changes to the system may need user approval; to create a file use write_file.",
                {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}}, "required": ["command"]},
            ),
        ]
    if settings.interactive:
        specs.append(ToolSpec(
            "ask_user", "Ask the user a question when the task is ambiguous or needs information only they have.",
            {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
        ))
    specs.append(ToolSpec(
        "done",
        ("Finish the current step. " if planner_mode else "Finish the task. ")
        + "Call it only after you have seen evidence on screen that it worked. Put any requested information in `answer`.",
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "what you did and the evidence it worked"},
                "answer": {"type": "string", "description": "information the user asked for, if any"},
                "success": {"type": "boolean", "description": "true only if the task was actually accomplished; false if it was impossible or you could not do it"},
            },
            "required": ["summary", "success"],
        },
    ))
    if planner_mode:
        specs.append(ToolSpec(
            "escalate",
            "Hand control back to the planner when you are stuck, the plan is wrong, or something unexpected needs a decision.",
            {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        ))
    return specs


class Toolbox:
    def __init__(
        self,
        env: MacEnv,
        settings: Settings,
        policy: Policy,
        coord_space: str = "pixels",
        jev: Jev | None = None,
        ask: Callable[[str], str] | None = None,
    ):
        self.env = env
        self.s = settings
        self.policy = policy
        self.space = coord_space
        self.jev = jev if jev and jev.available else None
        self.ask = ask
        self.signal: tuple[str, dict[str, Any]] | None = None
        self.allow_host = False
        self.schemas: dict[str, dict[str, Any]] = {}  # tool name -> JSON schema, for argument coercion

    # -- helpers -------------------------------------------------------------------------
    def _pt(self, coord: Any) -> tuple[float, float] | None:
        if coord is None:
            return None
        if not (isinstance(coord, (list, tuple)) and len(coord) == 2):
            raise ValueError("coordinate must be [x, y]")
        return self.env.to_points(float(coord[0]), float(coord[1]), self.space)

    CONSENT_APPS = {"UserNotificationCenter", "universalAccessAuthWarn", "CoreServicesUIAgent", "SecurityAgent", "loginwindow"}

    EDITABLE = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXWebArea", "AXDateField"}

    def _await_editable(self, timeout: float | None = None) -> None:
        """Typing right after a key that opens an editor (cmd+shift+n, rename, a search box)
        races the UI: keystrokes land before the field exists. Wait briefly for one."""
        prof = self.s.profile
        if not prof.await_editable:
            return
        timeout = prof.editable_timeout if timeout is None else timeout
        if not self.env.use_ax:
            time.sleep(min(0.15, timeout))
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            el = self.env.ax.attr(self.env.ax.system, "AXFocusedUIElement")
            role = self.env.ax.attr(el, "AXRole") if el is not None else None
            if role in self.EDITABLE or (role and self.env.ax.attr(el, "AXEditable")):
                return
            time.sleep(0.05)

    def _choose_from_popup(self, node: Node, value: str) -> ToolResult | None:
        """Open a popup and press the matching menu item by its Accessibility title."""
        ax = self.env.ax
        if ax.perform(node.ref, "AXPress") != 0:
            return None
        deadline = time.monotonic() + 1.0
        items: list[tuple[str, Any]] = []
        while time.monotonic() < deadline and not items:
            time.sleep(0.08)
            for menu in ax.attr(node.ref, "AXChildren") or []:
                for item in ax.attr(menu, "AXChildren") or []:
                    title = ax.attr(item, "AXTitle")
                    if title:
                        items.append((str(title), item))
        target = next((it for t, it in items if ol.norm(t) == ol.norm(value)), None) or \
            next((it for t, it in items if ol.norm(value) in ol.norm(t)), None)
        if target is None:
            self.env.input.key("Escape")
            names = ", ".join(t for t, _ in items[:15])
            return ToolResult("", False, f"{value!r} is not an option" + (f"; options: {names}" if names else "") + ".")
        ax.perform(target, "AXPress")
        time.sleep(0.15)
        got = ax.attr(node.ref, "AXValue") or ax.attr(node.ref, "AXTitle")
        ok = got is None or ol.norm(str(got)) == ol.norm(value) or ol.norm(value) in ol.norm(str(got))
        return ToolResult("", ok, f"Chose {value!r} in {ol.describe(node)}" + ("." if ok else f", but it now shows {got!r}."))

    def _settled_value(self, node: Node, expected: str, timeout: float = 0.5):
        """Read a field back after typing; web views apply keystrokes asynchronously, so an
        immediate read can miss the last characters (seen: 'mara Okafo')."""
        if node.ref is None:
            return None
        deadline = time.monotonic() + timeout
        got = self.env.ax.attr(node.ref, "AXValue")
        while got is not None and str(got).strip() != str(expected).strip() and time.monotonic() < deadline:
            time.sleep(0.05)
            got = self.env.ax.attr(node.ref, "AXValue")
        return got

    def _await_focus(self, node: Node, timeout: float = 0.5) -> bool:
        if node.ref is None or not self.env.use_ax:
            time.sleep(0.1)
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.env.ax.attr(node.ref, "AXFocused"):
                return True
            time.sleep(0.04)
        return False

    def _guard_consent(self) -> str | None:
        """macOS permission and security dialogs are the user's to answer, never the agent's."""
        _, name, _ = self.env.frontmost()
        if name in self.CONSENT_APPS:
            if msg := self._check(Verdict("confirm", f"answering a macOS {name} permission/security dialog"), "interact with system dialog"):
                return msg + " Leave this dialog for the user and continue another way, or finish with success=false."
        return None

    def _guard_keyboard(self) -> str | None:
        if msg := self._guard_consent():
            return msg
        pid, name, _ = self.env.frontmost()
        if self.env.host_pid and pid == self.env.host_pid and not self.allow_host:
            return f"Refused: {name} (the terminal running Pilot) is frontmost. Bring the target app forward first."
        return None

    def _check(self, verdict: Verdict, action: str) -> str | None:
        ok, msg = self.policy.check(verdict, action)
        return None if ok else msg

    def _hit(self, x: float, y: float) -> Node | None:
        return self.env.ax.element_at(x, y) if self.env.use_ax else None

    # -- entry point ------------------------------------------------------------------
    def execute(self, call: ToolCall) -> ToolResult:
        if "__invalid__" in call.args:
            return ToolResult(call.id, False, "Invalid JSON arguments; send valid JSON matching the schema.")
        handler = getattr(self, f"t_{call.name}", None)
        if handler is None:
            return ToolResult(call.id, False, f"Unknown tool {call.name!r}.")
        if call.name in self.schemas:
            call.args = coerce_args(call.args, self.schemas[call.name])
        if call.name in MUTATING or call.args.get("action") in MUTATING:
            if kill_switch_engaged(self.env.input.position()):
                raise Aborted("kill switch: mouse in the top-left corner")
        try:
            return handler(call)
        except Aborted:
            raise
        except Exception as e:
            return ToolResult(call.id, False, f"{type(e).__name__}: {e}")

    # -- computer -------------------------------------------------------------------------
    def t_computer(self, call: ToolCall) -> ToolResult:
        a = call.args
        act = a.get("action")
        inp = self.env.input
        if act == "screenshot":
            obs = self.env.observe(settle=True, coord_space=self.space, with_ax=self.env.use_ax)
            return ToolResult(call.id, True, "Screenshot taken.", image=obs.image, observation=obs.text())
        if act == "zoom":
            region = a.get("region")
            if not region or len(region) != 4:
                return ToolResult(call.id, False, "zoom needs region [x0, y0, x1, y1].")
            if self.space == "norm1000":
                g = self.env.geometry
                region = [region[0] * g.model_w / 1000, region[1] * g.model_h / 1000, region[2] * g.model_w / 1000, region[3] * g.model_h / 1000]
            img = self.env.screen.zoom(tuple(float(v) for v in region))
            return ToolResult(call.id, True, "Zoomed (coordinates still refer to the full screenshot).", image=self.env.image_part(img))
        if act == "cursor_position":
            x, y = self.env.to_model(*inp.position(), self.space)
            return ToolResult(call.id, True, f"X={x}, Y={y}")
        if act == "wait":
            time.sleep(min(max(float(a.get("duration", 1)), 0), 60))
            return ToolResult(call.id, True, "Waited.")

        if act in {"left_click", "right_click", "middle_click", "double_click", "triple_click"}:
            if msg := self._guard_consent():
                return ToolResult(call.id, False, msg)
            pt = self._pt(a.get("coordinate")) or inp.position()
            hit = self._hit(*pt)
            desc = ""
            if hit is not None:
                if self.env.host_pid and self.env.ax.pid_of(hit.ref) == self.env.host_pid and not self.allow_host:
                    return ToolResult(call.id, False, "Refused: that point is on the terminal running Pilot.")
                desc = ol.describe(hit)
                if act != "right_click":
                    if msg := self._check(classify_label(hit.label, hit.role, self.policy.scope), f"click {desc}"):
                        return ToolResult(call.id, False, msg)
            button = {"right_click": "right", "middle_click": "middle"}.get(act, "left")
            count = {"double_click": 2, "triple_click": 3}.get(act, 1)
            inp.click(*pt, button=button, count=count, modifiers=a.get("text"))
            time.sleep(0.12)
            return ToolResult(call.id, True, f"{act} on {desc}" if desc else f"{act} done")
        if act == "left_click_drag":
            start, end = self._pt(a.get("start_coordinate")), self._pt(a.get("coordinate"))
            if start is None or end is None:
                return ToolResult(call.id, False, "left_click_drag needs start_coordinate and coordinate.")
            inp.drag(*start, *end, modifiers=a.get("text"))
            return ToolResult(call.id, True, "Dragged.")
        if act == "mouse_move":
            pt = self._pt(a.get("coordinate"))
            if pt is None:
                return ToolResult(call.id, False, "mouse_move needs coordinate.")
            inp.move(*pt)
            return ToolResult(call.id, True, "Moved.")
        if act in {"left_mouse_down", "left_mouse_up"}:
            (inp.mouse_down if act == "left_mouse_down" else inp.mouse_up)()
            return ToolResult(call.id, True, "OK")
        if act == "scroll":
            pt = self._pt(a.get("coordinate")) or inp.position()
            inp.scroll(*pt, a.get("scroll_direction", "down"), int(a.get("scroll_amount", 3) or 3), a.get("text"))
            return ToolResult(call.id, True, "Scrolled.")
        if act == "type":
            text = a.get("text")
            if not isinstance(text, str) or not text:
                return ToolResult(call.id, False, "type needs text.")
            if msg := self._guard_keyboard():
                return ToolResult(call.id, False, msg)
            snap = self.env.last_snapshot
            if snap and snap.focused and snap.focused.secure:
                if msg := self._check(Verdict("confirm", "typing into a password field"), "type into password field"):
                    return ToolResult(call.id, False, msg)
            self._await_editable()
            how = inp.type_text(text)
            return ToolResult(call.id, True, f"Text {how}.")
        if act in {"key", "hold_key"}:
            combo = a.get("text")
            if not combo:
                return ToolResult(call.id, False, f"{act} needs text, e.g. 'cmd+s'.")
            if msg := self._guard_keyboard():
                return ToolResult(call.id, False, msg)
            if msg := self._check(classify_key(combo), f"press {combo}"):
                return ToolResult(call.id, False, msg)
            if act == "key":
                inp.key(combo, int(a.get("repeat", 1) or 1))
            else:
                inp.hold_key(combo, float(a.get("duration", 0.5)))
            time.sleep(0.08)
            return ToolResult(call.id, True, f"Pressed {combo}.")
        return ToolResult(call.id, False, f"Unknown computer action {act!r}.")

    # -- semantic UI -------------------------------------------------------------------------
    def _resolve(self, a: dict[str, Any]) -> tuple[Node | None, str]:
        if not self.env.use_ax:
            return None, "Accessibility is unavailable; use computer clicks."
        if a.get("id") is not None:
            node = self.env.registry.get(int(a["id"]))
            if node is None:
                return None, f"No element [{a['id']}] in the latest list. Take a screenshot to refresh ids."
            if self.env.ax.attr(node.ref, "AXRole") is None:
                return None, f"Element [{a['id']}] no longer exists (the UI changed). Take a screenshot to refresh ids."
            return node, ""
        target = (a.get("target") or "").strip()
        if not target:
            return None, "Give an element `id` or a `target` label."
        nodes = list(self.env.registry.values())
        ranked = ol.rank(target, nodes, limit=12)
        if not ranked or ranked[0][0] < 0.85:
            ranked = self._find_nodes(target, None, 12) or ranked
        if (a.get("action") or "press") in {"press", "set_value", "focus", "show_menu", "confirm"}:
            # A form's label text and its control often share a name ("Country"): act on the control.
            controls = [(sc, n) for sc, n in ranked if n.interactive]
            if controls and controls[0][0] >= 0.6:
                ranked = controls
        if ranked:
            best_s, best = ranked[0]
            second = ranked[1][0] if len(ranked) > 1 else 0.0
            if best_s >= 0.85 and best_s - second >= 0.08:
                return best, ""
            if self.jev and len(ranked) > 1:
                choice = self._jev_choose(target, [n for _, n in ranked])
                if choice is not None:
                    return choice, ""
            if best_s >= 0.95:
                return best, ""
        if self.jev and self.env.last_image is not None:
            node = self._jev_box(target)
            if node is not None:
                return node, ""
        if not ranked:
            return None, f"Nothing matches {target!r}. Use a screenshot and computer clicks, or `find` with other words."
        listing = "\n".join(f"[{n.id}] {ol.describe(n, self._to_model)} (match {s:.2f})" for s, n in ranked[:8])
        return None, f"{target!r} is ambiguous; choose an id:\n{listing}"

    def _to_model(self, x: float, y: float) -> tuple[int, int]:
        return self.env.to_model(x, y, self.space)

    def _jev_choose(self, target: str, nodes: list[Node]) -> Node | None:
        options = {f"e{n.id}": ol.describe(n, self._to_model) for n in nodes}
        try:
            ans = self.jev.choose(f"Which element is the best match for: {target}?", options,
                                  self.env.image_part(self.env.last_image) if self.env.last_image else None)
        except Exception:
            return None
        if ans.value in options and ans.confidence >= 0.75:
            return self.env.registry.get(int(str(ans.value)[1:]))
        return None

    def _jev_box(self, target: str) -> Node | None:
        try:
            ans = self.jev.box(target, self.env.image_part(self.env.last_image))
        except Exception:
            return None
        if not ans.value or ans.confidence < 0.75:
            return None
        l, t, r, b = ans.value
        g = self.env.geometry
        x, y = (l + r) / 2 * g.points_w, (t + b) / 2 * g.points_h
        node = self.env.ax.element_at(x, y) if self.env.use_ax else None
        if node is None:
            node = Node(role="AXUnknown", label=target, frame=(x - 1, y - 1, 2, 2))
        if node.frame is None:
            node.frame = (x - 1, y - 1, 2, 2)
        node.id = self.env.next_id
        self.env.registry[node.id] = node
        self.env.next_id += 1
        return node

    def _find_nodes(self, query: str, role: str | None, limit: int) -> list[tuple[float, Node]]:
        pid, _, _ = self.env.frontmost()
        g = self.env.geometry
        nodes: list[Node] = []
        for root in self.env.ax.search_roots(pid):
            got, _ = self.env.ax.walk(root, (g.points_w, g.points_h), max_nodes=5000, time_budget=2.0)
            nodes.extend(got)
        ranked = ol.rank(query, nodes, role=role, limit=limit)
        for _, n in ranked:
            n.id = self.env.next_id
            self.env.registry[n.id] = n
            self.env.next_id += 1
        return ranked

    def t_ui(self, call: ToolCall) -> ToolResult:
        a = call.args
        action = a.get("action") or "press"
        node, err = self._resolve(a)
        if node is None:
            return ToolResult(call.id, False, err)
        if msg := self._guard_consent():
            return ToolResult(call.id, False, msg)
        ax, inp = self.env.ax, self.env.input
        desc = f"[{node.id}] {ol.describe(node)}"
        if action in {"press", "confirm"}:
            if msg := self._check(classify_label(node.label, node.role, self.policy.scope), f"press {desc}"):
                return ToolResult(call.id, False, msg)
        if action == "set_value":
            value = a.get("value")
            if value is None:
                return ToolResult(call.id, False, "set_value needs value.")
            if node.secure:
                if msg := self._check(Verdict("confirm", "filling a password field"), f"fill {desc}"):
                    return ToolResult(call.id, False, msg)
            if node.role in {"AXPopUpButton", "AXMenuButton"} and node.ref is not None:
                # Choose from a popup through Accessibility: pixel clicks on menu items can land
                # without registering (seen live: Safari's Country <select> stayed "Choose…").
                picked = self._choose_from_popup(node, str(value))
                if picked is not None:
                    return ToolResult(call.id, picked.ok, picked.text)
            if node.ref is not None and ax.set_attr(node.ref, "AXValue", value) == 0:
                got = ax.attr(node.ref, "AXValue")
                if got is not None and str(got).strip() == str(value).strip():
                    ax.set_attr(node.ref, "AXFocused", True)
                    return ToolResult(call.id, True, f"Set {desc} to {value!r}.")
            # Fallback: focus, select all, type (works for web fields and custom controls).
            # Wait until *this* field has focus: typing early lands in the previous field
            # (seen live: the name field ended up holding the email address).
            if node.ref is not None:
                ax.set_attr(node.ref, "AXFocused", True)
            if node.center:
                inp.click(*node.center)
            focused = self._await_focus(node)
            if msg := self._guard_keyboard():
                return ToolResult(call.id, False, msg)
            inp.key("cmd+a")
            time.sleep(0.06)  # let the Cmd release land first: the next keystroke was being swallowed ('mara Okafor')
            inp.type_text(value)
            got = self._settled_value(node, value)
            if got is not None and str(got).strip() != str(value).strip():
                return ToolResult(call.id, False, f"Typed into {desc} but it now reads {str(got)[:80]!r}, not {value!r}.")
            return ToolResult(call.id, True, f"Typed {value!r} into {desc}." + ("" if focused else " (focus unconfirmed)"))
        if action == "focus":
            if node.ref is not None and ax.set_attr(node.ref, "AXFocused", True) == 0:
                return ToolResult(call.id, True, f"Focused {desc}.")
            if node.center:
                inp.click(*node.center)
                return ToolResult(call.id, True, f"Clicked to focus {desc}.")
            return ToolResult(call.id, False, f"Could not focus {desc}.")
        ax_action = {
            "press": "AXPress", "show_menu": "AXShowMenu", "increment": "AXIncrement", "decrement": "AXDecrement",
            "confirm": "AXConfirm", "cancel": "AXCancel", "raise": "AXRaise", "scroll_to_visible": "AXScrollToVisible",
        }.get(action)
        if ax_action is None:
            return ToolResult(call.id, False, f"Unknown ui action {action!r}.")
        if node.ref is not None and ax.perform(node.ref, ax_action) == 0:
            time.sleep(0.1)
            return ToolResult(call.id, True, f"{action} {desc}.")
        if action in {"press", "show_menu"} and node.center:
            if self.env.host_pid and node.ref is not None and ax.pid_of(node.ref) == self.env.host_pid and not self.allow_host:
                return ToolResult(call.id, False, "Refused: element belongs to the terminal running Pilot.")
            inp.click(*node.center, button="right" if action == "show_menu" else "left")
            time.sleep(0.1)
            return ToolResult(call.id, True, f"{action} {desc} (by clicking its centre).")
        return ToolResult(call.id, False, f"{desc} does not support {action}.")

    def t_find(self, call: ToolCall) -> ToolResult:
        if not self.env.use_ax:
            return ToolResult(call.id, False, "Accessibility is unavailable.")
        q = call.args.get("query", "")
        ranked = self._find_nodes(q, call.args.get("role"), int(call.args.get("limit") or 15))
        if not ranked:
            return ToolResult(call.id, True, f"No elements match {q!r}. Try other words, a screenshot, or zoom.")
        lines = [f"[{n.id}] {ol.describe(n, self._to_model)}" for _, n in ranked]
        return ToolResult(call.id, True, "\n".join(lines))

    def t_menu(self, call: ToolCall) -> ToolResult:
        if not self.env.use_ax:
            return ToolResult(call.id, False, "Accessibility is unavailable; use the menu with computer clicks.")
        path = [p for p in (call.args.get("path") or []) if str(p).strip()]
        ax = self.env.ax
        pid, app, _ = self.env.frontmost()
        bar = [(t, el) for t, el in ax.menu_bar_items(pid) if t]
        if not path:
            return ToolResult(call.id, True, f"{app} menus: " + ", ".join(t for t, _ in bar))

        def match(name: str, items: list[tuple[str, Any]]) -> tuple[str, Any] | None:
            q = ol.norm(name)
            for t, el in items:
                if ol.norm(t) == q:
                    return t, el
            for t, el in items:
                if ol.norm(t).startswith(q):
                    return t, el
            return None

        hit = match(path[0], bar)
        if hit is None:
            return ToolResult(call.id, False, f"No menu {path[0]!r}. Menus: " + ", ".join(t for t, _ in bar))
        trail = [hit[0]]
        el = hit[1]
        for name in path[1:]:
            children = ax.menu_children(el)
            found = match(name, [(t, e) for t, e, _, _ in children])
            if found is None:
                listing = "; ".join(f"{t}{' (' + s + ')' if s else ''}{'' if en else ' [disabled]'}" for t, _, en, s in children)
                return ToolResult(call.id, False, f"No item {name!r} in {' > '.join(trail)}. Items: {listing}")
            trail.append(found[0])
            el = found[1]
        children = ax.menu_children(el)
        if children:
            listing = "; ".join(f"{t}{' (' + s + ')' if s else ''}{'' if en else ' [disabled]'}" for t, _, en, s in children)
            return ToolResult(call.id, True, f"{' > '.join(trail)}: {listing}")
        if len(trail) == 1:
            return ToolResult(call.id, False, f"{trail[0]} has no items.")
        if ax.attr(el, "AXEnabled") is False:
            return ToolResult(call.id, False, f"{' > '.join(trail)} is disabled right now.")
        if msg := self._check(classify_label(trail[-1], 'AXMenuItem', self.policy.scope), f"choose menu {' > '.join(trail)}"):
            return ToolResult(call.id, False, msg)
        if ax.perform(el, "AXPress") != 0:
            return ToolResult(call.id, False, f"Could not choose {' > '.join(trail)}.")
        time.sleep(0.15)
        return ToolResult(call.id, True, f"Chose {' > '.join(trail)} in {app}.")

    def t_open(self, call: ToolCall) -> ToolResult:
        target = call.args.get("target", "")
        if not target:
            return ToolResult(call.id, False, "open needs target.")
        before = self.env.frontmost()[0]
        app = call.args.get("app")
        if not app and os.environ.get("PILOT_BROWSER") and re.match(r"^[a-z][a-z0-9+.-]*://", target.strip(), re.I):
            app = os.environ["PILOT_BROWSER"]  # e.g. benchmarks keep web tasks out of the user's own browser
        res = apps.open_target(target, app)
        if not res.ok:
            return ToolResult(call.id, False, res.output)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            pid, name, _ = self.env.frontmost()
            if pid != before:
                break
            time.sleep(0.1)
        time.sleep(0.2)
        return ToolResult(call.id, True, f"{res.output}. Frontmost: {self.env.frontmost()[1]}.")

    def t_applescript(self, call: ToolCall) -> ToolResult:
        script = call.args.get("script", "")
        if msg := self._check(classify_script(script), "run AppleScript"):
            return ToolResult(call.id, False, msg)
        # A first script to an app raises macOS's "wants to control" Automation prompt and blocks until a human
        # answers; fail fast instead of stalling the run.
        res = apps.applescript(script, timeout=12)
        if not res.ok and ("timed out" in res.output or "-1743" in res.output or "Not authorized" in res.output):
            return ToolResult(call.id, False, f"{res.output}. AppleScript is not permitted for this app (macOS Automation "
                              "permission). Do not retry AppleScript for it: use the GUI, `ui`/`find`, or the shell instead. "
                              "If a 'wants to control' dialog is showing, press Escape.")
        return ToolResult(call.id, res.ok, res.output)

    def t_write_file(self, call: ToolCall) -> ToolResult:
        import os
        from pathlib import Path

        raw, text = call.args.get("path", ""), call.args.get("text")
        if not raw or not isinstance(text, str):
            return ToolResult(call.id, False, "write_file needs path and text.")
        path = Path(os.path.expanduser(raw)).resolve()
        roots = [Path(os.path.expanduser(r)).resolve() for r in self.s.write_roots]
        inside = any(path == r or r in path.parents for r in roots) or self.policy.scope.allows_path(str(path))
        if not inside:
            what = "overwrite" if path.exists() else "create"
            if msg := self._check(Verdict("confirm", f"{what} {path}"), f"write {path}"):
                return ToolResult(call.id, False, msg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if call.args.get("append") else "w") as f:
            f.write(text)
        return ToolResult(call.id, True, f"Wrote {len(text)} chars to {path}.")

    def t_shell(self, call: ToolCall) -> ToolResult:
        cmd = call.args.get("command", "")
        if msg := self._check(classify_shell(cmd, self.policy.scope), f"shell: {cmd}"):
            return ToolResult(call.id, False, msg)
        res = apps.shell(cmd, timeout=min(float(call.args.get("timeout") or 30), 120))
        return ToolResult(call.id, res.ok, res.output)

    def t_wait_for(self, call: ToolCall) -> ToolResult:
        a = call.args
        text, app, gone = a.get("text"), a.get("app"), bool(a.get("gone"))
        timeout = min(max(float(a.get("timeout") or 8), 0.5), 60)
        if not text and not app:
            return ToolResult(call.id, False, "wait_for needs text or app.")
        t0 = time.monotonic()
        g = self.env.geometry
        while time.monotonic() - t0 < timeout:
            ok = True
            pid, name, _ = self.env.frontmost()
            if app and app.lower() not in name.lower():
                ok = False
            if ok and text and self.env.use_ax:
                q = ol.norm(text)
                found = False
                for root in self.env.ax.search_roots(pid)[:3]:
                    nodes, _ = self.env.ax.walk(root, (g.points_w, g.points_h), max_nodes=2500, time_budget=0.8)
                    if any(q in ol.norm(n.label) or (n.value and q in ol.norm(n.value)) for n in nodes):
                        found = True
                        break
                ok = found != gone
            if ok:
                return ToolResult(call.id, True, f"Condition met after {time.monotonic() - t0:.1f}s.")
            time.sleep(0.25)
        return ToolResult(call.id, False, f"Timed out after {timeout:.0f}s waiting for {'absence of ' if gone else ''}{text or app!r}.")

    def t_ask_user(self, call: ToolCall) -> ToolResult:
        q = call.args.get("question", "")
        if not self.ask:
            return ToolResult(call.id, False, "No user is available. Use your best judgment, or finish with success=false.")
        return ToolResult(call.id, True, f"User says: {self.ask(q)}")

    def t_done(self, call: ToolCall) -> ToolResult:
        # An unstated outcome must never count as success (a summary saying "impossible"
        # with no flag once scored as done), so the model has to say which it is.
        if not isinstance(call.args.get("success"), bool):
            return ToolResult(call.id, False, "done needs `success`: true only if the task was accomplished, false if it was impossible or failed. Call done again with it.")
        self.signal = ("done", call.args)
        return ToolResult(call.id, True, "Recorded.")

    def t_escalate(self, call: ToolCall) -> ToolResult:
        self.signal = ("escalate", call.args)
        return ToolResult(call.id, True, "Escalated to the planner.")
