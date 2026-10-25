"""Terminal output. Quiet by default: one line per action, with timing."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.prompt import Confirm, Prompt


def _args(name: str, args: dict[str, Any]) -> str:
    if name == "computer":
        a = dict(args)
        act = a.pop("action", "?")
        bits = [act] + [json.dumps(v, ensure_ascii=False) if not isinstance(v, (int, float)) else str(v) for v in a.values()]
        return " ".join(bits)[:120]
    return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())[:120]


class Reporter:
    def __init__(self, verbose: bool = False, quiet: bool = False):
        self.c = Console(highlight=False)
        self.verbose = verbose
        self.quiet = quiet

    def header(self, task: str, settings: Any) -> None:
        if self.quiet:
            return
        plan = settings.planner.label if settings.planner else "none"
        self.c.print(f"[bold]Pilot[/] · planner [cyan]{plan}[/] · executor [cyan]{settings.executor.label}[/] · approve {settings.approve}")
        self.c.print(f"[bold]Task:[/] {task}")
        self.c.print("[dim]Stop any time: Ctrl+C, or move the mouse into the top-left corner.[/]")

    def plan(self, data: dict[str, Any], seconds: float, label: str = "Plan") -> None:
        if self.quiet:
            return
        self.c.print(f"[magenta]◆ {label}[/] [dim]({seconds:.1f}s)[/] {data.get('strategy') or data.get('advice') or ''}")
        for i, s in enumerate(data.get("steps", []), 1):
            self.c.print(f"   {i}. {s.get('goal')}  [dim]→ {s.get('done_when')}[/]")

    def step(self, i: int, n: int, goal: str) -> None:
        if not self.quiet:
            self.c.print(f"[bold blue]▸ Step {i}/{n}[/] {goal}")

    def model(self, role: str, latency: float, text: str = "") -> None:
        if self.verbose and not self.quiet:
            self.c.print(f"   [dim]{role} {latency:.2f}s {text[:160]}[/]")

    def note(self, text: str) -> None:
        if not self.quiet:
            self.c.print(f"   [dim italic]· {text.strip()[:200]}[/]")

    def action(self, name: str, args: dict[str, Any], ok: bool, result: str, seconds: float) -> None:
        if self.quiet:
            return
        mark = "[green]✓[/]" if ok else "[red]✗[/]"
        tail = "" if ok and not self.verbose else f" [dim]{result[:140]}[/]"
        self.c.print(f"   {mark} {name} {_args(name, args)} [dim]{seconds:.2f}s[/]{tail}")

    def info(self, text: str) -> None:
        if not self.quiet:
            self.c.print(f"   [yellow]{text}[/]")

    def final(self, success: bool, answer: str, summary: str, stats: Any, seconds: float, report: str) -> None:
        cost = f"${stats.cost:.4f}" if stats.cost_known else "cost n/a"
        mark = "[bold green]✔ Done[/]" if success else "[bold red]✘ Not done[/]"
        self.c.print(f"{mark} in {seconds:.1f}s · {stats.model_calls} model calls ({stats.model_seconds:.1f}s) · {stats.actions} actions · {cost}")
        if answer:
            self.c.print(f"[bold]Answer:[/] {answer}")
        elif summary:
            self.c.print(f"[dim]{summary}[/]")
        self.c.print(f"[dim]Report: {report}[/]")

    def confirm(self, prompt: str) -> bool:
        return Confirm.ask(f"[bold yellow]Allow?[/] {prompt}", default=False, console=self.c)

    def ask(self, question: str) -> str:
        return Prompt.ask(f"[bold cyan]Pilot asks:[/] {question}", console=self.c)
