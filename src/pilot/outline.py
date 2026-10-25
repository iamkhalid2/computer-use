"""Pure functions over AX snapshots: numbering, outline text, fuzzy matching."""

from __future__ import annotations

import difflib
import re
from typing import Callable

from .mac.ax import Node, Snapshot

ROLE_NAMES = {
    "AXButton": "button", "AXCheckBox": "checkbox", "AXRadioButton": "radio", "AXPopUpButton": "popup",
    "AXMenuButton": "menubutton", "AXComboBox": "combobox", "AXTextField": "field", "AXTextArea": "textarea",
    "AXLink": "link", "AXSlider": "slider", "AXIncrementor": "stepper", "AXDisclosureTriangle": "disclosure",
    "AXCell": "cell", "AXRow": "row", "AXOutlineRow": "row", "AXMenuItem": "menuitem", "AXMenuBarItem": "menu",
    "AXStaticText": "text", "AXHeading": "heading", "AXImage": "image", "AXTab": "tab", "AXDockItem": "dock",
    "AXColorWell": "color", "AXDateField": "date", "AXValueIndicator": "indicator", "AXSwitch": "switch",
}
SUBROLE_NAMES = {"AXSearchField": "search", "AXSecureTextField": "password", "AXTabButton": "tab", "AXSwitch": "switch", "AXToggle": "toggle"}

ToModel = Callable[[float, float], tuple[int, int]]


def role_name(n: Node) -> str:
    return SUBROLE_NAMES.get(n.subrole or "", ROLE_NAMES.get(n.role, n.role.removeprefix("AX").lower()))


def describe(n: Node, to_model: ToModel | None = None) -> str:
    parts = [role_name(n)]
    if n.label:
        parts.append(f'"{n.label}"')
    if n.value not in (None, "") and n.value != n.label:
        parts.append(f"={n.value!r}" if n.role not in {"AXCheckBox", "AXRadioButton", "AXSwitch"} else f"[{n.value}]")
    flags = []
    if not n.enabled:
        flags.append("disabled")
    # Per-element AXFocused is unreliable (Finder marks every sidebar cell focused); the
    # observation header carries the app's real keyboard focus instead.
    if n.selected:
        flags.append("selected")
    if flags:
        parts.append("(" + ",".join(flags) + ")")
    if to_model and n.center:
        x, y = to_model(*n.center)
        parts.append(f"@{x},{y}")
    return " ".join(parts)


def _inside(child: Node, parent: Node) -> bool:
    """True when child sits within parent on screen (unknown frames count as inside)."""
    if not child.frame or not parent.frame or not child.center:
        return True
    x, y = child.center
    px, py, pw, ph = parent.frame
    return px <= x <= px + pw and py <= y <= py + ph


def compact(nodes: list[Node]) -> list[Node]:
    """Drop redundant lines: unlabelled rows wrapping a labelled cell, and text that
    repeats the label of the element just above it (row > cell > text "Downloads")."""
    out: list[Node] = []
    for i, n in enumerate(nodes):
        nxt = nodes[i + 1] if i + 1 < len(nodes) else None
        if n.role in {"AXRow", "AXOutlineRow"} and not n.label and nxt is not None and nxt.depth > n.depth and nxt.label:
            continue
        if n.role in {"AXStaticText", "AXImage"} and out:
            prev = out[-1]
            # Only a decoration of the element it sits inside: depth alone also matches unrelated
            # branches (Finder's file icons follow the last sidebar row), so require containment.
            nested = prev.depth < n.depth and _inside(n, prev)
            if nested and n.label and (n.label == prev.label or (n.role == "AXImage" and prev.label)):
                continue
        out.append(n)
    return out


def number(nodes: list[Node], start: int = 1) -> int:
    for i, n in enumerate(nodes):
        n.id = start + i
    return start + len(nodes)


def outline(snap: Snapshot, to_model: ToModel, max_lines: int = 180, max_chars: int = 8000) -> str:
    """Compact, numbered element list. Interactive elements win when space runs out."""
    nodes = snap.nodes
    if len(nodes) > max_lines:
        interactive = [n for n in nodes if n.interactive]
        keep_ids = {id(n) for n in interactive[:max_lines]}
        room = max_lines - len(keep_ids)
        for n in nodes:
            if room <= 0:
                break
            if id(n) not in keep_ids:
                keep_ids.add(id(n))
                room -= 1
        shown = [n for n in nodes if id(n) in keep_ids]
    else:
        shown = nodes
    lines: list[str] = []
    used = 0
    for n in shown:
        indent = "  " * min(max(n.depth - 2, 0) // 3, 4)
        line = f"{indent}[{n.id}] {describe(n, to_model)}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    hidden = len(nodes) - len(lines)
    if hidden > 0 or snap.truncated:
        lines.append(f"(+{max(hidden, 0)} more elements not shown{' - tree truncated' if snap.truncated else ''}; use `find` to search)")
    return "\n".join(lines)


_ELLIPSIS = re.compile(r"(\.\.\.|…)$")


def norm(text: str) -> str:
    text = _ELLIPSIS.sub("", text.strip().lower())
    return " ".join(re.sub(r"[“”\"'`]", "", text).split())


def score(query: str, n: Node) -> float:
    """How well a node matches a free-text target like "Save", "search field"."""
    q = norm(query)
    if not q:
        return 0.0
    best = 0.0
    role = role_name(n)
    candidates = [n.label or "", n.value or "", n.identifier or ""]
    for raw in candidates:
        c = norm(raw)
        if not c:
            continue
        if c == q:
            s = 1.0
        elif c.startswith(q) or q.startswith(c) and len(c) >= 3:
            s = 0.88
        elif re.search(rf"\b{re.escape(q)}\b", c):
            s = 0.8
        elif q in c:
            s = 0.7
        else:
            qt, ct = set(q.split()), set(c.split())
            overlap = len(qt & ct) / max(len(qt), 1)
            s = max(0.6 * overlap, 0.6 * difflib.SequenceMatcher(None, q, c).ratio() if len(q) > 3 else 0.0)
        best = max(best, s)
    # "search field", "OK button": let a role word in the query help.
    words = q.split()
    if len(words) > 1 and words[-1] in {role, n.role.removeprefix("AX").lower()}:
        best = max(best, score(" ".join(words[:-1]), n) * 0.98)
    if n.interactive:
        best += 0.03
    if not n.enabled:
        best -= 0.15
    return min(best, 1.0)


def rank(query: str, nodes: list[Node], role: str | None = None, limit: int = 15) -> list[tuple[float, Node]]:
    scored = []
    for n in nodes:
        if role and role.lower() not in {role_name(n), n.role.lower(), n.role.removeprefix("AX").lower()}:
            continue
        s = score(query, n)
        if s >= 0.45:
            scored.append((s, n))
    scored.sort(key=lambda t: -t[0])
    return scored[:limit]
