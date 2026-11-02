"""The background browser body: web pages in a headless Chromium-family browser Pilot owns.

It never touches the screen, the mouse or the user's own browser: the browser runs headless in
its own profile (its own cookies, signed in to nothing of the user's), driven over a private
DevTools pipe. The model reads a page as its accessibility tree, the same tree a screen reader
gets, rendered as indented lines with refs (`e42`). A ref is the element's DOM node id, so it
stays valid while that element exists, unlike screen element ids that renumber every turn.

Actions resolve a ref to the element's box and send trusted input events at its centre, so
pages see real clicks, key presses and pointer drags. A click is refused when another element
covers the target (a modal, a cookie banner): the model sees what is in the way instead of a
click that silently lands on the overlay.
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cdp import CDPError, Pipe

BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]
DEFAULT_DIR = "~/.config/pilot/browser"
VIEWPORT = (1280, 900)
FLAGS = [
    "--remote-debugging-pipe", "--no-first-run", "--no-default-browser-check", "--disable-sync", "--disable-extensions",
    "--disable-component-update", "--disable-background-networking", "--disable-default-apps", "--mute-audio",
    "--use-mock-keychain",  # a fresh profile otherwise asks the login keychain for its cookie key: a macOS dialog
    "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider",
    f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}",
]

# Roles that only group other content: their children are shown in their place.
HOIST = {"generic", "none", "presentation", "LineBreak", "InlineTextBox", "paragraph", "LabelText", "MenuListPopup", "Section"}
# Controls whose inner parts (a date field's day/month/year spinners) are noise: shown whole.
OPAQUE = {"Date", "DateTime", "InputTime", "time", "slider", "ProgressIndicator", "meter"}
FLAG_PROPS = ("checked", "selected", "pressed", "expanded", "disabled", "required", "focused", "level")
LINE_CHARS = 200

KEYS: dict[str, tuple[str, str, int, str]] = {
    "enter": ("Enter", "Enter", 13, "\r"), "return": ("Enter", "Enter", 13, "\r"), "kp_enter": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, ""), "escape": ("Escape", "Escape", 27, ""), "esc": ("Escape", "Escape", 27, ""),
    "backspace": ("Backspace", "Backspace", 8, ""), "delete": ("Delete", "Delete", 46, ""), "space": (" ", "Space", 32, " "),
    "up": ("ArrowUp", "ArrowUp", 38, ""), "down": ("ArrowDown", "ArrowDown", 40, ""), "left": ("ArrowLeft", "ArrowLeft", 37, ""),
    "right": ("ArrowRight", "ArrowRight", 39, ""), "home": ("Home", "Home", 36, ""), "end": ("End", "End", 35, ""),
    "page_up": ("PageUp", "PageUp", 33, ""), "page_down": ("PageDown", "PageDown", 34, ""),
}
for _k in list(KEYS):
    if _k in {"up", "down", "left", "right"}:
        KEYS["arrow" + _k] = KEYS[_k]
    if "_" in _k:
        KEYS[_k.replace("_", "")] = KEYS[_k]
MODIFIERS = {"alt": 1, "option": 1, "opt": 1, "ctrl": 2, "control": 2, "cmd": 4, "command": 4, "meta": 4, "super": 4, "shift": 8}
# Headless Chrome on macOS runs no editing shortcuts of its own: they must be named as commands.
MAC_COMMANDS = {"a": "selectAll", "c": "copy", "x": "cut", "v": "paste", "z": "undo"}


class WebError(RuntimeError):
    pass


@dataclass
class Element:
    backend: int
    role: str
    name: str


def find_browser() -> str | None:
    if (p := os.environ.get("PILOT_BODY_BROWSER")) and Path(p).exists():
        return p
    return next((b for b in BROWSERS if Path(b).exists()), None)


def _v(field: dict[str, Any] | None) -> Any:
    return (field or {}).get("value", "")


def _clip(text: str, n: int = LINE_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def render(nodes: list[dict[str, Any]], origin: str = "") -> tuple[list[str], dict[str, Element]]:
    """Accessibility nodes (CDP getFullAXTree) to indented lines; every non-text element gets a ref.

    An unnamed element holding only text (a list item, a table cell) takes that text as its name,
    and a label's text is dropped when the control beside it already carries it."""
    by_id = {n["nodeId"]: n for n in nodes}
    roots = [n for n in nodes if not n.get("parentId") or n["parentId"] not in by_id]
    lines: list[str] = []
    elements: dict[str, Element] = {}

    def role_of(n: dict[str, Any]) -> str:
        return str(_v(n.get("role")))

    def shown(n: dict[str, Any]) -> list[dict[str, Any]]:
        """Children as displayed: grouping-only nodes are replaced by their own children."""
        out: list[dict[str, Any]] = []
        for c in (by_id[i] for i in n.get("childIds", []) if i in by_id):
            out += shown(c) if c.get("ignored") or role_of(c) in HOIST or role_of(c) == "RootWebArea" else [c]
        return out

    def emit_all(kids: list[dict[str, Any]], depth: int, parent_name: str) -> None:
        siblings = {_clip(_v(k.get("name"))) for k in kids if role_of(k) != "StaticText"}
        for k in kids:
            emit(k, depth, parent_name, siblings)

    def emit(n: dict[str, Any], depth: int, parent_name: str, siblings: set[str]) -> None:
        role, name = role_of(n), _clip(_v(n.get("name")))
        if role == "StaticText":
            if name and name != parent_name and name not in siblings:
                lines.append("  " * depth + f"- text: {name}")
            return
        kids = [] if role in OPAQUE else shown(n)
        if not name and kids and all(role_of(k) == "StaticText" for k in kids):
            name, kids = _clip(" ".join(_v(k.get("name")) for k in kids)), []
        props = {p["name"]: _v(p.get("value")) for p in n.get("properties", [])}
        flags = []
        for key in FLAG_PROPS:
            val = props.get(key)
            if key == "level":
                if val and role == "heading":
                    flags.append(f"level={val}")
            elif key == "expanded" and val in (True, False, "true", "false"):
                flags.append("expanded" if val in (True, "true") else "collapsed")
            elif key in ("checked", "pressed") and val == "mixed":
                flags.append("mixed")
            elif val in (True, "true"):
                flags.append(key)
        line = f"- {role}" + (f' "{name}"' if name else "") + "".join(f" [{f}]" for f in flags)
        if "backendDOMNodeId" in n:
            ref = f"e{n['backendDOMNodeId']}"
            elements[ref] = Element(n["backendDOMNodeId"], role, name)
            line += f" [ref={ref}]"
        value = _clip(_v(n.get("value")))
        if value and value != name:
            line += f": {value}"
        url = props.get("url")
        if role == "link" and url:
            line += f" -> {url[len(origin):] if origin and str(url).startswith(origin) else url}"
        lines.append("  " * depth + line)
        emit_all(kids, depth + 1, name)

    for r in roots:
        emit_all(shown(r) if r.get("ignored") or role_of(r) in HOIST | {"RootWebArea"} else [r], 0, "")
    return lines, elements


def key_events(combo: str) -> list[dict[str, Any]]:
    """An xdotool-style combo ("cmd+a", "Return", "shift+Tab") as CDP key events."""
    parts = [p for p in re.split(r"\+(?!$)", combo.strip()) if p]
    if not parts:
        raise WebError("press needs a key")
    *mods, key = parts
    mask = 0
    for m in mods:
        if m.lower() not in MODIFIERS:
            raise WebError(f"unknown modifier {m!r}")
        mask |= MODIFIERS[m.lower()]
    k = key.lower()
    if k in KEYS:
        name, code, kc, text = KEYS[k]
    elif len(key) == 1:
        ch = key.upper() if mask & 8 else key
        name, kc, text = ch, ord(key.upper()), ch
        code = f"Key{key.upper()}" if key.isalpha() else f"Digit{key}" if key.isdigit() else ""
    else:
        raise WebError(f"unknown key {key!r}")
    commands = []
    if mask & 4:
        text = ""
        if k in MAC_COMMANDS:
            commands = ["redo" if k == "z" and mask & 8 else MAC_COMMANDS[k]]
    if mask & 2:
        text = ""
    down = {"type": "keyDown" if text else "rawKeyDown", "key": name, "code": code, "windowsVirtualKeyCode": kc,
            "nativeVirtualKeyCode": kc, "modifiers": mask, "text": text, "unmodifiedText": text}
    if commands:
        down["commands"] = commands
    up = {"type": "keyUp", "key": name, "code": code, "windowsVirtualKeyCode": kc, "nativeVirtualKeyCode": kc, "modifiers": mask}
    return [down, up]


class Body:
    """One headless browser and its current tab. Started on first use, closed with the task."""

    def __init__(self, data_dir: str | None = None, browser: str | None = None, headless: bool | None = None):
        self.dir = os.path.expanduser(data_dir or os.environ.get("PILOT_BODY_DIR") or DEFAULT_DIR)
        self.browser = browser or find_browser()
        self.headless = (os.environ.get("PILOT_BODY_HEADED") != "1") if headless is None else headless
        self.pipe: Pipe | None = None
        self.session = ""
        self.target = ""
        self.tabs: list[tuple[str, str]] = []  # (target, session), opener first
        self.elements: dict[str, Element] = {}
        self.dialog: dict[str, Any] | None = None
        self.notes: list[str] = []
        self.loading = False

    # -- lifecycle -------------------------------------------------------------------------
    @property
    def started(self) -> bool:
        return self.pipe is not None

    def start(self) -> None:
        if self.pipe is not None:
            return
        if not self.browser:
            raise WebError("no Chromium-family browser found (Chrome, Chromium, Edge or Brave); set PILOT_BODY_BROWSER")
        Path(self.dir).mkdir(parents=True, exist_ok=True)
        argv = [self.browser, *FLAGS, f"--user-data-dir={self.dir}"] + (["--headless"] if self.headless else []) + ["about:blank"]
        self.pipe = Pipe(argv)
        try:
            self.pipe.call("Browser.getVersion", timeout=20)
        except CDPError as e:
            self.pipe.close()
            self.pipe = None
            raise WebError(f"the browser did not start ({e}); is another Pilot using {self.dir}?") from e
        self.pipe.call("Target.setDiscoverTargets", {"discover": True})
        self.pipe.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        pages = [t for t in self.pipe.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]
        target = pages[0]["targetId"] if pages else self.pipe.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self._attach(target)

    def _attach(self, target: str) -> None:
        assert self.pipe is not None
        session = self.pipe.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        for domain in ("Page", "DOM"):
            self.pipe.call(f"{domain}.enable", session=session)
        self.target, self.session = target, session
        self.tabs.append((target, session))
        self.elements = {}

    def close(self) -> None:
        if self.pipe is not None:
            self.pipe.close()
            self.pipe = None

    # -- plumbing ---------------------------------------------------------------------------
    def _call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15) -> dict[str, Any]:
        self.start()
        assert self.pipe is not None
        return self.pipe.call(method, params, session=self.session, timeout=timeout)

    def _eval(self, expr: str, timeout: float = 5) -> Any:
        res = self._call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, timeout=timeout)
        return res.get("result", {}).get("value")

    def _drain(self) -> None:
        assert self.pipe is not None
        for e in self.pipe.take():
            m, p, sid = e.get("method"), e.get("params", {}), e.get("sessionId")
            if m == "Page.javascriptDialogOpening" and sid == self.session:
                if p.get("type") in ("alert", "beforeunload"):
                    # Nothing to decide: an alert only informs, and leaving a page is what was asked.
                    self.pipe.call("Page.handleJavaScriptDialog", {"accept": True}, session=self.session)
                    if p.get("type") == "alert":
                        self.notes.append(f"The page showed an alert: {_clip(p.get('message', ''))!r} (dismissed).")
                else:
                    self.dialog = p
            elif m == "Page.javascriptDialogClosed" and sid == self.session:
                self.dialog = None
            elif m == "Page.frameStartedLoading" and sid == self.session and p.get("frameId") == self.target:
                self.loading = True
            elif m in ("Page.frameStoppedLoading", "Page.loadEventFired") and sid == self.session and p.get("frameId", self.target) == self.target:
                self.loading = False
            elif m == "Target.targetCreated":
                info = p.get("targetInfo", {})
                if info.get("type") == "page" and info.get("openerId") == self.target:
                    self._attach(info["targetId"])
                    self.loading = True
                    self.notes.append("That opened a new tab; you are now on it (`back` returns to the previous tab).")

    def settle(self, timeout: float = 10.0) -> None:
        """Wait for a navigation the last action started, then for the document to finish loading."""
        time.sleep(0.15)
        deadline = time.monotonic() + timeout
        self._drain()
        while self.loading and not self.dialog and time.monotonic() < deadline:
            time.sleep(0.05)
            self._drain()
        while not self.dialog and time.monotonic() < deadline:
            try:
                if self._eval("document.readyState", timeout=2) == "complete":
                    break
            except CDPError:
                pass
            time.sleep(0.1)
            self._drain()
        time.sleep(0.1)
        self._drain()

    def info(self) -> tuple[str, str]:
        if self.dialog:
            return "", ""
        page = self._eval("[document.title, location.href]") or ["", ""]
        return str(page[0]), str(page[1])

    # -- reading ---------------------------------------------------------------------------
    def snapshot(self, max_chars: int = 8000, query: str | None = None) -> str:
        self._drain()
        if self.dialog:
            return (f"A {self.dialog.get('type')} dialog is open: {_clip(self.dialog.get('message', ''))!r}. "
                    "Answer it with action `dialog` (accept true or false).")
        title, url = self.info()
        origin = re.match(r"^[a-z]+://[^/]+", url)
        lines, self.elements = render(self._call("Accessibility.getFullAXTree")["nodes"], origin.group(0) if origin else "")
        head = f"Page: {title or '(untitled)'} — {url}"
        if query:
            q = query.lower()
            hits = [ln.strip() for ln in lines if q in ln.lower()]
            return head + f"\n{len(hits)} line(s) matching {query!r}:\n" + "\n".join(hits[:80])
        out, used = [], len(head)
        for i, ln in enumerate(lines):
            if used + len(ln) + 1 > max_chars:
                out.append(f"… {len(lines) - i} more lines cut: use snapshot with `query`, or scroll and read again.")
                break
            out.append(ln)
            used += len(ln) + 1
        return head + "\n" + "\n".join(out)

    def element(self, ref: str) -> Element:
        if ref in self.elements:
            return self.elements[ref]
        m = re.fullmatch(r"e?(\d+)", str(ref).strip())
        if not m:
            raise WebError(f"{ref!r} is not a ref (refs look like e42)")
        return Element(int(m.group(1)), "", "")

    def by_text(self, text: str) -> Element:
        """The element whose accessible name best matches visible text: exact first, then contained."""
        if not self.elements:
            self.snapshot()
        q = text.strip().lower()
        named = [e for e in self.elements.values() if e.name]
        for match in (lambda n: n == q, lambda n: q in n):
            hits = [e for e in named if match(e.name.lower())]
            if hits:
                interactive = [e for e in hits if e.role in {"button", "link", "checkbox", "radio", "menuitem", "tab", "option", "textbox", "combobox"}]
                return (interactive or hits)[0]
        raise WebError(f"nothing on the page is labelled {text!r}")

    def describe(self, backend: int) -> Element:
        """Role and name of the nearest named accessibility node at or above a DOM node."""
        try:
            nodes = self._call("Accessibility.getPartialAXTree", {"backendNodeId": backend, "fetchRelatives": True})["nodes"]
        except CDPError:
            return Element(backend, "", "")
        own = [n for n in nodes if n.get("backendDOMNodeId") == backend]
        for n in own + list(reversed(nodes)):
            role, name = str(_v(n.get("role"))), _clip(_v(n.get("name")))
            if not n.get("ignored") and name and role not in HOIST:
                return Element(backend, role, name)
        return Element(backend, "", "")

    def attrs(self, el: Element) -> dict[str, str]:
        node = self._call("DOM.describeNode", {"backendNodeId": el.backend})["node"]
        a = node.get("attributes", [])
        return {str(a[i]).lower(): str(a[i + 1]) for i in range(0, len(a) - 1, 2)} | {"tag": str(node.get("nodeName", "")).lower()}

    def screenshot(self) -> tuple[bytes, int, int]:
        data = base64.b64decode(self._call("Page.captureScreenshot", {"format": "png"})["data"])
        return data, VIEWPORT[0], VIEWPORT[1]

    # -- geometry --------------------------------------------------------------------------
    def _object(self, el: Element) -> str:
        try:
            return self._call("DOM.resolveNode", {"backendNodeId": el.backend})["object"]["objectId"]
        except CDPError as e:
            raise WebError(f"{self._label(el)} is gone (the page changed): read the page again") from e

    def _center(self, el: Element) -> tuple[float, float]:
        try:
            self._call("DOM.scrollIntoViewIfNeeded", {"backendNodeId": el.backend})
            quads = self._call("DOM.getContentQuads", {"backendNodeId": el.backend})["quads"]
        except CDPError as e:
            raise WebError(f"{self._label(el)} is gone or not visible: read the page again") from e
        for q in quads:
            xs, ys = q[0::2], q[1::2]
            if max(xs) - min(xs) >= 1 and max(ys) - min(ys) >= 1:
                return sum(xs) / 4, sum(ys) / 4
        raise WebError(f"{self._label(el)} has no visible box")

    def _label(self, el: Element) -> str:
        return f"{el.role or 'element'} {el.name!r}" if el.name else f"e{el.backend}"

    def hit(self, x: float, y: float) -> int:
        """The DOM node a click at viewport (x, y) would land on."""
        node = self._call("DOM.getNodeForLocation", {"x": int(x), "y": int(y), "includeUserAgentShadowDOM": True}).get("backendNodeId")
        if not node:
            raise WebError(f"nothing on the page at ({x:.0f}, {y:.0f})")
        return int(node)

    def covering(self, el: Element, x: float, y: float) -> str | None:
        """What would receive a click at (x, y) instead of `el`, described, if anything."""
        hit = self._call("DOM.getNodeForLocation", {"x": int(x), "y": int(y), "includeUserAgentShadowDOM": True}).get("backendNodeId")
        if not hit or hit == el.backend:
            return None
        hit_obj = self._call("DOM.resolveNode", {"backendNodeId": hit})["object"]["objectId"]
        inside = self._call("Runtime.callFunctionOn", {
            "objectId": self._object(el), "returnByValue": True, "arguments": [{"objectId": hit_obj}],
            "functionDeclaration": "function(h){return this===h||this.contains(h)||(h.control===this)||(this.labels&&[...this.labels].some(l=>l===h||l.contains(h)))}",
        })["result"].get("value")
        if inside:
            return None
        # Name the overlay as a whole (its outermost positioned ancestor) by its text: a modal's
        # backdrop is an unnamed div, and "covered by a div" tells the model nothing.
        return self._call("Runtime.callFunctionOn", {"objectId": hit_obj, "returnByValue": True, "functionDeclaration": """
function(){
  let e=this.nodeType===1?this:this.parentElement, o=e;
  for (let a=e; a && a!==document.body; a=a.parentElement) { const p=getComputedStyle(a).position; if (p==='fixed'||p==='absolute') o=a }
  const t=(o.innerText||'').trim().replace(/\\s+/g,' ');
  return o.tagName.toLowerCase()+(o.id?'#'+o.id:'')+(t?' ("'+(t.length>120?t.slice(0,119)+'…':t)+'")':'');
}"""})["result"].get("value") or "another element"

    def _mouse(self, kind: str, x: float, y: float, **extra: Any) -> None:
        self._call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, **extra})

    # -- acting ----------------------------------------------------------------------------
    def goto(self, url: str) -> str:
        if not re.match(r"^(https?|file|about|data):", url, re.I):
            if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
                raise WebError(f"refusing to open {url.split(':')[0]}: URLs")
            url = "https://" + url
        res = self._call("Page.navigate", {"url": url}, timeout=30)
        if res.get("errorText"):
            raise WebError(f"could not open {url}: {res['errorText']}")
        self.loading = True
        self.settle(timeout=20)
        return f"Opened {url}."

    def click(self, el: Element | None = None, point: tuple[float, float] | None = None, count: int = 1) -> str:
        if el is not None:
            x, y = self._center(el)
            if (cover := self.covering(el, x, y)) is not None:
                raise WebError(f"{self._label(el)} is covered by {cover}: deal with that first")
            what = self._label(el)
        elif point is not None:
            x, y = point
            what = f"({x:.0f}, {y:.0f})"
        else:
            raise WebError("click needs ref, text or x and y")
        self._mouse("mouseMoved", x, y)
        for n in range(1, count + 1):
            self._mouse("mousePressed", x, y, button="left", buttons=1, clickCount=n)
            self._mouse("mouseReleased", x, y, button="left", buttons=0, clickCount=n)
        self.settle()
        return f"Clicked {what}."

    def fill(self, el: Element, text: str, submit: bool = False) -> str:
        kind = self._call("Runtime.callFunctionOn", {"objectId": self._object(el), "returnByValue": True, "functionDeclaration": """
function(){
  const t=this.tagName, ty=(this.type||'').toLowerCase();
  if (t==='SELECT') return 'select';
  if (t==='INPUT' && ['date','time','datetime-local','month','week','color','range'].includes(ty)) return 'value';
  if (this.isContentEditable) {this.focus(); document.getSelection().selectAllChildren(this); return 'ok'}
  if (t==='INPUT' || t==='TEXTAREA') {this.focus(); this.select(); return 'ok'}
  return 'not editable: '+t.toLowerCase();
}"""})["result"].get("value")
        if kind == "select":
            return self.select(el, text)
        if kind == "value":
            self._set_value(el, text)
        elif kind == "ok":
            if text:
                self._call("Input.insertText", {"text": text})
            else:
                self.press("Backspace", settle=False)
        else:
            raise WebError(f"{self._label(el)} is {kind}")
        if submit:
            self.press("Enter", settle=False)
        self.settle()
        return f"Filled {self._label(el)}" + (" and pressed Enter." if submit else ".")

    def _set_value(self, el: Element, value: str) -> None:
        self._call("Runtime.callFunctionOn", {"objectId": self._object(el), "arguments": [{"value": value}], "functionDeclaration":
                    "function(v){this.focus();this.value=v;this.dispatchEvent(new Event('input',{bubbles:true}));"
                    "this.dispatchEvent(new Event('change',{bubbles:true}))}"})

    def select(self, el: Element, option: str) -> str:
        got = self._call("Runtime.callFunctionOn", {"objectId": self._object(el), "returnByValue": True, "arguments": [{"value": option}],
                                                    "functionDeclaration": """
function(v){
  if (this.tagName!=='SELECT') return 'error:not a dropdown (use click on the option instead)';
  const want=v.trim().toLowerCase(), opts=[...this.options];
  const o=opts.find(o=>o.text.trim().toLowerCase()===want||o.value.toLowerCase()===want)||opts.find(o=>o.text.toLowerCase().includes(want));
  if (!o) return 'error:no option '+JSON.stringify(v)+'; options: '+opts.map(o=>o.text.trim()).join(' | ');
  this.focus(); this.value=o.value;
  this.dispatchEvent(new Event('input',{bubbles:true})); this.dispatchEvent(new Event('change',{bubbles:true}));
  return o.text.trim();
}"""})["result"].get("value", "")
        if str(got).startswith("error:"):
            raise WebError(str(got)[6:])
        self.settle()
        return f"Chose {got!r} in {self._label(el)}."

    def press(self, combo: str, settle: bool = True) -> str:
        for ev in key_events(combo):
            self._call("Input.dispatchKeyEvent", ev)
        if settle:
            self.settle()
        return f"Pressed {combo}."

    def type(self, text: str) -> str:
        self._call("Input.insertText", {"text": text})
        self.settle()
        return f"Typed {len(text)} characters at the focus."

    def scroll(self, el: Element | None = None, direction: str = "down", amount: int = 3) -> str:
        if el is not None:
            self._center(el)
            return f"Scrolled {self._label(el)} into view."
        dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}.get(direction, (0, 1))
        self._mouse("mouseWheel", VIEWPORT[0] / 2, VIEWPORT[1] / 2, deltaX=dx * 120 * amount, deltaY=dy * 120 * amount)
        self.settle(timeout=2)
        return f"Scrolled {direction}."

    def drag(self, src: Element, dst: Element) -> str:
        x0, y0 = self._center(src)
        x1, y1 = self._center(dst)
        # Pointer-event drags (setPointerCapture lists) work as is; native HTML drag and drop is
        # intercepted and replayed as drag events, as the browser would for a real mouse.
        self._call("Input.setInterceptDrags", {"enabled": True})
        try:
            self._mouse("mouseMoved", x0, y0)
            self._mouse("mousePressed", x0, y0, button="left", buttons=1, clickCount=1)
            steps = 12
            for i in range(1, steps + 1):
                self._mouse("mouseMoved", x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, button="left", buttons=1)
                time.sleep(0.01)
            assert self.pipe is not None
            intercepted = self.pipe.take("Input.dragIntercepted")
            if intercepted:
                data = intercepted[-1]["params"]["data"]
                for kind in ("dragEnter", "dragOver", "drop"):
                    self._call("Input.dispatchDragEvent", {"type": kind, "x": x1, "y": y1, "data": data})
            self._mouse("mouseReleased", x1, y1, button="left", buttons=0, clickCount=1)
        finally:
            self._call("Input.setInterceptDrags", {"enabled": False})
        self.settle()
        return f"Dragged {self._label(src)} onto {self._label(dst)}."

    def back(self) -> str:
        hist = self._call("Page.getNavigationHistory")
        if hist["currentIndex"] > 0:
            self._call("Page.navigateToHistoryEntry", {"entryId": hist["entries"][hist["currentIndex"] - 1]["id"]})
            self.loading = True
            self.settle()
            return "Went back."
        if len(self.tabs) > 1:
            assert self.pipe is not None
            self.pipe.call("Target.closeTarget", {"targetId": self.target})
            self.tabs.pop()
            self.target, self.session = self.tabs[-1]
            self.elements = {}
            return "Closed this tab and returned to the previous one."
        raise WebError("there is no earlier page")

    def answer_dialog(self, accept: bool, text: str | None = None) -> str:
        self._drain()
        if not self.dialog:
            raise WebError("no dialog is open")
        params: dict[str, Any] = {"accept": accept}
        if text is not None:
            params["promptText"] = text
        self._call("Page.handleJavaScriptDialog", params)
        self.dialog = None
        self.settle()
        return ("Accepted" if accept else "Dismissed") + " the dialog."


def temp_dir() -> str:
    """A throwaway profile, for benchmarks and tests that must not share cookies."""
    return tempfile.mkdtemp(prefix="pilot-body-")
