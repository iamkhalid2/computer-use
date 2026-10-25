"""Screen geometry and capture.

Three coordinate frames exist on a Retina Mac:
  * pixels  - what a raw capture contains (e.g. 2940x1912)
  * points  - what Quartz events and the Accessibility API use (e.g. 1470x956)
  * model   - the screenshot we send to the model (points, capped in size)

`Geometry` owns every conversion so nothing else has to think about it.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
import time
from dataclasses import dataclass

from PIL import Image


@dataclass(frozen=True)
class Geometry:
    points_w: int
    points_h: int
    pixels_w: int
    pixels_h: int
    model_w: int
    model_h: int

    @classmethod
    def make(cls, points_w: int, points_h: int, pixels_w: int, pixels_h: int, max_long_edge: int) -> "Geometry":
        scale = min(1.0, max_long_edge / max(points_w, points_h))
        return cls(points_w, points_h, pixels_w, pixels_h, round(points_w * scale), round(points_h * scale))

    def model_to_points(self, x: float, y: float) -> tuple[float, float]:
        px = x * self.points_w / self.model_w
        py = y * self.points_h / self.model_h
        return (min(max(px, 0.0), self.points_w - 1.0), min(max(py, 0.0), self.points_h - 1.0))

    def points_to_model(self, x: float, y: float) -> tuple[int, int]:
        return (round(x * self.model_w / self.points_w), round(y * self.model_h / self.points_h))

    def points_to_pixels(self, x: float, y: float) -> tuple[int, int]:
        return (round(x * self.pixels_w / self.points_w), round(y * self.pixels_h / self.points_h))


def thumb_hash(img: Image.Image) -> str:
    """A tiny perceptual fingerprint used for settle and no-change detection."""
    small = img.convert("L").resize((48, 30), Image.Resampling.BILINEAR)
    # Quantise so sub-pixel antialiasing and cursor blink don't count as change.
    data = bytes(v // 24 for v in small.tobytes())
    return hashlib.blake2b(data, digest_size=8).hexdigest()


def encode(img: Image.Image, fmt: str = "png") -> tuple[bytes, str]:
    buf = io.BytesIO()
    if fmt == "jpeg":
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue(), "image/png"


class Screen:
    """Captures the main display, scaled to the model frame."""

    def __init__(self, max_long_edge: int = 1568):
        import Quartz

        self._q = Quartz
        self.display = Quartz.CGMainDisplayID()
        bounds = Quartz.CGDisplayBounds(self.display)
        mode = Quartz.CGDisplayCopyDisplayMode(self.display)
        self.geometry = Geometry.make(
            int(bounds.size.width),
            int(bounds.size.height),
            int(Quartz.CGDisplayModeGetPixelWidth(mode)),
            int(Quartz.CGDisplayModeGetPixelHeight(mode)),
            max_long_edge,
        )
        self._cg_ok = True

    # -- raw capture ---------------------------------------------------------
    def _capture_cg(self) -> Image.Image | None:
        q = self._q
        image = q.CGDisplayCreateImage(self.display)
        if image is None:
            return None
        w, h = q.CGImageGetWidth(image), q.CGImageGetHeight(image)
        bpr = q.CGImageGetBytesPerRow(image)
        data = q.CGDataProviderCopyData(q.CGImageGetDataProvider(image))
        return Image.frombuffer("RGBA", (w, h), bytes(data), "raw", "BGRA", bpr, 1)

    def _capture_cli(self) -> Image.Image:
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            subprocess.run(["screencapture", "-x", "-C", "-m", "-t", "png", f.name], check=True, timeout=10)
            img = Image.open(f.name)
            img.load()
            return img

    def capture_full(self) -> Image.Image:
        """Full-resolution (pixel) capture of the main display."""
        if self._cg_ok:
            try:
                img = self._capture_cg()
                if img is not None:
                    return img
            except Exception:
                pass
            self._cg_ok = False
        return self._capture_cli()

    def _to_model(self, img: Image.Image) -> Image.Image:
        g = self.geometry
        if img.size == (g.model_w, g.model_h):
            return img
        factor = img.size[0] // g.model_w
        if factor >= 2 and img.size[0] == g.model_w * factor and img.size[1] == g.model_h * factor:
            return img.reduce(factor)
        return img.resize((g.model_w, g.model_h), Image.Resampling.LANCZOS)

    def capture(self) -> Image.Image:
        """Capture in the model frame (RGB)."""
        return self._to_model(self.capture_full()).convert("RGB")

    def capture_settled(self, timeout: float = 1.5, interval: float = 0.12) -> Image.Image:
        """Capture once the screen stops changing (animations, page loads)."""
        deadline = time.monotonic() + timeout
        prev = self.capture()
        prev_hash = thumb_hash(prev)
        while time.monotonic() < deadline:
            time.sleep(interval)
            cur = self.capture()
            cur_hash = thumb_hash(cur)
            if cur_hash == prev_hash:
                return cur
            prev, prev_hash = cur, cur_hash
        return prev

    def zoom(self, region: tuple[float, float, float, float]) -> Image.Image:
        """Crop a model-frame region at full resolution, scaled to fit the model frame."""
        g = self.geometry
        x0, y0, x1, y1 = region
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        p0 = g.points_to_pixels(*g.model_to_points(x0, y0))
        p1 = g.points_to_pixels(*g.model_to_points(x1, y1))
        full = self.capture_full()
        crop = full.crop((p0[0], p0[1], max(p1[0], p0[0] + 8), max(p1[1], p0[1] + 8))).convert("RGB")
        crop.thumbnail((g.model_w, g.model_h), Image.Resampling.LANCZOS)
        if crop.size[0] < g.model_w and crop.size[1] < g.model_h:
            ratio = min(g.model_w / crop.size[0], g.model_h / crop.size[1])
            crop = crop.resize((round(crop.size[0] * ratio), round(crop.size[1] * ratio)), Image.Resampling.LANCZOS)
        return crop

    def has_permission(self) -> bool:
        return bool(self._q.CGPreflightScreenCaptureAccess())

    def request_permission(self) -> bool:
        return bool(self._q.CGRequestScreenCaptureAccess())
