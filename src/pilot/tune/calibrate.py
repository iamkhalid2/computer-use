"""`pilot tune calibrate`: measure a model, then write a profile tuned to it.

Three measurements, each one model call per item, no screen involved:
  1. coordinate convention   synthetic grounding probes in pixels and in 0-1000
  2. element ids             does the element list help this model, or is it dead weight?
  3. effort sweep            the same fixed replay set at each effort: decision quality,
                             protocol compliance, latency, tokens
Decision rule (cost / speed / quality frontier): among efforts scoring within
`tolerance` items of the best, take the fastest. Safety items are reported loudly
but never traded: the invariant guards protect the run whatever the score.

The result is a new profile derived from a base profile, with every calibrated knob
citing its measurement, plus a JSON report with all raw results.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..config import parse_spec
from ..profile import Profile
from ..providers import get_provider
from ..replay.runner import replay, summarise
from . import probes

EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]


@dataclass
class CalibrationConfig:
    efforts: tuple[str, ...] = ("low", "medium")
    frames: int = 12
    layouts: int = 8
    parallel: int = 2
    tolerance: int = 1  # items a cheaper effort may trail the best by
    sizes: tuple[int, ...] = (1280, 1024)  # smaller screenshot widths to try: fewer tokens, faster calls

    def budget(self, native: bool) -> int:
        coord = 0 if native else 2 * self.layouts
        return coord + self.layouts + self.layouts * len(self.sizes) + self.frames * len(self.efforts)


def calibration_set(frames: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Deterministic and model-independent: one hard item per failure type, then soft items across tasks."""
    hard = sorted((f for f in frames if f["label"]["kind"] == "hard"), key=lambda f: f["id"])
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in hard:
        key = next((t for t in f["label"].get("tags", []) if t not in {"safety"}), "safety")
        if key not in seen and len(picked) < max(1, n // 2):
            picked.append(f)
            seen.add(key)
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in sorted((f for f in frames if f["label"]["kind"] == "soft"), key=lambda f: f["id"]):
        by_task[(f.get("outcome") or {}).get("task_id") or "?"].append(f)
    while len(picked) < n and any(by_task.values()):
        for k in sorted(by_task):
            if by_task[k] and len(picked) < n:
                picked.append(by_task[k].pop(len(by_task[k]) // 2))
    return picked


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def protocol(results: list[dict[str, Any]], tool_names: set[str]) -> dict[str, int]:
    out = {"no_action": 0, "unknown_tool": 0, "invalid_args": 0, "errors": 0}
    for r in results:
        if "error" in r:
            out["errors"] += 1
            continue
        if not r["calls"]:
            out["no_action"] += 1
        out["unknown_tool"] += sum(1 for c in r["calls"] if c["name"] not in tool_names)
        out["invalid_args"] += sum(1 for c in r["calls"] if "__invalid__" in (c.get("args") or {}))
    return out


def choose_effort(sweep: dict[str, dict[str, Any]], tolerance: int) -> tuple[str, str]:
    scored = {e: s["score"] for e, s in sweep.items()}
    best = max(scored.values())
    near = [e for e in sweep if scored[e] >= best - tolerance]
    pick = min(near, key=lambda e: (sweep[e]["median_latency"] or 1e9, EFFORT_ORDER.index(e)))
    why = (f"{pick}: scored {scored[pick]}/{sweep[pick]['graded']} at a median {sweep[pick]['median_latency']} s; "
           + "; ".join(f"{e} scored {scored[e]} at {sweep[e]['median_latency']} s" for e in sweep if e != pick)
           + f" (fastest effort within {tolerance} item(s) of the best)")
    return pick, why


def calibrate(model: str, base: Profile, frames: list[dict[str, Any]], root: Path, cfg: CalibrationConfig,
              out_dir: Path, name: str | None = None, log: Callable[[str], None] = print) -> tuple[Profile, Path]:
    spec = parse_spec(model)
    native = get_provider(spec).native_computer
    report: dict[str, Any] = {"model": model, "started": datetime.now().isoformat(timespec="seconds"), "config": vars(cfg)}
    scale_edge: int | None = None
    scale_why = ""
    cheapest = replace(spec, effort=min(cfg.efforts, key=EFFORT_ORDER.index), thinking="off")
    layouts = probes.LAYOUTS[: cfg.layouts]

    # 1. coordinate convention
    if native:
        coords, coord_why = "auto", "auto: the provider's native computer tool fixes the convention (pixels)."
        report["coords"] = {"skipped": "native computer tool"}
    else:
        hits: dict[str, list[probes.ProbeResult]] = {}
        for space in ("pixels", "norm1000"):
            hits[space] = [probes.run(cheapest, space, "grounding", lay) for lay in layouts]
            log(f"  grounding in {space:8}: " + ", ".join(f"{'hit' if r.hit else 'miss'} ({r.detail})" for r in hits[space]))
        rate = {s: sum(r.hit for r in v) for s, v in hits.items()}
        coords = max(rate, key=lambda s: (rate[s], s == spec.coord_space))
        coord_why = (f"{coords}: grounding probes hit {rate[coords]}/{len(layouts)} with {coords} coordinates vs "
                     f"{rate['norm1000' if coords == 'pixels' else 'pixels']}/{len(layouts)} with the other convention.")
        report["coords"] = {s: [vars(r) for r in v] for s, v in hits.items()}

        # 1b. hidden downscale: misses with one consistent ratio mean the screenshot exceeded the
        # model's image budget and was shrunk, so it answers in the shrunken frame.
        scale = hidden_scale(hits["pixels"])
        if scale is not None and rate["pixels"] < len(layouts):
            edge = int(max(probes.W, probes.H) * scale * 0.97)
            size = (edge, round(probes.H * edge / probes.W))
            retry = [probes.run(cheapest, "pixels", "grounding", lay, size=size) for lay in layouts]
            log(f"  hidden downscale ~{scale:.3f}: re-probing at {size[0]}x{size[1]}: "
                + ", ".join(f"{'hit' if r.hit else 'miss'} ({r.detail})" for r in retry))
            report["scale"] = {"estimate": scale, "size": size, "retry": [vars(r) for r in retry]}
            if sum(r.hit for r in retry) > rate["pixels"]:
                coords, rate["pixels"] = "pixels", sum(r.hit for r in retry)
                scale_edge = edge
                scale_why = (f"{edge}: at {probes.W} px the model's clicks were scaled by {scale:.3f} on both axes (a hidden resize "
                             f"to its image budget); at {edge} px grounding probes hit {rate['pixels']}/{len(layouts)}.")

    # 2. element ids
    spaced = replace(cheapest, coords=coords)
    ids = [probes.run(spaced, coords if coords != "auto" else "pixels", "ids", lay) for lay in layouts]
    log("  element ids        : " + ", ".join(f"{'hit' if r.hit else 'miss'} ({r.detail})" for r in ids))
    used_ids = sum(r.detail.startswith("ui") for r in ids)
    report["ids"] = [vars(r) for r in ids]
    grounding_ok = native or max(sum(r["hit"] for r in v) for v in report["coords"].values() if isinstance(v, list)) >= len(layouts) / 2
    # The list is dead weight only if the model never acts through it and clicks reliably without it.
    use_ax = used_ids > 0 or not grounding_ok
    ax_why = (f"{'on' if use_ax else 'off'}: element-id probes hit {sum(r.hit for r in ids)}/{len(ids)}, acting through ids "
              f"{used_ids}/{len(ids)} times" + ("" if use_ax else "; it never used the list and pixel grounding was reliable, "
                                                  "so the list would only cost tokens."))

    # 2b. screenshot size: the smallest that loses no grounding accuracy (tokens scale with area)
    probe_space = coords if coords != "auto" else "pixels"
    current = scale_edge or probes.W
    base_hits = sum(r["hit"] for r in report["scale"]["retry"]) if scale_edge else (
        len(layouts) if native else max(sum(r["hit"] for r in v) for v in report["coords"].values() if isinstance(v, list)))
    size_rows = {current: base_hits}
    for width in sorted((w for w in cfg.sizes if w < current), reverse=True):
        size = (width, round(probes.H * width / probes.W))
        got = [probes.run(spaced, probe_space, "grounding", lay, size=size) for lay in layouts]
        size_rows[width] = sum(r.hit for r in got)
        log(f"  screenshot {width:4} px  : grounding {size_rows[width]}/{len(layouts)} "
            f"(misses: {', '.join(r.layout for r in got if not r.hit) or 'none'})")
        report.setdefault("sizes", {})[width] = [vars(r) for r in got]
        if size_rows[width] < base_hits:
            break  # accuracy dropped: no point trying smaller
    best_width = min(w for w, h in size_rows.items() if h >= base_hits)
    size_why = ("; ".join(f"{w} px: {h}/{len(layouts)}" for w, h in sorted(size_rows.items(), reverse=True))
                + f" grounding hits. {best_width} px is the smallest without losing accuracy"
                + (f" (~{round(100 * (best_width / probes.W) ** 2)}% of full-size visual tokens)." if best_width < probes.W else "."))

    # 3. effort sweep on the fixed replay set
    subset = calibration_set(frames, cfg.frames)
    from ..config import Settings
    from ..tools import build_specs

    tool_names = {t.name for t in build_specs(Settings(planner=None, executor=spec, interactive=False), "pixels", (1470, 956),
                                              planner_mode=True, ax=True)}
    sweep: dict[str, dict[str, Any]] = {}
    raw: dict[str, list[dict[str, Any]]] = {}
    for effort in cfg.efforts:
        es = replace(spec, effort=effort, coords=coords)
        results = replay(subset, es, root, parallel=cfg.parallel)
        s = summarise(results)
        hp, hn = (int(x) for x in s["hard"].split("/"))
        sp_, sn = (int(x) for x in s["soft_agreement"].split("/"))
        sweep[effort] = {"score": hp + sp_, "graded": hn + sn, "hard": s["hard"], "hard_by_tag": s["hard_by_tag"],
                         "soft": s["soft_agreement"], "ungradable": s["ungradable"], "median_latency": s["median_latency"],
                         "mean_tokens_in": s["mean_tokens_in"], "protocol": protocol(results, tool_names)}
        raw[effort] = results
        log(f"  effort {effort:6}: hard {s['hard']} · soft {s['soft_agreement']} · median {s['median_latency']} s · "
            f"protocol {sweep[effort]['protocol']}")
        if any("usage limit" in r.get("error", "").lower() for r in results):
            log("  stopped: plan usage limit reached")
            break
    report.update(sweep=sweep, frames=[f["id"] for f in subset])
    effort, effort_why = choose_effort(sweep, cfg.tolerance)
    safety = {e: v["hard_by_tag"].get("safety") for e, v in sweep.items()}
    report["safety"] = safety

    out_dir.mkdir(parents=True, exist_ok=True)
    rpath = out_dir / f"calibrate-{slug(model)}-{datetime.now():%Y%m%d-%H%M%S}.json"
    rpath.write_text(json.dumps({**report, "results": raw, "chosen": {"effort": effort, "coords": coords, "use_ax": use_ax,
                                                                     "max_long_edge": best_width}},
                                indent=1, default=str))
    cite = f" [calibration {rpath.name}]"
    extra = {"max_long_edge": best_width} if best_width != probes.W else {}
    prof = base.mutate(name or f"{base.name}-{slug(model)}", executor=model, executor_effort=effort, executor_coords=coords,
                       executor_fallbacks=[], use_ax=use_ax, **extra)
    prof.why.update({"executor": f"calibrated candidate executor{cite}", "executor_effort": effort_why + cite,
                     "executor_coords": coord_why + cite, "use_ax": ax_why + cite, "executor_fallbacks": "none: calibrated alone."})
    if best_width != probes.W or scale_edge:
        prof.why["max_long_edge"] = (scale_why + " " if scale_edge else "") + size_why + cite
    return prof, rpath


def hidden_scale(results: list[Any]) -> float | None:
    """A consistent shrink factor across probes and both axes, or None."""
    ratios = []
    for r in results:
        if r.raw is None or r.center is None:
            return None
        ratios += [r.raw[0] / r.center[0], r.raw[1] / r.center[1]]
    if len(ratios) < 4:
        return None
    mean = statistics.mean(ratios)
    if 0.55 <= mean <= 0.98 and max(ratios) - min(ratios) <= 0.06:
        return mean
    return None


def median_or_none(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None
