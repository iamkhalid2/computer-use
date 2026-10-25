"""Run traces: events.jsonl, screenshots, and a self-contained HTML report.

The report breaks wall-clock time into model latency, action time and
observation time, so speed work is driven by measurement rather than guesses.
"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .types import ImagePart


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


@dataclass
class Stats:
    model_calls: int = 0
    model_seconds: float = 0.0
    action_seconds: float = 0.0
    observe_seconds: float = 0.0
    actions: int = 0
    escalations: int = 0
    cost: float = 0.0
    cost_known: bool = True
    by_role: dict[str, dict[str, float]] = field(default_factory=dict)

    def model(self, role: str, seconds: float, cost: float | None, tokens_in: int, tokens_out: int) -> None:
        self.model_calls += 1
        self.model_seconds += seconds
        if cost is None:
            self.cost_known = False
        else:
            self.cost += cost
        r = self.by_role.setdefault(role, {"calls": 0, "seconds": 0.0, "in": 0, "out": 0})
        r["calls"] += 1
        r["seconds"] += seconds
        r["in"] += tokens_in
        r["out"] += tokens_out


class Trace:
    def __init__(self, root: Path, task: str, meta: dict[str, Any]):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = root / f"{stamp}-{_slug(task)}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.task = task
        self.t0 = time.monotonic()
        self.events: list[dict[str, Any]] = []
        self.images = 0
        self.stats = Stats()
        self._f = (self.dir / "events.jsonl").open("a")
        self.log("start", task=task, **meta)

    def log(self, kind: str, /, **data: Any) -> dict[str, Any]:
        ev = {"t": round(time.monotonic() - self.t0, 3), **data, "kind": kind}
        self.events.append(ev)
        self._f.write(json.dumps(ev, default=str) + "\n")
        self._f.flush()
        return ev

    def image(self, img: ImagePart, label: str) -> str:
        self.images += 1
        ext = "png" if img.media_type.endswith("png") else "jpg"
        name = f"{self.images:03d}-{_slug(label)}.{ext}"
        (self.dir / name).write_bytes(img.data)
        return name

    def finish(self, outcome: dict[str, Any]) -> Path:
        s = self.stats
        summary = {
            "task": self.task,
            "seconds": round(time.monotonic() - self.t0, 2),
            "model_calls": s.model_calls,
            "model_seconds": round(s.model_seconds, 2),
            "action_seconds": round(s.action_seconds, 2),
            "observe_seconds": round(s.observe_seconds, 2),
            "actions": s.actions,
            "escalations": s.escalations,
            "cost_usd": round(s.cost, 4) if s.cost_known else None,
            "by_role": s.by_role,
            **outcome,
        }
        self.log("finish", **summary)
        (self.dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        self._f.close()
        (self.dir / "report.html").write_text(render_report(summary, self.events))
        return self.dir / "report.html"


def render_report(summary: dict[str, Any], events: list[dict[str, Any]]) -> str:
    e = html.escape
    total = max(summary.get("seconds") or 0.001, 0.001)
    bars = [("model", summary["model_seconds"], "#6d5efc"), ("actions", summary["action_seconds"], "#1fa971"),
            ("observe", summary["observe_seconds"], "#e0a100")]
    other = max(total - sum(b[1] for b in bars), 0)
    bars.append(("other", other, "#9aa0a6"))
    bar_html = "".join(f'<div style="flex:{v / total:.4f};background:{c}" title="{n}: {v:.1f}s"></div>' for n, v, c in bars if v > 0)
    legend = " ".join(f'<span><i style="background:{c}"></i>{n} {v:.1f}s</span>' for n, v, c in bars)
    rows = []
    for ev in events:
        k = ev["kind"]
        t = f'<td class="t">{ev["t"]:.1f}s</td>'
        if k == "plan" or k == "replan" or k == "verify":
            body = e(json.dumps({x: ev[x] for x in ev if x not in {"t", "kind"}}, indent=1, default=str))
            rows.append(f'<tr class="{k}">{t}<td><b>{k}</b><pre>{body}</pre></td></tr>')
        elif k == "model":
            notes = "".join(f"<div class=note>{e(n)}</div>" for n in ev.get("notes", []))
            txt = f"<div>{e(ev.get('text', ''))}</div>" if ev.get("text") else ""
            rows.append(f'<tr class="model">{t}<td><b>{e(ev["role"])}</b> · {e(ev.get("model", ""))} · {ev["latency"]:.2f}s · '
                        f'{ev.get("in", 0)}→{ev.get("out", 0)} tok{notes}{txt}</td></tr>')
        elif k == "action":
            ok = "ok" if ev.get("ok") else "err"
            rows.append(f'<tr class="act {ok}">{t}<td><code>{e(ev["name"])}({e(json.dumps(ev.get("args", {}), default=str)[:300])})</code>'
                        f' → {e(str(ev.get("result", ""))[:400])} <span class=d>{ev.get("seconds", 0):.2f}s</span></td></tr>')
        elif k == "observe" and ev.get("image"):
            rows.append(f'<tr>{t}<td><img loading=lazy src="{e(ev["image"])}"><div class=d>{e(ev.get("app", ""))} · '
                        f'capture {ev.get("capture_ms", 0):.0f}ms · ax {ev.get("ax_ms", 0):.0f}ms</div></td></tr>')
        elif k in {"step", "escalate", "note", "error", "finish"}:
            body = e(json.dumps({x: ev[x] for x in ev if x not in {"t", "kind"}}, default=str)[:800])
            rows.append(f'<tr class="{k}">{t}<td><b>{k}</b> {body}</td></tr>')
    status = "✅ success" if summary.get("success") else "❌ failed"
    cost = f"${summary['cost_usd']:.4f}" if summary.get("cost_usd") is not None else "n/a"
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pilot run</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--card:#f5f5f7;--line:#e5e5ea}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#f5f5f7;--mut:#a1a1a6;--card:#1c1c1e;--line:#2c2c2e}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif;margin:0 auto;max-width:980px;padding:24px 16px}}
h1{{font-size:20px;margin:0 0 4px}} .mut{{color:var(--mut)}} .bar{{display:flex;height:14px;border-radius:7px;overflow:hidden;margin:14px 0 6px}}
.legend span{{margin-right:14px}} .legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:14px 0}}
.grid div{{background:var(--card);border-radius:10px;padding:10px}} .grid b{{display:block;font-size:18px}}
table{{width:100%;border-collapse:collapse}} td{{border-top:1px solid var(--line);padding:8px 6px;vertical-align:top}}
td.t{{width:56px;color:var(--mut);font-variant-numeric:tabular-nums}} img{{max-width:100%;border-radius:8px;border:1px solid var(--line)}}
pre{{white-space:pre-wrap;margin:4px 0;font-size:12px}} code{{font-size:12px}} .err code{{color:#d70015}} .d{{color:var(--mut);font-size:12px}}
.note{{color:var(--mut);font-style:italic;margin-top:2px}}</style></head><body>
<h1>{e(summary['task'])}</h1><div class=mut>{status} · {summary['seconds']:.1f}s · {summary['model_calls']} model calls · {summary['actions']} actions · {cost}</div>
<div class=bar>{bar_html}</div><div class="legend mut">{legend}</div>
<div class=grid>{''.join(f"<div><span class=mut>{e(r)}</span><b>{int(v['calls'])} calls</b><span class=mut>{v['seconds']:.1f}s · {int(v['in'])}→{int(v['out'])} tok</span></div>" for r, v in summary.get('by_role', {}).items())}</div>
<p>{e(str(summary.get('answer') or summary.get('summary') or ''))}</p>
<table>{''.join(rows)}</table></body></html>"""
