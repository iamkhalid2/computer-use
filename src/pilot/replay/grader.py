"""Judge an action by what it means, not by its exact form.

Every action is reduced to a *meaning*: a canonical verb (click, fill, type, key, open,
menu, scroll, drag, done, escalate, shell, ...) plus what it targets (element role and
label, coordinates in screenshot pixels, normalised text). Two actions agree when they
mean the same thing: pressing [12] "Save" by id, by label, or by clicking its pixels
are one action; "cmd+S" and "Command+s" are one key.

Soft items compare the first effective (non-observing) action with the one taken in a
run the grader passed. Hard items carry curated rules:
  expect           the first effective action must match one of these patterns
  expect_in_batch  some action in the batch must match one of these
  forbid           no action in the batch may match any of these
Pattern keys: tool (str | list), label (substring), success (bool), contains
(substring of the action's text and args), region ([x0, y0, x1, y1] in pixels).
"""

from __future__ import annotations

import json
import re
from typing import Any

OBSERVE = {"screenshot", "zoom", "cursor_position", "wait", "mouse_move", "find", "wait_for", "observe"}
CLICKS = {"left_click", "double_click", "triple_click", "right_click", "middle_click"}
MOD_ALIASES = {"command": "cmd", "super": "cmd", "meta": "cmd", "win": "cmd", "control": "ctrl", "option": "alt", "opt": "alt"}
NEAR_PX = 40


def norm(text: Any) -> str:
    t = str(text or "").strip().lower().replace("…", "").replace("...", "")
    return " ".join(re.sub(r"[“”\"'`]", "", t).split())


def norm_key(combo: str) -> str:
    parts = [MOD_ALIASES.get(p.strip().lower(), p.strip().lower()) for p in str(combo).split("+") if p.strip()]
    mods = sorted(p for p in parts[:-1])
    return "+".join(mods + parts[-1:])


def norm_url(u: str) -> str:
    return norm(u).rstrip("/").removeprefix("https://").removeprefix("http://").removeprefix("www.")


def to_pixels(coord: Any, frame: dict[str, Any]) -> tuple[float, float] | None:
    if not (isinstance(coord, (list, tuple)) and len(coord) == 2):
        return None
    x, y = float(coord[0]), float(coord[1])
    if frame.get("space") == "norm1000":
        w, h = frame.get("screen") or (1000, 1000)
        x, y = x * w / 1000.0, y * h / 1000.0
    return (x, y)


def hit(frame: dict[str, Any], pt: tuple[float, float] | None) -> dict[str, Any] | None:
    """Smallest element whose box contains the point (interactive preferred)."""
    if pt is None or not frame.get("elements"):
        return None
    best = None
    for e in frame["elements"]:
        b = e.get("box")
        if not b or not (b[0] <= pt[0] <= b[0] + b[2] and b[1] <= pt[1] <= b[1] + b[3]):
            continue
        key = (not e.get("interactive"), b[2] * b[3])
        if best is None or key < best[0]:
            best = (key, e)
    return best[1] if best else None


def element(frame: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    if args.get("id") is not None and frame.get("elements"):
        return next((e for e in frame["elements"] if e.get("id") == args.get("id")), None)
    if args.get("target"):
        return {"label": args["target"]}
    return None


def meaning(action: dict[str, Any], frame: dict[str, Any], known_target: dict[str, Any] | None = None) -> dict[str, Any]:
    """Canonical form of one action. `known_target` is what Pilot reported it hit (reference actions)."""
    name, args = action.get("name"), action.get("args") or {}
    m: dict[str, Any] = {"tool": name, "label": None, "role": None, "pt": None, "text": "", "raw": json.dumps(args, ensure_ascii=False)}
    if name == "computer":
        act = args.get("action")
        if act in CLICKS:
            m["tool"] = "click"
            m["pt"] = to_pixels(args.get("coordinate"), frame)
        elif act in {"key", "hold_key"}:
            m["tool"], m["text"] = "key", norm_key(args.get("text", ""))
        elif act == "type":
            m["tool"], m["text"] = "type", norm(args.get("text"))
        elif act == "scroll":
            m["tool"], m["text"] = "scroll", norm(args.get("scroll_direction", "down"))
            m["pt"] = to_pixels(args.get("coordinate"), frame)
        elif act == "left_click_drag":
            m["tool"] = "drag"
            m["pt"] = to_pixels(args.get("start_coordinate"), frame)
        else:
            m["tool"] = "observe"
    elif name == "ui":
        act = args.get("action") or "press"
        m["id"] = args.get("id")
        m["tool"] = {"press": "click", "confirm": "click", "set_value": "fill", "focus": "focus", "show_menu": "click"}.get(act, "click")
        m["text"] = norm(args.get("value"))
        el = element(frame, args)
        if el:
            m["label"], m["role"] = el.get("label"), el.get("role")
            if el.get("box"):
                b = el["box"]
                m["pt"] = (b[0] + b[2] / 2, b[1] + b[3] / 2)
    elif name == "open":
        m["text"] = norm_url(args.get("target", ""))
    elif name == "menu":
        m["text"] = " > ".join(norm(p) for p in args.get("path") or [])
    elif name == "done":
        ok = args.get("success")
        m["success"] = ok.strip().lower() in {"true", "yes", "1"} if isinstance(ok, str) else ok
        m["text"] = norm(f"{args.get('summary', '')} {args.get('answer', '')}")
    elif name in {"shell", "applescript", "write_file"}:
        m["text"] = norm(args.get("command") or args.get("script") or f"{args.get('path', '')} {args.get('text', '')}")
    elif name in OBSERVE:
        m["tool"] = "observe"
    if m["tool"] in {"click", "focus", "fill", "drag"} and m["label"] is None:
        el = hit(frame, m["pt"])
        if el:
            m["label"], m["role"] = el.get("label"), el.get("role")
    if known_target and m["label"] in (None, ""):
        m["label"], m["role"] = known_target.get("label"), known_target.get("role")
    return m


def first_effective(meanings: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((m for m in meanings if m["tool"] != "observe"), None)


def labels_match(a: Any, b: Any) -> bool:
    na, nb = norm(a), norm(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))


def same(c: dict[str, Any], r: dict[str, Any]) -> tuple[bool | None, str]:
    """Do candidate c and reference r mean the same action?"""
    ct, rt = c["tool"], r["tool"]
    # Starting to fill a field (click/focus on it) agrees with filling it.
    if rt == "fill" and ct in {"click", "focus"}:
        ct = "fill"
    if ct == "fill" and rt in {"click", "focus"}:
        rt = "fill"
    if ct != rt:
        return False, f"{c['tool']} vs {r['tool']}"
    if ct in {"click", "focus", "fill", "drag"}:
        if c.get("id") is not None and c.get("id") == r.get("id"):
            return True, f"same element id {c['id']}"  # ids are per frame, so equal ids are the same element
        if c["label"] and r["label"]:
            return labels_match(c["label"], r["label"]), f"target {c['label']!r} vs {r['label']!r}"
        if c["pt"] and r["pt"]:
            d = ((c["pt"][0] - r["pt"][0]) ** 2 + (c["pt"][1] - r["pt"][1]) ** 2) ** 0.5
            return d <= NEAR_PX, f"{d:.0f}px apart"
        return None, "ungradable: what the candidate clicked can't be identified on this frame"
    if ct in {"key", "type", "menu", "open", "scroll"}:
        if ct == "open":
            ok = c["text"] == r["text"] or c["text"].split("/")[0] == r["text"].split("/")[0] and c["text"].endswith(r["text"].split("/")[-1])
            return ok, f"{c['text']!r} vs {r['text']!r}"
        return c["text"] == r["text"], f"{c['text']!r} vs {r['text']!r}"
    if ct == "done":
        return c.get("success") == r.get("success"), f"success {c.get('success')} vs {r.get('success')}"
    return True, "same tool"


def matches(m: dict[str, Any], p: dict[str, Any]) -> bool:
    tools = p.get("tool")
    if tools and tools != "any":
        tools = [tools] if isinstance(tools, str) else tools
        if m["tool"] not in tools:
            return False
    if "label" in p and not labels_match(m.get("label"), p["label"]):
        return False
    if "success" in p and m.get("success") is not p["success"]:
        return False
    if "contains" in p:
        blob = norm(f"{m.get('text', '')} {m.get('label') or ''} {m.get('raw', '')}")
        if norm(p["contains"]) not in blob:
            return False
    if "region" in p:
        if not m.get("pt"):
            return False
        x0, y0, x1, y1 = p["region"]
        if not (x0 <= m["pt"][0] <= x1 and y0 <= m["pt"][1] <= y1):
            return False
    return True


def reference(frame: dict[str, Any]) -> list[dict[str, Any]]:
    executed = frame.get("executed") or []
    return [meaning(a, frame, a.get("target")) for a in executed]


def grade(frame: dict[str, Any], calls: list[dict[str, Any]], cand_space: str | None = None) -> dict[str, Any]:
    # The candidate may speak another coordinate space than the frame was recorded in.
    cframe = {**frame, "space": cand_space} if cand_space else frame
    cand = [meaning(c, cframe) for c in calls]
    label = frame.get("label") or {}
    if label.get("kind") == "hard":
        forbidden = [p for p in label.get("forbid", []) for m in cand if matches(m, p)]
        if forbidden:
            return {"pass": False, "why": f"forbidden: {forbidden[0]}"}
        first = first_effective(cand)
        if label.get("expect"):
            if first is None or not any(matches(first, p) for p in label["expect"]):
                return {"pass": False, "why": f"first action {first['tool'] if first else 'none'} {first and first.get('label')!r} not expected"}
        if label.get("expect_in_batch") and not any(matches(m, p) for m in cand for p in label["expect_in_batch"]):
            return {"pass": False, "why": "expected action missing from batch"}
        return {"pass": True, "why": "meets the curated rules"}
    ref = first_effective(reference(frame))
    first = first_effective(cand)
    if ref is None:
        return {"pass": first is None, "why": "reference only observed"}
    if first is None:
        return {"pass": False, "why": "candidate only observed"}
    ok, why = same(first, ref)
    return {"pass": ok, "why": why}
