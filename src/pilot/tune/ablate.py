"""`pilot tune ablate`: find the scaffolding a model no longer needs.

Two kinds of evidence, matched to what each knob affects:

  replay ablation  knobs that change a single decision's input (optional prompt rules,
                   element-list length, the element list itself). The same fixed set of
                   recorded decisions is re-asked under each variant and compared, paired,
                   against the profile's baseline. Prompt rules are searched by bisection
                   (adaptive group testing), so dead weight is found in far fewer calls
                   than one test per rule.
  trace audit      scaffolds that act across turns (disk check before done, loop and
                   no-change notes, budget warning, nudge, waiting for editable fields).
                   Their logged activations are read back: how often each fired and
                   whether the model changed course afterwards.

A variant is acceptable only if it keeps every hard item the baseline passed and
loses at most `tolerance` soft items. The output is a *candidate* profile: replay
sees single decisions, so a removal must be confirmed with `pilot eval` before use.
Safety rules are not candidates: they are invariant.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..agent import prompts
from ..config import specs_from_profile
from ..profile import Profile
from ..replay.runner import replay
from .calibrate import calibration_set, slug


@dataclass
class AblationConfig:
    frames: int = 10
    budget: int = 120  # model calls
    tolerance: int = 1
    parallel: int = 2
    outline_lines: tuple[int, ...] = (80, 40)


# Rules written to prevent a known failure. Replay can only clear one for removal when the
# ablation set holds a hard item of that failure; otherwise "no loss" would just mean "not tested".
RULE_GUARDS: dict[str, set[str]] = {
    "impossible": {"impossible"},
    "done": {"false-done"},
    "exact_url": {"stale-state"},
    "check_after": {"stale-state", "verification"},
}


# Rules whose effect is on the sequence of turns, not on one decision: not looking again for its own
# sake, changing approach after a failure, staying with element actions after one of them errors.
# PilotBench showed it (apply-form: 9 waits and 8 zooms without them, against 0 and 5 with them), and replay can't, so
# replay never proposes removing them.
EPISODE_RULES = {"batching", "check_after", "action_priority"}


class Budget:
    def __init__(self, calls: int):
        self.left = calls

    def can(self, n: int) -> bool:
        return self.left >= n

    def spend(self, n: int) -> None:
        self.left -= n


def score(results: list[dict[str, Any]], exclude: set[str] = frozenset()) -> dict[str, Any]:
    done = [r for r in results if "error" not in r and r.get("pass") is not None and r["id"] not in exclude]
    hard = {r["id"] for r in done if r["kind"] == "hard" and r["pass"]}
    soft = sum(1 for r in done if r["kind"] != "hard" and r["pass"])
    return {"hard_passed": hard, "soft": soft, "graded": len(done),
            "errors": sum(1 for r in results if "error" in r),
            "tokens_in": round(statistics.mean(r["tokens_in"] for r in done)) if done else None,
            "latency": statistics.median(r["latency"] for r in done) if done else None,
            "actions": round(statistics.mean(r.get("actions", 0) for r in done), 2) if done else None}


def acceptable(base: dict[str, Any], var: dict[str, Any], tolerance: int) -> tuple[bool, str]:
    lost = base["hard_passed"] - var["hard_passed"]
    if var["errors"]:
        return False, f"{var['errors']} errors"
    if lost:
        return False, f"lost hard item(s) {sorted(lost)}"
    if var["soft"] < base["soft"] - tolerance:
        return False, f"soft {var['soft']} vs baseline {base['soft']}"
    return True, f"soft {var['soft']} vs {base['soft']}, hard kept"


def flaky(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> set[str]:
    """Items whose verdict differs between two identical baseline runs: noise, not signal."""
    pa = {r["id"]: r.get("pass") for r in a if "error" not in r}
    pb = {r["id"]: r.get("pass") for r in b if "error" not in r}
    return {i for i in pa.keys() & pb.keys() if pa[i] != pb[i]}


GROUNDED_CLICKS = {"left_click", "double_click", "right_click", "triple_click", "left_click_drag", "left_mouse_down"}


def grounded(frame: dict[str, Any]) -> bool:
    """The recorded decision acted on something seen: an element, or a point on the screenshot.

    Only these separate a model that perceives from one that guesses from the task text: a blind
    model passes most first-turn `open`/`shell` decisions and every forbid-only safety item."""
    for e in frame.get("executed") or []:
        a = e.get("args") or {}
        if e["name"] == "ui" and (a.get("id") or a.get("target")):
            return True
        if e["name"] == "computer" and a.get("action") in GROUNDED_CLICKS and a.get("coordinate"):
            return True
    return False


def ablation_set(frames: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """A quarter hard items (one per failure type, so safety regressions still show), the rest
    grounded soft decisions spread across tasks, recorded ones (with element snapshots) first."""
    hard = calibration_set([f for f in frames if f["label"]["kind"] == "hard"], max(1, n // 4) * 2)[: max(1, n // 4)]
    soft = [f for f in frames if f["label"]["kind"] == "soft" and grounded(f)]
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in sorted(soft, key=lambda f: (f.get("source") != "recorded", f["id"])):
        by_task[(f.get("outcome") or {}).get("task_id") or "?"].append(f)
    picked = list(hard)
    while len(picked) < n and any(by_task.values()):
        for k in sorted(by_task):
            if by_task[k] and len(picked) < n:
                picked.append(by_task[k].pop(0))
    return picked


def ablate(profile: Profile, frames: list[dict[str, Any]], root: Path, cfg: AblationConfig, out_dir: Path,
           log: Callable[[str], None] = print) -> tuple[Profile, Path]:
    """Protocol, in order; every step is skipped (and nothing proposed from it) once the budget runs out:

      1. baseline twice      items that disagree with themselves are noise and set aside
      2. blind control       no screenshot, no list: if the set can't tell this from the baseline
                             it can't tell anything, and no change is proposed
      3. list length         shorter lists, each tested on top of what was already accepted
      4. no element list     reported only: the list's value is in execution (exact presses),
                             which single-decision replay can't see
      5. prompt rules        bisection, cumulative, guard rules kept unless their failure is in the set
    Accepted changes are cumulative, so the last accepted test *is* the candidate, tested whole.
    """
    _, spec = specs_from_profile(profile)
    subset = ablation_set(frames, cfg.frames)
    n = len(subset)
    budget = Budget(cfg.budget)
    report: dict[str, Any] = {"profile": profile.name, "executor": spec.label, "frames": [f["id"] for f in subset], "tests": []}
    noise: set[str] = set()

    def run(label: str, rules: list[str], variant: dict[str, Any] | None = None) -> tuple[dict[str, Any], list] | None:
        if not budget.can(n):
            log(f"  [budget] skipped {label}")
            return None
        budget.spend(n)
        res = replay(subset, spec, root, system=prompts.build_executor(rules), parallel=cfg.parallel, variant=variant)
        sc = score(res, noise)
        report["tests"].append({"label": label, "rules": rules, "variant": variant, **{k: (sorted(v) if isinstance(v, set) else v) for k, v in sc.items()},
                                "results": res})
        if any("usage limit" in r.get("error", "").lower() for r in res):
            budget.left = 0
            log("  stopped: plan usage limit reached")
        return sc, res

    rules = list(profile.executor_rules)
    lines = profile.outline_max_lines
    first, second = run("baseline", rules, {"outline_max_lines": lines}), run("baseline (repeat)", rules, {"outline_max_lines": lines})
    if first is None or second is None:
        raise RuntimeError(f"budget too small: the baseline alone needs {2 * n} calls")
    noise |= flaky(first[1], second[1])
    base = score(first[1], noise)
    soft_n = base["graded"] - sum(1 for f in subset if f["label"]["kind"] == "hard" and f["id"] not in noise)
    log(f"  baseline (x2)      : hard {len(base['hard_passed'])} · soft {base['soft']}/{soft_n} · {base['tokens_in']} tokens in · "
        f"{base['actions']} actions/turn · median {base['latency']} s · noisy items set aside: {len(noise)}")
    decisions: dict[str, Any] = {"noisy_items": sorted(noise)}
    accepted = "baseline"

    def verdict(label: str, out: tuple[dict[str, Any], list] | None) -> bool:
        if out is None:
            return False
        sc = out[0]
        ok, why = acceptable(base, sc, cfg.tolerance)
        log(f"  {label[:19]:19}: {'OK ' if ok else 'NO '} {why} · {sc['tokens_in']} tokens · {sc['actions']} actions/turn · median {sc['latency']} s")
        return ok

    # Power check: a test set that can't see the difference from a blind model has nothing to say.
    control = run("blind control", rules, {"blind": True})
    powered = control is not None and not verdict("blind control", control)
    decisions["powered"] = powered
    best_lines, dropped, untested = lines, [], []
    if not powered:
        log("  no power           : the set can't tell a blind model from the baseline; proposing nothing")
    else:
        for L in sorted((x for x in cfg.outline_lines if x < lines), reverse=True):
            if verdict(f"list {L} lines", run(f"outline_max_lines={L}", rules, {"outline_max_lines": L})):
                best_lines, accepted = L, f"outline_max_lines={L}"
            else:
                break

        no_ax = run("use_ax=false (info)", rules, {"use_ax": False, "outline_max_lines": best_lines})
        if no_ax:
            ok = verdict("no element list", no_ax)
            decisions["no_element_list"] = ("decisions held up without it" if ok else "decisions got worse without it") + \
                "; replay can't see execution reliability, so use_ax is not changed from replay"

        variant = {"outline_max_lines": best_lines}
        covered = {t for f in subset if f["label"]["kind"] == "hard" and f["id"] not in noise for t in f["label"].get("tags", [])}
        untested = [r for r in rules if RULE_GUARDS.get(r) and not RULE_GUARDS[r] & covered]
        if untested:
            log(f"  kept untested      : {untested} (no stable hard item of the failure they guard)")

        def removable(cands: list[str], removed: list[str]) -> list[str]:
            nonlocal accepted
            if not cands:
                return []
            keep = [r for r in rules if r not in removed and r not in cands]
            label = f"remove {cands}"
            if verdict("-" + "/".join(cands), run(label, keep, variant)):
                accepted = label
                return cands
            if len(cands) == 1:
                return []
            mid = len(cands) // 2
            a = removable(cands[:mid], removed)
            return a + removable(cands[mid:], removed + a)

        episode = [r for r in rules if r in EPISODE_RULES]
        if episode:
            log(f"  not replayable     : {episode} (they act across turns; only pilot eval can test them)")
        dropped = removable([r for r in rules if r not in untested and r not in EPISODE_RULES], [])
    decisions["executor_rules"] = [r for r in rules if r not in dropped]
    decisions["untested_rules"] = untested
    decisions["candidate_test"] = accepted
    # A loss smaller than this can't be seen: any hard item, or more than `tolerance` soft ones.
    decisions["min_detectable_loss"] = f"{cfg.tolerance + 1} of {soft_n} soft items, or any of {len(base['hard_passed'])} hard"

    report["audit"] = audit(Path("runs"))
    report["decisions"] = decisions
    report["calls_used"] = cfg.budget - budget.left
    out_dir.mkdir(parents=True, exist_ok=True)
    rpath = out_dir / f"ablate-{slug(profile.name)}-{datetime.now():%Y%m%d-%H%M%S}.json"
    rpath.write_text(json.dumps(report, indent=1, default=str))
    cite = (f" [ablation {rpath.name}: single decisions, losses under {decisions['min_detectable_loss']} unseen; "
            "confirm with pilot eval before adopting]")
    changes: dict[str, Any] = {}
    why: dict[str, str] = {}
    if best_lines != lines:
        changes["outline_max_lines"] = best_lines
        why["outline_max_lines"] = f"{best_lines}: no measurable loss vs {lines} lines on {n - len(noise)} stable recorded decisions" + cite
    if dropped:
        changes["executor_rules"] = decisions["executor_rules"]
        why["executor_rules"] = f"dropped {dropped}: no measurable loss without them" + cite
    cand = profile.mutate(f"{profile.name}-lean", **changes)
    cand.why.update(why)
    return cand, rpath


# -- trace audit of multi-turn scaffolds ---------------------------------------------------------
def _turns(events: list[dict[str, Any]]) -> list[tuple[int, str]]:
    return [(i, json.dumps(e.get("calls"), sort_keys=True)) for i, e in enumerate(events) if e["kind"] == "model" and e.get("role") == "executor"]


def audit(runs_dir: Path) -> dict[str, Any]:
    """For each scaffold activation: did the model change course afterwards?"""
    stats: dict[str, Counter] = defaultdict(Counter)
    for f in sorted(runs_dir.glob("*/events.jsonl")):
        events = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        turns = _turns(events)
        for i, e in enumerate(events):
            name = e.get("name") if e["kind"] == "scaffold" else ("evidence_gate" if e["kind"] == "evidence_gate" else None)
            if not name:
                continue
            stats[name]["fired"] += 1
            before = next((sig for j, sig in reversed(turns) if j < i), None)
            after_evt = next((events[j] for j, _ in turns if j > i), None)
            after = json.dumps(after_evt.get("calls"), sort_keys=True) if after_evt else None
            if name == "await_editable":
                stats[name]["waited" if e.get("waited", 0) > 0.05 and e.get("found") else ("timed_out" if not e.get("found") else "instant")] += 1
            elif name == "evidence_gate":
                calls = (after_evt or {}).get("calls") or []
                changed = any(c["name"] != "done" or (c.get("args") or {}).get("success") is False for c in calls)
                stats[name]["changed" if changed else "confirmed"] += 1
            elif name == "nudge":
                stats[name]["acted" if after_evt and after_evt.get("calls") else "ended"] += 1
            else:
                stats[name]["changed" if after and after != before else "same"] += 1
    out = {}
    for name, c in stats.items():
        helped = c["changed"] + c["acted"] + c["waited"]
        n = c["fired"]
        if n < 3:
            verdict = "insufficient data"
        elif helped:
            verdict = f"keep: changed behaviour {helped}/{n} times"
        else:
            # Zero catches in n tries bounds the catch rate below 3/n at 95% (rule of three), not at zero:
            # a check for a rare failure can look useless here and still be worth its cost.
            verdict = f"no effect in {n} fires (catch rate < {min(1, 3 / n):.0%} at 95%); candidate off if that rate is worth less than its cost"
        out[name] = {**dict(c), "verdict": verdict}
    return out
