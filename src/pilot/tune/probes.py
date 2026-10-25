"""Synthetic probes: screens drawn in memory, so answers are checked against exact rectangles.

  grounding  screenshot only, one probe per coordinate space: which convention does the
             model actually use, and does it click where it means to?
  ids        the same screen with an element list: does it act through element ids?
No real screen is involved and each probe is one model call.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..agent import prompts
from ..config import ModelSpec, Settings
from ..providers import get_provider
from ..tools import build_specs
from ..types import ImagePart, TextPart

W, H = 1470, 956

LAYOUTS = [
    {"title": "Export settings", "buttons": {"Advanced…": (340, 700, 460, 730), "Cancel": (860, 700, 960, 730), "Export": (980, 700, 1120, 730)},
     "target": "Export"},
    {"title": "Delete 3 photos?", "buttons": {"Keep": (520, 470, 640, 500), "Move to Album": (660, 470, 820, 500), "Help": (300, 600, 360, 624)},
     "target": "Move to Album"},
    {"title": "Print", "buttons": {"PDF ▾": (330, 640, 410, 666), "Cancel": (900, 640, 990, 666), "Print": (1010, 640, 1100, 666)},
     "target": "Print"},
    # Small targets: a toolbar of icon-sized buttons.
    {"title": "Toolbar", "style": "small", "target": "Share",
     "buttons": {n: (300 + i * 44, 200, 326 + i * 44, 226) for i, n in enumerate(["Back", "Forward", "Sidebar", "Share", "Tag", "Info", "Trash"])}},
    # Dense list: one row among many look-alikes.
    {"title": "Documents", "style": "rows", "target": "Invoice 2026-09.pdf",
     "buttons": {n: (310, 210 + i * 30, 1160, 236 + i * 30) for i, n in enumerate(
         [f"Invoice 2026-0{m}.pdf" for m in range(1, 10)] + ["Invoice 2026-10.pdf", "Notes.txt", "Budget.numbers", "Photo.heic", "Plan.key"])}},
    # Dark mode, low contrast.
    {"title": "Save changes to “Report”?", "style": "dark", "target": "Save",
     "buttons": {"Don't Save": (360, 690, 500, 718), "Cancel": (880, 690, 980, 718), "Save": (1000, 690, 1100, 718)}},
    # Target hugging the screen corner.
    {"title": "Edit profile", "target": "Done",
     "buttons": {"Done": (1392, 34, 1462, 60), "Cancel": (300, 700, 390, 726)}},
    # A text field rather than a button.
    {"title": "Sign up for updates", "style": "field", "target": "Email address",
     "buttons": {"Name": (330, 300, 1100, 334), "Email address": (330, 380, 1100, 414), "Submit": (980, 700, 1100, 730)}},
]


def _font(size: int = 15):
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
    except OSError:
        return ImageFont.load_default()


def draw(layout: dict[str, Any], size: tuple[int, int] = (W, H)) -> ImagePart:
    style = layout.get("style", "")
    dark = style == "dark"
    bg, card, ink, edge = ((32, 32, 34), (44, 44, 46), (225, 225, 228), (70, 70, 74)) if dark else \
                          ((236, 236, 236), (255, 255, 255), (0, 0, 0), (200, 200, 200))
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    f = _font(11 if style == "small" else 15)
    d.rectangle([0, 0, W, 28], fill=(250, 250, 250) if not dark else (20, 20, 22))
    d.text((14, 6), "  Preview   File   Edit   View   Window   Help", fill=ink, font=_font())
    d.rounded_rectangle([280, 140, 1190, 780], 12, fill=card, outline=edge)
    d.text((310, 160), layout["title"], fill=ink, font=_font(18))
    target = layout["target"]
    for name, (x0, y0, x1, y1) in layout["buttons"].items():
        if style == "rows":
            d.rectangle([x0, y0, x1, y1], fill=(246, 246, 248) if (y0 // 30) % 2 else card)
            d.text((x0 + 10, y0 + 5), name, fill=ink, font=f)
            continue
        if style == "field" and name != "Submit":
            d.text((x0, y0 - 20), name, fill=ink, font=f)
            d.rounded_rectangle([x0, y0, x1, y1], 5, fill=card, outline=(160, 160, 160))
            continue
        primary = name == target and style not in {"small"} and name != "Move to Album"
        fill = (10, 132, 255) if primary else ((58, 58, 62) if dark else (245, 245, 245))
        d.rounded_rectangle([x0, y0, x1, y1], 6, fill=fill, outline=edge)
        label = name if style != "small" else name[:2]
        d.text((x0 + (6 if style == "small" else 14), y0 + (7 if style == "small" else 6)), label,
               fill=(255, 255, 255) if primary or dark else (0, 0, 0), font=f)
        if style == "small":
            d.text((x0 - 4, y1 + 4), name, fill=(90, 90, 90), font=_font(9))
    if size != (W, H):
        img = img.resize(size, Image.Resampling.LANCZOS)  # what a smaller max_long_edge would send
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return ImagePart(buf.getvalue(), "image/png", size[0], size[1])


def outline_text(layout: dict[str, Any], space: str) -> tuple[str, dict[int, str]]:
    lines, ids = [], {}
    for i, (name, (x0, y0, x1, y1)) in enumerate(layout["buttons"].items(), start=1):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if space == "norm1000":
            cx, cy = cx * 1000 / W, cy * 1000 / H
        role = {"rows": "row", "field": "field"}.get(layout.get("style", ""), "button")
        lines.append(f'[{i}] {role} "{name}" @{round(cx)},{round(cy)}')
        ids[i] = name
    return "Frontmost app: Preview\nUI elements (use ids with the `ui` tool; @x,y = screenshot coords):\n" + "\n".join(lines), ids


@dataclass
class ProbeResult:
    kind: str
    space: str
    layout: str
    hit: bool
    detail: str
    latency: float
    pt: tuple[float, float] | None = None  # click in base (1470x956) pixels
    center: tuple[float, float] | None = None  # target centre in base pixels
    raw: tuple[float, float] | None = None  # click as the model gave it, in the frame it was shown


def _clicked(calls: list[Any], space: str, size: tuple[int, int]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    for c in calls:
        if c.name == "computer" and c.args.get("action") in {"left_click", "double_click"} and isinstance(c.args.get("coordinate"), list):
            x, y = (float(v) for v in c.args["coordinate"][:2])
            raw = (x, y)
            if space == "norm1000":
                return raw, (x * W / 1000, y * H / 1000)
            return raw, (x * W / size[0], y * H / size[1])  # back to base pixels
    return None


def run(spec: ModelSpec, space: str, kind: str, layout: dict[str, Any], system: str | None = None,
        size: tuple[int, int] = (W, H)) -> ProbeResult:
    provider = get_provider(spec)
    space = "pixels" if provider.native_computer else space
    tools = build_specs(Settings(planner=None, executor=spec, interactive=False), space, size, planner_mode=False, ax=(kind == "ids"))
    session = provider.session(system or prompts.EXECUTOR, tools)
    target = layout["target"]
    noun = {"rows": "row", "field": "field"}.get(layout.get("style", ""), "button")
    lead = f"Task: click the {target!r} {noun}. That is the only thing to do: one action, then stop.\n\nCurrent screen:"
    if kind == "ids":
        listing, ids = outline_text(layout, space)
    else:
        listing, ids = f"Frontmost app: Preview\nScreenshot: {size[0]}x{size[1]}\n(The element list is unavailable: use the computer tool.)", {}
    t0 = time.monotonic()
    try:
        turn = session.send([TextPart(lead), draw(layout, size), TextPart(listing)])
    finally:
        if hasattr(session, "close"):
            session.close()
    dt = time.monotonic() - t0
    x0, y0, x1, y1 = layout["buttons"][target]
    center = ((x0 + x1) / 2, (y0 + y1) / 2)
    for c in turn.calls:
        if c.name == "ui":
            chosen = ids.get(int(c.args["id"])) if str(c.args.get("id", "")).isdigit() else c.args.get("target")
            return ProbeResult(kind, space, layout["title"], chosen == target, f"ui → {chosen!r}", dt)
    got = _clicked(turn.calls, space, size)
    if got is None:
        return ProbeResult(kind, space, layout["title"], False, f"no click ({[c.name for c in turn.calls]})", dt, center=center)
    raw, pt = got
    hit = x0 <= pt[0] <= x1 and y0 <= pt[1] <= y1
    return ProbeResult(kind, space, layout["title"], hit, f"click at {round(pt[0])},{round(pt[1])}", dt, pt, center, raw)
