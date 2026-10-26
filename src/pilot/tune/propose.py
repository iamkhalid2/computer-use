"""`pilot tune propose`: a strong model reads the waste clusters and proposes profile changes.

What it sees: clusters mined from `dev` runs only (never the sealed `test` tasks or the red-team
gate), the profile's current knob values with each knob's type, range and meaning, and the
ledger of past experiments, so a rejected idea is not proposed again without new evidence.

What it may return:
  proposals       single-knob changes, typed and validated against the registry. One knob per
                  candidate, so an accepted gain can be attributed. Invariants are not knobs and
                  can't be proposed.
  harness_notes   waste no knob can fix (a tool bug, an over-cautious safety rule). These go to
                  a human; nothing here edits code or safety.

Each valid proposal becomes a candidate profile and a "proposed" ledger entry. Nothing is
adopted until `pilot tune accept` has measured it on sealed tasks.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import parse_spec, specs_from_profile
from ..profile import KNOBS, Profile, ProfileError
from ..providers import get_provider, inert_knobs
from ..types import TextPart
from .calibrate import slug

LEDGER = Path("profiles/ledger.jsonl")

SYSTEM = """You tune the harness of a computer-use agent on macOS. The harness is configured by a profile of typed knobs; a separate model (the executor) acts on screen.

You get clusters of wasted work mined from real runs: each has a signature, how often it happened, the turns, model seconds and tokens it cost, and raw examples. You also get every knob with its type, allowed values, current value and meaning, and the ledger of past experiments with their outcomes.

Goal: the Pareto frontier of cost, speed, long-horizon reliability and intelligence. Fewer model calls, actions and tokens at no loss of success or safety.

Rules:
- Propose only changes to the listed knobs, one knob per proposal, with a value of the knob's type and range. Tie every proposal to the clusters it addresses and explain the mechanism: why this knob changes this waste.
- Prefer few, high-confidence proposals (0 to 3). Proposing nothing is right when no knob addresses the waste.
- Do not repeat an experiment the ledger shows as rejected unless you name new evidence.
- Waste that no knob can fix (a tool bug, an over-cautious safety check, a provider behaviour) goes in harness_notes, with the evidence and the fix you suggest. Safety behaviour is never a knob: you may only describe it for a human.
- Estimates must be honest: say what you expect to change and what could get worse.
Reply with JSON only."""

SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {"type": "array", "items": {"type": "object", "properties": {
            "knob": {"type": "string"},
            "value_json": {"type": "string", "description": "the new value, JSON-encoded (true, 1024, \"jpeg\", [\"a\",\"b\"])"},
            "clusters": {"type": "array", "items": {"type": "string"}},
            "mechanism": {"type": "string"},
            "expected": {"type": "string"},
            "risk": {"type": "string"},
        }, "required": ["knob", "value_json", "clusters", "mechanism", "expected", "risk"], "additionalProperties": False}},
        "harness_notes": {"type": "array", "items": {"type": "object", "properties": {
            "cluster": {"type": "string"},
            "evidence": {"type": "string"},
            "suggestion": {"type": "string"},
        }, "required": ["cluster", "evidence", "suggestion"], "additionalProperties": False}},
    },
    "required": ["proposals", "harness_notes"],
    "additionalProperties": False,
}


# -- the ledger: what has been tried, and what happened --------------------------------------------
def ledger(path: Path = LEDGER) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def record(entry: dict[str, Any], path: Path = LEDGER) -> dict[str, Any]:
    entry = {"at": datetime.now().isoformat(timespec="seconds"), **entry}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def latest(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The newest entry per candidate: its current status."""
    out: dict[str, dict[str, Any]] = {}
    for e in entries:
        out[e["candidate"]] = e
    return out


# -- the request --------------------------------------------------------------------------------
def knob_table(profile: Profile) -> list[dict[str, Any]]:
    values = profile.resolved()
    inert = inert_knobs(specs_from_profile(profile)[1])  # proposing these would only measure noise
    rows = []
    for k in KNOBS.values():
        if k.kind in {"model", "models"} or k.name in {"executor_coords"} | inert:
            continue  # models and coordinate conventions are calibration's job, measured on probes
        row = {"knob": k.name, "type": k.kind, "current": values[k.name], "meaning": k.doc}
        if k.kind in {"int", "float"}:
            row["range"] = [k.lo, k.hi]
        if k.choices:
            row["choices"] = list(k.choices)
        rows.append(row)
    return rows


def request(profile: Profile, mined: dict[str, Any], entries: list[dict[str, Any]], top: int = 12) -> str:
    clusters = mined["clusters"][:top]
    history = [{k: e.get(k) for k in ("candidate", "knob", "value", "status", "evidence")} for e in latest(entries).values()]
    calls = mined.get("executor_calls") or 0
    return "\n\n".join([
        f"Profile: {profile.name} (executor {profile.executor}, effort {profile.executor_effort}; planner {profile.planner})",
        f"Mined: {mined['runs']} runs from the dev split, {calls} executor calls, "
        f"{mined.get('executor_in', 0) // max(1, calls)} input tokens per executor call on average.",
        "Waste clusters, most turns lost first:\n" + json.dumps(clusters, indent=1),
        "Knobs:\n" + json.dumps(knob_table(profile), indent=1),
        "Ledger of past experiments:\n" + (json.dumps(history, indent=1) if history else "(none yet)"),
    ])


def validate(raw: dict[str, Any], profile: Profile) -> tuple[list[dict[str, Any]], list[str]]:
    """Typed, allowed, actually a change: everything else is dropped with a reason."""
    allowed = {r["knob"] for r in knob_table(profile)}
    current = profile.resolved()
    ok, dropped = [], []
    for p in raw.get("proposals") or []:
        k = p.get("knob")
        try:
            if k not in allowed:
                raise ProfileError(f"{k!r} is not a tunable knob here")
            value = KNOBS[k].validate(json.loads(p.get("value_json", "null")))
            if value == current[k]:
                raise ProfileError(f"{k} is already {value!r}")
        except (ProfileError, json.JSONDecodeError) as e:
            dropped.append(f"{k}: {e}")
            continue
        ok.append({**p, "value": value})
    return ok, dropped


def propose(profile: Profile, mined: dict[str, Any], out_dir: Path, model: str | None = None,
            ledger_path: Path = LEDGER) -> tuple[list[Profile], dict[str, Any], Path]:
    entries = ledger(ledger_path)
    planner, executor = specs_from_profile(profile)
    spec = parse_spec(model) if model else (planner or executor)
    text = request(profile, mined, entries)
    raw, turn = get_provider(spec).complete_json(SYSTEM, [TextPart(text)], SCHEMA)
    props, dropped = validate(raw, profile)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rpath = out_dir / f"propose-{slug(profile.name)}-{stamp}.json"
    cands = []
    for i, p in enumerate(props, start=1):
        name = f"{profile.name}-{p['knob'].replace('_', '-')}-{stamp[-6:]}"
        why = (f"{p['value']!r}: {p['mechanism']} Addresses {', '.join(p['clusters'])}. Expected: {p['expected']} "
               f"Risk: {p['risk']} [proposed by {spec.label} from {mined['runs']} dev runs, {rpath.name}; "
               "unconfirmed until pilot tune accept]")
        cand = profile.mutate(name, why=why, **{p["knob"]: p["value"]})
        cands.append(cand)
        record({"candidate": name, "base": profile.name, "knob": p["knob"], "value": p["value"], "status": "proposed",
                "evidence": f"{rpath.name}: {p['expected']}"}, ledger_path)
    report = {"profile": profile.name, "model": spec.label, "request": text, "raw": raw, "dropped": dropped,
              "candidates": [c.name for c in cands], "seconds": round(turn.latency, 1)}
    rpath.write_text(json.dumps(report, indent=1, default=str))
    return cands, report, rpath
