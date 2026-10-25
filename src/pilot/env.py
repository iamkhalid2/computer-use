"""The Mac environment: observation (screenshot + AX outline) and primitives."""

from __future__ import annotations

import os
import time

from PIL import Image

from . import outline as ol
from .mac import apps
from .mac.ax import AX, Node, Snapshot
from .mac.input import Input
from .mac.screen import Screen, encode, thumb_hash
from .types import ImagePart, Observation


class MacEnv:
    def __init__(self, max_long_edge: int = 1568, image_format: str = "png", use_ax: bool = True):
        self.screen = Screen(max_long_edge)
        self.max_long_edge = max_long_edge
        self.input = Input()
        self.ax = AX()
        self.image_format = image_format
        self.use_ax = use_ax and self.ax.trusted()
        self.registry: dict[int, Node] = {}
        self.next_id = 1
        self.last_snapshot: Snapshot | None = None
        self.last_image: Image.Image | None = None
        self.host_pid = self._host_pid()
        self.outline_max_lines, self.outline_max_chars = 180, 8000
        self.compact, self.settle_timeout = True, 1.5

    def configure(self, profile) -> None:
        """Apply a profile's observation knobs (what the model sees, and how fast)."""
        if profile.max_long_edge != self.max_long_edge:
            self.screen, self.max_long_edge = Screen(profile.max_long_edge), profile.max_long_edge
        self.image_format = profile.image_format
        self.use_ax = profile.use_ax and self.ax.trusted()
        self.outline_max_lines, self.outline_max_chars = profile.outline_max_lines, profile.outline_max_chars
        self.compact, self.settle_timeout = profile.compact_outline, profile.settle_timeout
        self.input.paste_threshold = profile.paste_threshold

    @property
    def geometry(self):
        return self.screen.geometry

    # -- coordinates -----------------------------------------------------------------
    def to_points(self, x: float, y: float, space: str = "pixels") -> tuple[float, float]:
        g = self.geometry
        if space == "norm1000":
            x, y = x * g.model_w / 1000.0, y * g.model_h / 1000.0
        return g.model_to_points(x, y)

    def to_model(self, x: float, y: float, space: str = "pixels") -> tuple[int, int]:
        mx, my = self.geometry.points_to_model(x, y)
        if space == "norm1000":
            g = self.geometry
            return (round(mx * 1000 / g.model_w), round(my * 1000 / g.model_h))
        return (mx, my)

    # -- observation ----------------------------------------------------------------
    def image_part(self, img: Image.Image) -> ImagePart:
        data, media = encode(img, self.image_format)
        return ImagePart(data, media, img.size[0], img.size[1])

    def snapshot(self) -> Snapshot | None:
        if not self.use_ax:
            return None
        g = self.geometry
        try:
            snap = self.ax.snapshot((g.points_w, g.points_h))
        except Exception as e:  # never let AX failures kill a run
            pid, name, bundle = self.frontmost()
            return Snapshot(name, pid, bundle, "", [], error=str(e))
        if self.compact:
            snap.nodes = ol.compact(snap.nodes)
        self.registry = {}
        self.next_id = ol.number(snap.nodes, 1)
        for n in snap.nodes:
            self.registry[n.id] = n
        self.last_snapshot = snap
        return snap

    def observe(self, settle: bool = True, coord_space: str = "pixels", with_ax: bool = True) -> Observation:
        t0 = time.monotonic()
        img = self.screen.capture_settled(timeout=self.settle_timeout) if settle and self.settle_timeout > 0 else self.screen.capture()
        cap_ms = (time.monotonic() - t0) * 1000
        self.last_image = img
        snap = self.snapshot() if with_ax else None
        to_model = lambda x, y: self.to_model(x, y, coord_space)  # noqa: E731
        if snap is not None:
            app, window = snap.app, snap.window
            text = ol.outline(snap, to_model, self.outline_max_lines, self.outline_max_chars) if snap.nodes else (f"(accessibility error: {snap.error})" if snap.error else "(no accessible elements)")
            focused = ol.describe(snap.focused) if snap.focused else ""
            ax_ms = snap.elapsed_ms
        else:
            _, app, _ = self.frontmost()
            window, text, focused, ax_ms = "", "", "", 0.0
        return Observation(
            image=self.image_part(img), app=app, window=window, outline=text,
            windows=apps.visible_windows(), focused=focused, ax_ms=ax_ms, capture_ms=cap_ms,
            hash=thumb_hash(img),
        )

    def quick_hash(self) -> str:
        return thumb_hash(self.screen.capture())

    # -- apps -------------------------------------------------------------------------
    def frontmost(self) -> tuple[int, str, str | None]:
        try:
            return self.ax.frontmost()
        except Exception:
            return (-1, "unknown", None)

    def _host_pid(self) -> int | None:
        """The terminal app hosting Pilot, so we never type into ourselves."""
        try:
            pid = os.getppid()
            import AppKit

            for _ in range(12):
                app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if app is not None and app.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular:
                    return pid
                out = os.popen(f"ps -o ppid= -p {pid}").read().strip()
                if not out or out == "1":
                    break
                pid = int(out)
        except Exception:
            return None
        return None
