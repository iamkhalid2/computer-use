"""Games: can an agent do things that depend on timing and speed, not just on getting the right answer?

An agent that thinks for two or three seconds between looks cannot play these the way a person does. To do
well it has to work with its own latency: batch a press, a measured wait and another press into one turn,
repeat a key instead of pressing it twelve times, wait on a condition instead of polling with screenshots.
That is the skill measured here.

    timing     press Stop when a running counter reads a target (three rounds, three targets)
    dial       turn a dial to three numbers in order against the clock, with no wrong entry
    reaction   press as soon as a box turns green (three rounds), never before

These are original pages, generic to the skill. They are not copies of any vendor's assessment and are not
meant for preparing an agent to take one in somebody's place. Each page posts what happened when the game
ends, including whether each press was a real input event (`isTrusted`) or was made by script, and the
grader reports that beside the score. Targets are drawn fresh for every run, so nothing can be memorised.

    pilot eval --suite games --validate      check the graders with scripted results (no model, no screen)
    pilot eval --suite games                 play them
"""

from __future__ import annotations

import random
import statistics

from .fixtures import page
from .tasks import Ctx, EvalTask, close_safari_tabs, post

STATE: dict[str, list] = {}  # this run's targets, set by setup and read by the grader and the scripted solution

TIMING_MEAN, TIMING_WORST = 0.25, 0.40  # seconds off target: mean and worst round allowed
DIAL_SECONDS = 60.0  # from the first move to the third entry
REACTION_MEAN_MS = 1000.0  # a person manages about 250-300 ms

REPORT = """
function report(form, data) {
  fetch('/submit/' + form, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)})
    .then(() => { document.getElementById('end').textContent = 'Finished. Your result has been recorded.'; });
}"""
BIG = "font:64px ui-monospace,Menlo,monospace;margin:12px 0"
BTN = "button{font-size:20px;padding:10px 26px;margin-right:10px;border-radius:10px;border:1px solid #888;background:#f4f4f4}"


# -- timing: stop the clock on a target ----------------------------------------------------------
def setup_timing(ctx: Ctx) -> None:
    targets = random.sample([2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0], 3)
    STATE["timing"] = targets
    ctx.server.pages["/timing.html"] = page("Stop the clock", f"""<h1>Stop the clock</h1>
<p>Three rounds. In each round press <b>Start</b>, then press <b>Stop</b> when the counter reads the round's target, as exactly as you can.</p>
<p>Round <b id="round">1</b> of 3 · target <b id="target">{targets[0]:.2f}</b> seconds</p>
<p id="clock" style="{BIG}">0.00</p>
<p><button id="start">Start</button><button id="stop" disabled>Stop</button></p>
<ol id="log"></ol><p id="end" style="font-weight:600"></p>""", script=REPORT + f"""
const targets = {targets}; let round = 0, t0 = 0, running = false; const stops = [], trusted = [];
const $ = (id) => document.getElementById(id);
function tick() {{ if (!running) return; $('clock').textContent = ((performance.now() - t0) / 1000).toFixed(2); requestAnimationFrame(tick); }}
$('start').onclick = () => {{ if (running || round >= 3) return; t0 = performance.now(); running = true; $('start').disabled = true; $('stop').disabled = false; tick(); }};
$('stop').onclick = (e) => {{
  if (!running) return;
  const v = (performance.now() - t0) / 1000; running = false; $('clock').textContent = v.toFixed(2);
  stops.push(v.toFixed(2)); trusted.push(e.isTrusted ? '1' : '0');
  const li = document.createElement('li'); li.textContent = 'target ' + targets[round].toFixed(2) + ' · stopped at ' + v.toFixed(2); $('log').append(li);
  round += 1; $('stop').disabled = true;
  if (round < 3) {{ $('round').textContent = round + 1; $('target').textContent = targets[round].toFixed(2); $('start').disabled = false; }}
  else report('timing', {{stops: stops.join(','), trusted: trusted.join(',')}});
}};""", style=BTN)


def check_timing(ctx: Ctx) -> tuple[bool, str]:
    s = ctx.server.last("timing")
    if s is None:
        return False, "the game was not finished"
    try:
        stops = [float(x) for x in s.one("stops").split(",")]
    except ValueError:
        return False, "unreadable result"
    targets = STATE.get("timing") or []
    if len(stops) != 3 or len(targets) != 3:
        return False, f"{len(stops)} of 3 rounds played"
    errors = [abs(a - b) for a, b in zip(stops, targets, strict=True)]
    mean, worst = statistics.mean(errors), max(errors)
    ok = mean <= TIMING_MEAN and worst <= TIMING_WORST
    return ok, f"mean {mean:.2f}s off target, worst {worst:.2f}s (allowed {TIMING_MEAN}/{TIMING_WORST}); {_real(s.one('trusted'))}"


# -- dial: a combination against the clock --------------------------------------------------------
def setup_dial(ctx: Ctx) -> None:
    combo = random.sample(range(3, 38), 3)
    STATE["dial"] = combo
    ctx.server.pages["/dial.html"] = page("Open the safe", f"""<h1>Open the safe</h1>
<p>The combination is <b>{combo[0]} · {combo[1]} · {combo[2]}</b>. Turn the dial to each number in order and press <b>Enter number</b> at each one.
The dial goes from 0 to 39 and wraps round. The arrow keys turn it too. The clock starts at your first move: be quick, and a wrong entry counts against you.</p>
<p>Dial: <span id="dial" style="{BIG}">0</span></p>
<p><button id="left">◀ Left</button><button id="right">Right ▶</button><button id="enter">Enter number</button></p>
<p>Entered: <b id="entered">nothing yet</b> · time <span id="time">0.0</span> s</p><p id="end" style="font-weight:600"></p>""", script=REPORT + f"""
const combo = {combo}; let value = 0, t0 = 0, moves = 0, errors = 0, done = false; const entered = [], trusted = [];
const $ = (id) => document.getElementById(id);
function clock() {{ if (t0 && !done) {{ $('time').textContent = ((performance.now() - t0) / 1000).toFixed(1); requestAnimationFrame(clock); }} }}
function turn(d, e) {{ if (done) return; if (!t0) {{ t0 = performance.now(); clock(); }} value = (value + d + 40) % 40; moves += 1; trusted.push(e.isTrusted ? '1' : '0'); $('dial').textContent = value; }}
$('left').onclick = (e) => turn(-1, e); $('right').onclick = (e) => turn(1, e);
document.addEventListener('keydown', (e) => {{ if (e.key === 'ArrowLeft') {{ turn(-1, e); e.preventDefault(); }} if (e.key === 'ArrowRight') {{ turn(1, e); e.preventDefault(); }} }});
$('enter').onclick = (e) => {{
  if (done) return; if (!t0) {{ t0 = performance.now(); clock(); }}
  if (value !== combo[entered.length]) errors += 1;
  entered.push(value); trusted.push(e.isTrusted ? '1' : '0'); $('entered').textContent = entered.join(' · ');
  if (entered.length === 3) {{ done = true; const secs = ((performance.now() - t0) / 1000).toFixed(1); $('time').textContent = secs;
    report('dial', {{entered: entered.join(','), errors: String(errors), seconds: secs, moves: String(moves), trusted: trusted.join(',')}}); }}
}};""", style=BTN)


def check_dial(ctx: Ctx) -> tuple[bool, str]:
    s = ctx.server.last("dial")
    if s is None:
        return False, "the safe was not opened: fewer than three numbers entered"
    combo = STATE.get("dial") or []
    try:
        entered, seconds = [int(x) for x in s.one("entered").split(",")], float(s.one("seconds"))
    except ValueError:
        return False, "unreadable result"
    ok = entered == combo and seconds <= DIAL_SECONDS
    what = "opened" if entered == combo else f"wrong combination {entered}, wanted {combo}"
    return ok, f"{what} in {seconds:.1f}s (allowed {DIAL_SECONDS:g}), {s.one('moves') or '?'} moves; {_real(s.one('trusted'))}"


# -- reaction: press when it turns green, never before ---------------------------------------------
def setup_reaction(ctx: Ctx) -> None:
    STATE["reaction"] = [3]
    ctx.server.pages["/reaction.html"] = page("Reaction", f"""<h1>Reaction</h1>
<p>Three rounds. Press <b>Ready</b>. After a pause of a few seconds the box turns green and says GO: press <b>Now!</b> as fast as you can.
Pressing Now! before the box says GO is a foul.</p>
<p>Round <b id="round">1</b> of 3</p>
<p id="box" style="{BIG};background:#c0392b;color:#fff;padding:18px 28px;display:inline-block;border-radius:12px">WAIT</p>
<p><button id="ready">Ready</button><button id="now">Now!</button></p>
<ol id="log"></ol><p id="end" style="font-weight:600"></p>""", script=REPORT + """
let round = 0, armed = false, go = 0, timer = null; const times = [], trusted = [];
const $ = (id) => document.getElementById(id);
function show(text, colour) { $('box').textContent = text; $('box').style.background = colour; }
function finish(result, e) {
  times.push(result); trusted.push(e.isTrusted ? '1' : '0'); armed = false; go = 0; clearTimeout(timer);
  const li = document.createElement('li'); li.textContent = result === 'foul' ? 'foul: pressed before GO' : result + ' ms'; $('log').append(li);
  round += 1; show('WAIT', '#c0392b');
  if (round < 3) { $('round').textContent = round + 1; $('ready').disabled = false; }
  else report('reaction', {times: times.join(','), trusted: trusted.join(',')});
}
$('ready').onclick = () => {
  if (armed || round >= 3) return; armed = true; $('ready').disabled = true; show('WAIT', '#c0392b');
  timer = setTimeout(() => { go = performance.now(); show('GO', '#1e8e3e'); }, 1500 + Math.random() * 2500);
};
$('now').onclick = (e) => { if (!armed) return; finish(go ? String(Math.round(performance.now() - go)) : 'foul', e); };""", style=BTN)


def check_reaction(ctx: Ctx) -> tuple[bool, str]:
    s = ctx.server.last("reaction")
    if s is None:
        return False, "the game was not finished"
    raw = [x for x in s.one("times").split(",") if x]
    fouls = raw.count("foul")
    try:
        times = [float(x) for x in raw if x != "foul"]
    except ValueError:
        return False, "unreadable result"
    if len(raw) != 3:
        return False, f"{len(raw)} of 3 rounds played"
    mean = statistics.mean(times) if times else float("inf")
    ok = fouls == 0 and mean <= REACTION_MEAN_MS
    return ok, (f"mean reaction {mean:.0f} ms" if times else "no clean round") + f", {fouls} foul(s) (allowed {REACTION_MEAN_MS:g} ms, 0 fouls); {_real(s.one('trusted'))}"


def _real(flags: str) -> str:
    """How many presses were real input events, as opposed to made by script on the page."""
    bits = [b for b in flags.split(",") if b]
    return f"{bits.count('1')} of {len(bits)} presses were real input" if bits else "input kind not recorded"


def _solve_timing(ctx: Ctx) -> None:
    post(ctx, "timing", {"stops": ",".join(f"{t + 0.05:.2f}" for t in STATE["timing"]), "trusted": "1,1,1"})


def _solve_dial(ctx: Ctx) -> None:
    post(ctx, "dial", {"entered": ",".join(str(n) for n in STATE["dial"]), "errors": "0", "seconds": "21.4", "moves": "58", "trusted": "1,1,1"})


GAMES: list[EvalTask] = [
    EvalTask("timing", "Play the timing game at {url:/timing.html}: in each of its three rounds press Start, then press Stop when the counter "
             "reads that round's target, as exactly as you can. You are scored on how close each stop is.", check_timing,
             setup=setup_timing, cleanup=lambda c: close_safari_tabs(), oracle=_solve_timing, tags=["realtime", "precision"]),
    EvalTask("dial", "Open the safe at {url:/dial.html}: turn the dial to each of the three numbers of the combination in order, pressing "
             "Enter number at each one. It is timed, so be quick, and a wrong entry counts against you.", check_dial,
             setup=setup_dial, cleanup=lambda c: close_safari_tabs(), oracle=_solve_dial, tags=["realtime", "speed"]),
    EvalTask("reaction", "Play the reaction game at {url:/reaction.html}: in each of its three rounds press Ready, then press Now! as soon as "
             "the box turns green and says GO. Never press Now! before it says GO.", check_reaction,
             setup=setup_reaction, cleanup=lambda c: close_safari_tabs(),
             oracle=lambda c: post(c, "reaction", {"times": "310,280,295", "trusted": "1,1,1"}), tags=["realtime", "speed"]),
]
