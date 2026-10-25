"""Jev (TypeSafe AI) as the reflex layer: typed, calibrated, ~0.4 s decisions.

Jev returns a choice, a yes/no probability or a bounding box - never text - with
a calibrated confidence. Pilot only acts on a Jev answer above a confidence
threshold and otherwise falls through to the executor model, so Jev can only
save time, never silently make things worse.

Experimental: Jev is waitlisted, and image input is behind the
`TypeSafe-Danger-Zone-Images` header. Enabled when TYPESAFE_API_KEY is set and
`--jev` is passed.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..types import ImagePart

ENDPOINT = "https://api.typesafe.ai/v1/systemone"


@dataclass
class JevAnswer:
    value: Any
    confidence: float
    latency: float


class Jev:
    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 6.0):
        self.key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        self.model = model or os.environ.get("PILOT_JEV_MODEL", "jev-latest")
        self.timeout = timeout
        self.calls = 0
        self.seconds = 0.0

    @property
    def available(self) -> bool:
        return bool(self.key)

    def _ask(self, state: dict[str, Any], questions: dict[str, Any], image: ImagePart | None) -> tuple[dict[str, Any], float]:
        body: dict[str, Any] = {"model": self.model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        if image is not None:
            fmt = "png" if image.media_type.endswith("png") else "jpg"
            body["assets"] = {"pilot-screen-0001": {"type": "image", "format": fmt, "data_base64": image.b64}}
            headers["TypeSafe-Danger-Zone-Images"] = "true"
        req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), headers=headers, method="POST")
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        dt = time.monotonic() - t0
        self.calls += 1
        self.seconds += dt
        return data.get("answers", {}), dt

    @staticmethod
    def _conf(ans: dict[str, Any]) -> float:
        for k in ("confidence", "probability", "p"):
            if isinstance(ans.get(k), (int, float)):
                return float(ans[k])
        return 0.0

    def choose(self, instructions: str, options: dict[str, str], image: ImagePart | None = None, context: str = "") -> JevAnswer:
        state: dict[str, Any] = {"ui": context or "(see screen)"}
        if image is not None:
            state["screen"] = "pilot-screen-0001"
        answers, dt = self._ask(state, {"pick": {"type": "choice", "instructions": instructions, "criteria": options}}, image)
        a = answers.get("pick", {})
        value = next((a[k] for k in ("choice", "value", "answer", "option", "selected") if k in a), None)
        return JevAnswer(value, self._conf(a), dt)

    def yes_no(self, instructions: str, image: ImagePart | None = None, context: str = "") -> JevAnswer:
        state: dict[str, Any] = {"ui": context or "(see screen)"}
        if image is not None:
            state["screen"] = "pilot-screen-0001"
        answers, dt = self._ask(state, {"check": {"type": "noul", "instructions": instructions}}, image)
        a = answers.get("check", {})
        value = next((a[k] for k in ("value", "answer", "noul", "result") if k in a), None)
        conf = self._conf(a)
        if value is None and "probability" in a:  # probability of "yes"
            p = float(a["probability"])
            value, conf = p >= 0.5, max(p, 1 - p)
        if isinstance(value, str):
            value = value.strip().lower() in {"yes", "true", "1"}
        return JevAnswer(bool(value) if value is not None else None, conf, dt)

    def box(self, description: str, image: ImagePart) -> JevAnswer:
        """Normalised (left, top, right, bottom) of the described element."""
        answers, dt = self._ask(
            {"screen": "pilot-screen-0001"},
            {"target": {"type": "box", "instructions": f"the UI element in `screen`: {description}"}},
            image,
        )
        a = answers.get("target", {})
        b = a.get("box") or {}
        value = (b["left"], b["top"], b["right"], b["bottom"]) if {"left", "top", "right", "bottom"} <= b.keys() else None
        return JevAnswer(value, self._conf(a), dt)
