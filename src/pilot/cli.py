"""pilot - command line entry point.

    pilot "open example.com in Safari and tell me the heading"
    pilot                       interactive session
    pilot doctor                permissions, credentials, speed check
    pilot observe               what the agent sees right now (no model calls)
    pilot eval                  graded task suite; compare presets
    pilot replay build|stats|run  re-ask recorded decisions; grade by meaning (no screen needed)
    pilot presets | models <provider>
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

SUBCOMMANDS = {"doctor", "observe", "eval", "presets", "models", "replay", "profile", "tune"}


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--preset", help="balanced | fast | lite | solo | sub | sub-solo | gemini | gemini-fast")
    p.add_argument("--profile", help="a saved profile: name (profiles/, ~/.config/pilot/profiles/) or path to .json")
    p.add_argument("--planner", help="provider:model, or 'none' for a single-model agent")
    p.add_argument("--executor", help="provider:model for the fast action loop")
    p.add_argument("--planner-effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--executor-effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--approve", choices=["risky", "all", "none"], help="ask before risky actions (default), every action, or never")
    p.add_argument("--gui-only", action="store_true", default=None, help="no AppleScript or shell tools")
    p.add_argument("--no-ax", dest="use_ax", action="store_false", default=None, help="screenshots only, no accessibility tree")
    p.add_argument("--jev", action="store_true", default=None, help="use Jev (TypeSafe) reflex decisions; needs TYPESAFE_API_KEY")
    p.add_argument("--plan", choices=["lazy", "upfront"], help="lazy (default): act at once, plan only when stuck; upfront: plan first")
    p.add_argument("--verify", choices=["auto", "always", "never"], help="planner's final check (auto: after escalations or long runs)")
    p.add_argument("--max-minutes", type=float)
    p.add_argument("--max-cost", type=float)
    p.add_argument("--max-turns", type=int)
    p.add_argument("--speak", action="store_true", default=None, help="say the result aloud")


def _settings(args: argparse.Namespace, **extra):
    from .config import build_settings

    keys = ["approve", "gui_only", "use_ax", "jev", "verify", "plan", "max_minutes", "max_cost", "max_turns", "speak"]
    return build_settings(
        preset=args.preset, planner=args.planner, executor=args.executor,
        planner_effort=args.planner_effort, executor_effort=args.executor_effort, profile=getattr(args, "profile", None),
        **{k: getattr(args, k, None) for k in keys}, **extra,
    )


def _boot() -> None:
    from .config import load_dotenv

    load_dotenv(Path.cwd() / ".env", Path.home() / ".config" / "pilot" / ".env", Path(__file__).resolve().parents[2] / ".env")
    if platform.system() != "Darwin":
        sys.exit("Pilot drives macOS; it needs to run on a Mac.")


def cmd_task(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pilot", description="Mac computer-use agent")
    p.add_argument("task", nargs="*", help="what to do (omit for an interactive session)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    _common(p)
    args = p.parse_args(argv)
    from .agent.runner import Agent
    from .console import Reporter
    from .env import MacEnv
    from .providers.jev import Jev

    settings = _settings(args)
    reporter = Reporter(verbose=args.verbose, quiet=args.quiet)
    env = MacEnv(settings.profile.max_long_edge, settings.profile.image_format, settings.profile.use_ax)
    _permission_hint(env, reporter)
    agent = Agent(settings, env, reporter, Jev())
    from .mac.apps import KeepAwake

    if args.task:
        with KeepAwake():
            out = agent.run(" ".join(args.task))
        return 0 if out.success else 1
    reporter.c.print("[bold]Pilot[/] interactive · type a task, or 'exit'")
    history: list[str] = []
    while True:
        try:
            task = reporter.c.input("[bold cyan]› [/]").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if task.lower() in {"exit", "quit", "q"}:
            return 0
        if not task:
            continue
        out = agent.run(task, context=" | ".join(history[-4:]))
        history.append(f"{task} → {'done' if out.success else 'not done'}{': ' + out.answer[:120] if out.answer else ''}")


def _permission_hint(env, reporter) -> None:
    if not env.ax.trusted():
        reporter.info("Accessibility permission is off: running on screenshots only (slower, less precise). Run `pilot doctor`.")
    if not env.screen.has_permission():
        reporter.info("Screen Recording permission is off: screenshots will show only the desktop. Run `pilot doctor`.")


def cmd_doctor(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pilot doctor")
    p.add_argument("--prompt", action="store_true", help="trigger the macOS permission prompts")
    args = p.parse_args(argv)
    from rich.console import Console

    from .config import PROVIDER_DEFAULTS, default_preset, has_credentials
    from .env import MacEnv

    c = Console(highlight=False)
    ok = lambda b: "[green]✓[/]" if b else "[red]✗[/]"  # noqa: E731
    c.print(f"[bold]Pilot doctor[/] · macOS {platform.mac_ver()[0]} · Python {platform.python_version()}")
    env = MacEnv()
    host = "unknown"
    if env.host_pid:
        import AppKit

        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(env.host_pid)
        host = str(app.localizedName()) if app else host
    ax_ok = env.ax.trusted(prompt=args.prompt)
    sc_ok = env.screen.has_permission() or (args.prompt and env.screen.request_permission())
    c.print(f"{ok(ax_ok)} Accessibility permission  [dim](System Settings → Privacy & Security → Accessibility → enable “{host}”)[/]")
    c.print(f"{ok(sc_ok)} Screen Recording permission  [dim](System Settings → Privacy & Security → Screen & System Audio Recording → enable “{host}”)[/]")
    if not (ax_ok and sc_ok):
        c.print("  [yellow]Grant both to the app you run Pilot from, then restart that app.[/] `pilot doctor --prompt` opens the prompts.")
    g = env.geometry
    c.print(f"  Display: {g.points_w}x{g.points_h} points · {g.pixels_w}x{g.pixels_h} pixels → model frame {g.model_w}x{g.model_h}")
    times = []
    for _ in range(3):
        t = time.monotonic()
        env.screen.capture()
        times.append((time.monotonic() - t) * 1000)
    c.print(f"  Capture: {min(times):.0f} ms ({'Quartz' if env.screen._cg_ok else 'screencapture CLI'})")
    if ax_ok:
        env.use_ax = True
        snap = env.snapshot()
        if snap:
            c.print(f"  Accessibility snapshot of {snap.app}: {len(snap.nodes)} elements in {snap.elapsed_ms:.0f} ms")
    c.print(f"  Host terminal (Pilot never types into it): {host}")
    c.print("[bold]Credentials[/]")
    for prov in PROVIDER_DEFAULTS:
        if prov in {"ollama", "lmstudio"}:
            continue
        c.print(f"  {ok(has_credentials(prov))} {prov} [dim]({' / '.join(PROVIDER_DEFAULTS[prov][1])})[/]")
    c.print(f"  {ok(bool(os.environ.get('TYPESAFE_API_KEY')))} jev [dim](TYPESAFE_API_KEY, optional)[/]")
    c.print(f"Default preset: [cyan]{default_preset()}[/]")
    return 0 if (ax_ok and sc_ok) else 1


def cmd_observe(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pilot observe")
    p.add_argument("--out", default="pilot-observe.png")
    p.add_argument("--coords", choices=["pixels", "norm1000"], default="pixels")
    args = p.parse_args(argv)
    from .env import MacEnv

    env = MacEnv()
    obs = env.observe(settle=False, coord_space=args.coords)
    Path(args.out).write_bytes(obs.image.data)
    print(obs.text())
    print(f"\n[capture {obs.capture_ms:.0f} ms · ax {obs.ax_ms:.0f} ms · screenshot saved to {args.out}]")
    return 0


def cmd_eval(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pilot eval", description="PilotBench: hard Mac tasks graded by end state")
    p.add_argument("--presets", default=None, help="comma-separated presets to compare (default: the default preset)")
    p.add_argument("--tasks", default=None, help="comma-separated task ids")
    p.add_argument("--tags", default=None, help="comma-separated axes: speed,long,intelligence,precision,redteam")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--out", default="evals")
    p.add_argument("--list", action="store_true", help="list tasks")
    p.add_argument("--validate", action="store_true", help="check every grader with its oracle (no model calls)")
    p.add_argument("--with-apps", action="store_true", help="include oracles that drive Notes via AppleScript")
    _common(p)
    args = p.parse_args(argv)
    from .config import default_preset
    from .evals.runner import run_evals, validate
    from .evals.tasks import TASKS

    task_ids = args.tasks.split(",") if args.tasks else None
    tags = args.tags.split(",") if args.tags else None
    if args.list:
        for t in TASKS:
            print(f"{t.id:15} {','.join(t.tags):26} {'[gui-only] ' if t.gui_only else ''}{t.prompt}")
        return 0
    if args.validate:
        return 0 if validate(task_ids, tags, args.with_apps) else 1
    presets = (args.presets or args.preset or default_preset()).split(",")
    overrides = {k: getattr(args, k) for k in ["approve", "use_ax", "jev", "verify", "plan", "max_minutes", "max_cost", "max_turns"] if getattr(args, k) is not None}
    if args.planner or args.executor:
        overrides.update(planner=args.planner, executor=args.executor)
    run_evals(presets, task_ids, args.repeat, overrides, Path(args.out), tags)
    return 0


def cmd_replay(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pilot replay", description="Replay recorded decisions and grade them by meaning")
    p.add_argument("action", choices=["build", "stats", "run"])
    p.add_argument("--dir", default="evals/replay")
    p.add_argument("--runs", default="runs")
    p.add_argument("--kind", default="hard,soft", help="hard, soft, or both")
    p.add_argument("--tags", default=None, help="hard-item tags, e.g. safety,false-done")
    p.add_argument("--limit", type=int, default=40, help="max items (hard first, soft sampled across tasks)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--parallel", type=int, default=2)
    p.add_argument("--executor", default=None, help="provider:model to test (default: the preset's executor)")
    p.add_argument("--preset", default=None)
    p.add_argument("--system", default=None, help="file with an alternative executor system prompt")
    args = p.parse_args(argv)
    from collections import Counter

    from rich.console import Console

    from .replay import frames as fr
    from .replay import seed

    c = Console(highlight=False)
    root = Path(args.dir)
    if args.action == "build":
        root.mkdir(parents=True, exist_ok=True)
        fr.build(Path(args.runs), Path("evals"), root)  # first pass so labels can see every frame
        n = seed.write(root, fr.load(root))
        stats = fr.build(Path(args.runs), Path("evals"), root)
        c.print(f"{stats['frames']} frames ({stats['recorded']} recorded, {stats['legacy']} legacy): "
                f"{stats.get('hard', 0)} hard, {stats.get('soft', 0)} soft, {stats.get('unlabeled', 0)} unlabelled · {n} curated labels")
        return 0
    frames = fr.load(root)
    if not frames:
        sys.exit("no frames yet: run `pilot replay build` first")
    if args.action == "stats":
        kinds = Counter(f["label"]["kind"] for f in frames)
        tags = Counter(t for f in frames if f["label"]["kind"] == "hard" for t in f["label"].get("tags", []))
        tasks = Counter((f.get("outcome") or {}).get("task_id") or "?" for f in frames)
        c.print(f"frames {len(frames)} · {dict(kinds)}\nhard tags {dict(tags)}\ntasks {dict(tasks)}")
        return 0
    from .config import build_settings, parse_spec
    from .replay.runner import replay, save, select, summarise

    spec = parse_spec(args.executor, thinking="off") if args.executor else build_settings(preset=args.preset).executor
    chosen = select(frames, args.kind.split(","), args.tags.split(",") if args.tags else None, args.limit, args.seed)
    system = Path(args.system).read_text() if args.system else None
    c.print(f"Replaying {len(chosen)} decisions on [cyan]{spec.label}[/] (one model call each, no screen needed)")

    def show(r: dict) -> None:
        if "error" in r:
            c.print(f"  [yellow]ERR[/] {r['id'][-40:]} {r['error'][:100]}")
        else:
            mark = "[dim]N/A [/]" if r["pass"] is None else ("[green]PASS[/]" if r["pass"] else "[red]FAIL[/]")
            c.print(f"  {mark} {r['kind']:4} {r['id'][-40:]} "
                    f"{r['latency']:.1f}s [dim]{r['why'][:80]}[/]")

    results = replay(chosen, spec, root, system=system, parallel=args.parallel, on_result=show)
    path = save(results, root, {"executor": spec.label, "system": args.system, "kind": args.kind, "limit": args.limit, "seed": args.seed})
    s = summarise(results)
    c.print(f"[bold]hard {s['hard']}[/] {s['hard_by_tag']} · [bold]soft agreement {s['soft_agreement']}[/] · "
            f"median {s['median_latency']}s · ~{s['mean_tokens_in']} input tokens/call · ungradable {s['ungradable']} · errors {s['errors']}")
    c.print(f"[dim]Results: {path}[/]")
    return 0


def cmd_profile(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pilot profile", description="Inspect harness profiles (the tunable genome)")
    p.add_argument("action", choices=["knobs", "show", "diff"])
    p.add_argument("refs", nargs="*", help="profile names or paths (show: one; diff: two)")
    args = p.parse_args(argv)
    from .config import default_preset, preset_profile
    from .profile import KNOBS, Profile, describe

    def resolve(ref: str):
        try:
            return Profile.load(ref)
        except Exception:
            return preset_profile(ref)

    if args.action == "knobs":
        print(describe())
        print("\nNot tunable (by design): approval mode, safety policy and guards, task scope, kill switch, "
              "spending limits, GUI-only, write roots, the Mac environment layer, graders.")
        return 0
    if args.action == "show":
        prof = resolve(args.refs[0] if args.refs else default_preset())
        print(f"profile {prof.name}" + (f" (from {prof.parent})" if prof.parent else ""))
        for k, v in prof.resolved().items():
            note = prof.why.get(k, "")
            print(f"  {k:20} = {json.dumps(v)}" + (f"\n      {note}" if note else ""))
        return 0
    if len(args.refs) != 2:
        sys.exit("usage: pilot profile diff A B")
    a, b = resolve(args.refs[0]), resolve(args.refs[1])
    d = a.diff(b)
    for k, (x, y) in d.items():
        print(f"  {k:20} {json.dumps(x)}  ->  {json.dumps(y)}")
    print(f"{len(d)} of {len(KNOBS)} knobs differ")
    return 0


def cmd_tune(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pilot tune", description="Adapt the harness to a model")
    p.add_argument("action", choices=["calibrate", "ablate", "audit", "mine", "propose", "accept"])
    p.add_argument("model", nargs="?", help="calibrate: provider:model to calibrate as the executor · accept: the candidate profile")
    p.add_argument("--profile", default="sub", help="ablate, propose: the profile to improve")
    p.add_argument("--scope", default="dev", choices=["dev", "test", "gate", "all"], help="mine: which split to read")
    p.add_argument("--rounds", default="3", help="mine, propose: newest bench rounds to read, or 'all'")
    p.add_argument("--proposer", default=None, help="propose: provider:model (default: the profile's planner)")
    p.add_argument("--repeat", type=int, default=1, help="accept: runs per task and profile")
    p.add_argument("--adopt", action="store_true", help="accept: write an accepted change into the base profile")
    p.add_argument("--budget", type=int, default=120, help="ablate: most model calls to spend")
    p.add_argument("--base", default="sub", help="profile to derive from")
    p.add_argument("--name", default=None, help="name of the new profile")
    p.add_argument("--efforts", default="low,medium")
    p.add_argument("--frames", type=int, default=12, help="replay frames per effort")
    p.add_argument("--layouts", type=int, default=8, help="synthetic probe screens (max 8)")
    p.add_argument("--sizes", default="1280,1024", help="smaller screenshot widths to try")
    p.add_argument("--parallel", type=int, default=2)
    p.add_argument("--tolerance", type=int, default=1)
    p.add_argument("--dir", default="evals/replay")
    p.add_argument("--out", default="profiles", help="where to save the new profile")
    p.add_argument("--dry-run", action="store_true", help="show the call budget and stop")
    args = p.parse_args(argv)
    from .config import parse_spec, preset_profile
    from .profile import Profile
    from .providers import get_provider
    from .replay import frames as fr
    from .tune.calibrate import CalibrationConfig, calibrate

    if args.action in {"mine", "propose"}:
        from .evals.tasks import TASKS
        from .tune.mine import mine

        rounds = None if args.rounds == "all" else int(args.rounds)
        mined = mine(Path("runs"), Path("evals"), TASKS, "dev" if args.action == "propose" else args.scope, rounds)
        calls = mined["executor_calls"] or 1
        print(f"Mined {mined['runs']} {mined['scope']} runs ({'all history' if rounds is None else f'newest {rounds} bench rounds'}): "
              f"{mined['executor_calls']} executor calls, {mined['executor_in'] // calls} input tokens per call")
        for cl in mined["clusters"][:15]:
            print(f"  {cl['turns_lost']:5} turns {cl['seconds_lost']:6} s {cl['tokens']:>8} tok  x{cl['count']:<3} {cl['signature'][:64]}"
                  f"  · {', '.join(cl['tasks'][:3])} · last {cl['last_seen']}")
        if args.action == "mine":
            return 0
        from .tune.propose import propose

        try:
            prof = Profile.load(args.profile)
        except Exception:
            prof = preset_profile(args.profile)
        print(f"\nAsking {args.proposer or prof.planner or prof.executor} for profile changes (one model call)…")
        if args.dry_run:
            return 0
        cands, rep, rpath = propose(prof, mined, Path("evals/tune"), args.proposer)
        for cand in cands:
            k, (a, b) = next(iter(prof.diff(cand).items()))
            path = cand.save(Path(args.out) / f"{cand.name}.json")
            print(f"\n  candidate {path}: {k} {json.dumps(a)} -> {json.dumps(b)}\n    {cand.why[k][:400]}")
        for note in rep["raw"].get("harness_notes") or []:
            print(f"\n  harness note [{note['cluster'][:60]}]\n    evidence: {note['evidence'][:300]}\n    suggestion: {note['suggestion'][:400]}")
        for d in rep["dropped"]:
            print(f"  dropped: {d}")
        print(f"\nReport {rpath}" + (f"  ·  test one: pilot tune accept {Path(args.out) / (cands[0].name + '.json')}" if cands else "  ·  no knob change proposed"))
        return 0
    if args.action == "accept":
        from .tune.accept import RULE, accept

        if not args.model:
            sys.exit("accept needs a candidate profile, e.g. pilot tune accept profiles/sub-max-long-edge-123456.json")
        print(f"Accepting only if: {RULE}")
        if args.dry_run:
            return 0
        v = accept(args.model, args.base, Path("evals"), args.repeat, args.adopt)
        print(f"\n{v['status'].upper()}: {v['why']}")
        for name, row in (v.get("metrics") or {}).items():
            print(f"  {name:40} pass {row['pass']} · red-team {row['gate']} · calls {row['calls']} · actions {row['actions']} · "
                  f"{row['seconds']} s · ${row['cost']}")
        if v.get("adopted"):
            print(f"Adopted into {v['adopted']}")
        return 0
    if args.action == "audit":
        from .tune.ablate import audit

        for name, row in sorted(audit(Path("runs")).items()):
            detail = ", ".join(f"{k} {v}" for k, v in row.items() if k != "verdict")
            print(f"  {name:16} {detail}  →  {row['verdict']}")
        return 0
    if args.action == "ablate":
        from .tune.ablate import AblationConfig, ablate

        frames = fr.load(Path(args.dir))
        try:
            prof = Profile.load(args.profile)
        except Exception:
            prof = preset_profile(args.profile)
        acfg = AblationConfig(frames=min(args.frames, 30), budget=args.budget, tolerance=args.tolerance, parallel=args.parallel)
        print(f"Ablating {prof.name} ({prof.executor}): up to {acfg.budget} model calls, no screen needed.")
        if args.dry_run:
            return 0
        cand, report = ablate(prof, frames, Path(args.dir), acfg, Path("evals/tune"))
        d = prof.diff(cand)
        if not d:
            print(f"\nNo change proposed  ·  report {report}")
            return 0
        path = cand.save(Path(args.out) / f"{cand.name}.json")
        print(f"\nCandidate {cand.name}: " + ", ".join(f"{k}: {json.dumps(a)} -> {json.dumps(b)}" for k, (a, b) in d.items()))
        print(f"Saved {path}  ·  report {report}")
        print(f"Confirm before adopting: pilot eval --presets {prof.name},{path}")
        return 0
    if not args.model:
        sys.exit("calibrate needs a model, e.g. pilot tune calibrate claude-code:claude-opus-5-5")
    cfg = CalibrationConfig(tuple(args.efforts.split(",")), args.frames, args.layouts, args.parallel, args.tolerance,
                            tuple(int(x) for x in args.sizes.split(",") if x))
    native = get_provider(parse_spec(args.model)).native_computer
    frames = fr.load(Path(args.dir))
    if not frames:
        sys.exit("no replay frames: run `pilot replay build` first")
    print(f"Calibrating {args.model}: about {cfg.budget(native)} model calls, no screen needed.")
    if args.dry_run:
        return 0
    try:
        base = Profile.load(args.base)
    except Exception:
        base = preset_profile(args.base)
    prof, report = calibrate(args.model, base, frames, Path(args.dir), cfg, Path("evals/tune"), args.name)
    path = prof.save(Path(args.out) / f"{prof.name}.json")
    print(f"\nProfile {prof.name}: " + ", ".join(f"{k}={json.dumps(v[1])}" for k, v in base.diff(prof).items()))
    print(f"Saved {path}  ·  report {report}")
    print(f"Use it: pilot --profile {prof.name} \"...\"   ·   compare: pilot profile diff {base.name} {path}")
    return 0


def cmd_presets(argv: list[str]) -> int:
    from .config import PRESETS, default_preset, has_credentials

    for name, p in PRESETS.items():
        pl, ex = p["planner"], p["executor"]
        ready = all(has_credentials(s.provider) for s in (pl, ex) if s)
        star = "*" if name == default_preset() else " "
        print(f"{star} {name:12} planner={pl.label if pl else 'none':32} executor={ex.label:32} {'ready' if ready else 'needs credentials'}")
    return 0


def cmd_models(argv: list[str]) -> int:
    if not argv:
        sys.exit("usage: pilot models <provider>   (gemini, openai, openrouter, ollama, ...)")
    from .config import parse_spec
    from .providers import get_provider

    spec = parse_spec(f"{argv[0]}:x")
    if spec.provider == "anthropic":
        import anthropic

        for m in anthropic.Anthropic().models.list():
            print(m.id)
        return 0
    prov = get_provider(spec)
    for m in prov.client.models.list():  # type: ignore[attr-defined]
        print(m.id)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _boot()
    if argv and argv[0] in SUBCOMMANDS:
        name = argv.pop(0)
        return {"doctor": cmd_doctor, "observe": cmd_observe, "eval": cmd_eval, "presets": cmd_presets, "models": cmd_models,
                "replay": cmd_replay, "profile": cmd_profile, "tune": cmd_tune}[name](argv)
    return cmd_task(argv)


if __name__ == "__main__":
    sys.exit(main())
