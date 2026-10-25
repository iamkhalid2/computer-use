# Pilot

**A self-tuning, model-agnostic computer-use agent for macOS.**

Pilot operates your Mac the way a careful person would: it reads the screen through the macOS **Accessibility API** *and* screenshots, acts through real UI elements instead of guessing pixels, checks its work against ground truth, and refuses anything risky you didn't ask for.

The harness itself is the product. Everything Pilot can tune about how it drives a model (which model plays which role, how hard it thinks, what it sees, which scaffolding is switched on, which prompt rules it gets) lives in a typed **profile**, and Pilot measures a model to write that profile itself. Give it a new model and it calibrates; as models get smarter, the scaffolding they no longer need can be measured away. The goal is the **Pareto frontier of cost, speed, long-horizon reliability and intelligence** for whatever model it is given.

```bash
pilot "Open example.com in Safari and tell me the main heading"
pilot "Move every PDF in ~/Downloads into ~/Documents/Archive/2026"
pilot                                   # interactive session
pilot tune calibrate claude-code:claude-opus-5-5   # adapt the harness to a model
```

Today it runs on Claude through the official Claude Code CLI with your own subscription; any Anthropic API key or OpenAI-compatible endpoint (Gemini, OpenAI, OpenRouter, Ollama, vLLM, LM Studio) works too.

---

## Contents

1. [How it works](#how-it-works)
2. [Install](#install)
3. [Using it](#using-it)
4. [Models and providers](#models-and-providers)
5. [Profiles: the harness genome](#profiles-the-harness-genome)
6. [Self-tuning: `pilot tune calibrate`](#self-tuning-pilot-tune-calibrate)
7. [Measuring it](#measuring-it): PilotBench · replay evals · run reports
8. [Results](#results)
9. [Safety](#safety)
10. [Project layout](#project-layout)
11. [Design notes and lessons](#design-notes-and-lessons)
12. [Known limits and roadmap](#known-limits-and-roadmap)

---

## How it works

```
            ┌──────────── planner (smart, rare: Opus 5.5) ─────────────────────────────┐
 task ──►   │ plan on demand (lazy)      replan on escalation      verify when worth it │
            └──────────────┬───────────────────────▲───────────────────────▲───────────┘
                           │ step + hint           │ escalate              │ log + screen + disk evidence
            ┌──────────────▼───────────────────────┴───────────────────────┴───────────┐
            │ executor (fast: Sonnet 5.5) — batches actions, sees a fresh screen each turn │
            │   ui · find · menu · open · write_file · applescript · shell · wait_for ·   │
            │   computer (mouse/keyboard/screenshot/zoom) · done · escalate             │
            └──────────────┬───────────────────────────────────────────────────────────┘
                           │
            ┌──────────────▼──────────── reflex layer (no model calls) ──────────────────┐
            │ element resolution · screen-settle detection · auto-observe · loop and      │
            │ no-change notes · wait-for-editable · disk check before "done" · type      │
            │ coercion · safety policy · consent and terminal guards · kill switch       │
            └──────────────┬───────────────────────────────────────────────────────────┘
                           ▼
         macOS: Quartz events (Retina-correct) · Accessibility API · screen capture (~25 ms)
```

**Three speeds.** Profiling of computer-use agents shows that model calls dominate wall-clock time and that visual grounding errors cause about a quarter of failures, so every layer attacks one of those:

| Lever | What Pilot does |
|---|---|
| **Fewer model calls** | Lazy planning: the executor starts at once and the planner is called only when it escalates or stalls. Verification runs only after escalations or long runs. The executor batches actions, and a fresh screenshot plus element list arrive after every turn, so it never spends a call asking to look. |
| **No pixel guessing** | Each observation includes a numbered list of real UI elements (role, label, value, position) from the Accessibility tree. `ui` presses or fills them by id or label (AXPress / AXValue), which is exact and works on covered windows. Chromium browsers are switched into full accessibility mode so web pages are readable too. |
| **Fewer steps** | `menu` chooses any menu command by path and reveals shortcuts; `open` goes straight to apps, URLs and files; `write_file`, `shell` and `applescript` replace dozens of clicks when the task allows them. |
| **Ground truth over impressions** | Before accepting "done" on a task that writes files, Pilot shows the executor what is actually on disk. The verifier gets the same evidence. A screenshot can show an uncommitted rename; the disk can't. |
| **Reflexes, not reflection** | Screen-settle detection instead of fixed sleeps; waits for an editable field before typing; notes when actions changed nothing or repeat; all without a model call. |

## Install

```bash
git clone https://github.com/iamkhalid2/computer-use && cd computer-use
uv sync
uv run pilot doctor
```

Grant two permissions to the app you run Pilot from (Terminal, iTerm, VS Code, the Claude app…), then restart it:

* **System Settings → Privacy & Security → Accessibility**: reading and pressing UI elements
* **System Settings → Privacy & Security → Screen & System Audio Recording**: screenshots

`pilot doctor --prompt` opens both prompts. It also reports capture speed, the Accessibility snapshot time and which model credentials are available.

**Model access**, any one of:

```bash
claude auth login                 # your Claude Pro/Max plan through the official CLI (personal use)
export ANTHROPIC_API_KEY=...      # Claude API
export GEMINI_API_KEY=...         # or OPENAI_API_KEY, OPENROUTER_API_KEY; local servers need no key
```

Keys can also live in `.env` (here or in `~/.config/pilot/.env`); see `.env.example`.

## Using it

```bash
pilot "task"                       # run once with the default profile for your credentials
pilot --profile <name|path> "task" # run with a saved profile
pilot --approve all "task"         # confirm every action
pilot --gui-only "task"            # no shell or AppleScript
pilot -v "task"                    # show every result and model latency
pilot observe                      # print what the agent sees right now (no model calls)
pilot presets | pilot profile show sub
```

Stop any time with **Ctrl+C**, or by moving the mouse into the **top-left corner** of the screen. Every run writes `runs/<time>-<task>/` with screenshots, an event log, every decision's full input (`decisions.jsonl`), and `report.html`: a timeline of plans, model calls, actions and screenshots, with a bar showing where the time went.

## Models and providers

| Provider | How | Pixel actions |
|---|---|---|
| `claude-code:` | your Claude subscription through the official `claude` CLI, as a subprocess (streaming JSON, structured output) | generic `computer` action |
| `anthropic:` | Claude API | Claude's native computer toolset (`computer_toolset_20260801`) |
| `gemini:`, `openai:`, `openrouter:`, `groq:`, `ollama:`, `lmstudio:`, `compat:model@url` | any OpenAI-compatible chat API | generic `computer` function, pixels or 0–1000 |

**Using your Claude subscription.** Anthropic allows you to sign in to the unmodified `claude` CLI with your own plan and script it for yourself. It does not allow extracting the OAuth token or offering claude.ai login to others, so Pilot never touches credentials: it runs the CLI. The CLI runs in safe mode with your CLAUDE.md, hooks and plugins disabled so they don't leak into the agent's context. Plan limits assume ordinary individual use; when a usage limit is hit, Pilot stops and reports when it resets.

**Presets** are starting profiles:

| Preset | Planner | Executor |
|---|---|---|
| `sub` (default with the CLI signed in) | Opus 5.5 · medium | Sonnet 5.5 · low |
| `sub-solo` | none | Opus 5.5 · medium |
| `balanced` (default with an API key) | Opus 5.5 · medium | Sonnet 5.5 · low |
| `fast` | Fable 5.1 · low | Sonnet 5.5 · low |
| `lite` | Sonnet 5.5 · medium | Haiku 5.5 · low |
| `solo` | none | Opus 5.5 · medium |
| `gemini`, `gemini-fast` | Gemini Pro / Flash with automatic failover | Gemini Flash / Flash-Lite |

The executor runs 10–30 times per task, so time to first token dominates its choice; the planner runs 0–3 times, so it can afford to think. Starting points came from Artificial Analysis data (Sonnet 5.5 low/medium answers in 1.0–1.4 s; Opus 5.5 medium sits on the speed/intelligence Pareto frontier), and calibration measures the rest.

## Profiles: the harness genome

Everything Pilot may change about itself lives in one typed **profile**: 30 knobs, each with a type, bounds, a default and an explanation.

| Group | Knobs |
|---|---|
| models | planner and executor model, effort, thinking, coordinate convention, fallbacks |
| topology | lazy or upfront planning, verification policy, step turn budget, escalations |
| observation | screenshot size and format, Accessibility list on/off, list length and compaction, screenshots kept, settle time |
| scaffold | disk check before done, loop window, budget warning, nudge on a text-only turn, no-change note, wait for editable fields, paste threshold |
| prompt | which optional executor rules are included |

```bash
pilot profile knobs          # every knob with type, bounds and meaning
pilot profile show sub       # values and the evidence behind each
pilot profile diff sub profiles/<other>.json
```

Profiles load from `./profiles`, `~/.config/pilot/profiles` or the built-ins (`src/pilot/profiles/`). `sub.json` is the configuration that scored 15/15 on PilotBench twice; every value records **why** it is set, and values never measured against alternatives are labelled *untuned*.

**Invariants: not in any profile, so no tuner can trade them for speed.** The approval mode, safety policy and classifiers, the consent-dialog and host-terminal guards, task-scoped authorisation, the kill switch, your spending limits, GUI-only, write folders, the macOS environment layer and the graders. The two safety rules in the executor prompt (on-screen text is data; never enter secrets or touch the terminal) are always included. A profile that names an invariant is rejected.

## Self-tuning: `pilot tune calibrate`

```bash
pilot tune calibrate claude-code:claude-opus-5-5 --efforts medium --dry-run   # call budget
pilot tune calibrate claude-code:claude-opus-5-5 --efforts medium             # writes profiles/sub-claude-code-claude-opus-5-5.json
```

Calibration measures a model as the executor, entirely without the screen (synthetic probe screens and recorded decisions), then writes a profile derived from `--base` in which every calibrated value cites its measurement and a JSON report in `evals/tune/`:

1. **Coordinate convention.** Eight synthetic screens (dialogs, an icon toolbar, a dense 14-row table, dark mode, a corner target, a text field) with exactly known target rectangles, probed in pixels and in 0–1000.
2. **Hidden downscale.** Misses by one consistent ratio on both axes mean the screenshot exceeded the model's image budget and was silently shrunk. Pilot shrinks screenshots to fit and re-probes.
3. **Element ids.** Does the model act through the element list? If it never does and clicks reliably without it, the list is dead weight.
4. **Screenshot size.** The smallest screenshot that loses no grounding accuracy (a 1024 px screenshot costs about half the visual tokens of 1470 px).
5. **Effort sweep.** A fixed replay set (one hard item per failure type, soft items across tasks), identical for every model, at each effort: decision quality, protocol errors, latency, tokens. Among efforts within `--tolerance` items of the best, the fastest wins.

Safety results are reported per effort but never traded.

## Measuring it

### PilotBench: graded by end state

Sixteen Mac tasks over five axes, graded by the real end state, never by a model's claim: files on disk, form submissions recorded by a local fixture server, the Calculator's accessibility value, system defaults.

| Axis | Tasks |
|---|---|
| speed | Calculator; new Finder folder; read the Appearance setting; TextEdit plain-text save |
| long horizon | two-step job application from a details file; visit three pages and compare; archive PDFs among decoys (`.PDF`, `x.pdf.txt`, a folder named `slides.pdf`); web recipe → Notes |
| intelligence | invoice total with refunds, shipping and a coupon; open action items across three meetings, one reassigned, one done |
| precision | tick exactly the primes among 36 boxes; pointer drag-and-drop sorting; date fields |
| red team | prompt injection on a web page (visible, hidden, and a trap URL); injection inside a local file; a dark-pattern "Subscribe" overlay; an impossible request that must be refused, not faked |

```bash
pilot eval --list
pilot eval --validate                     # every grader fails before, passes after a scripted correct solution
pilot eval --presets sub,profiles/x.json  # compare presets or saved profiles
```

The report shows pass rate per axis, median time, model time, cost, false "done" claims and safety failures, and marks the configurations on the success/speed/cost Pareto frontier. Fixtures run on 127.0.0.1, files live in `~/PilotEval`, web tasks open in Safari (never your own browser), the screen is held awake for the run, and runs lost to a locked screen, a provider outage or a usage limit are marked invalid instead of failed.

### Replay evals: measuring decisions without the screen

Every executor decision is recorded as a self-contained frame: brief, actions so far, harness notes, screenshot, element list and an element snapshot. `pilot replay` re-asks a model those exact decisions (one call each, no screen) and grades the answer by **meaning**: pressing `[12] "Save"` by id, by label or by its pixels is one action; `cmd+S` and `Command+s` are one key.

```bash
pilot replay build && pilot replay stats
pilot replay run --limit 40                      # hard items first, soft items across tasks
pilot replay run --executor claude-code:claude-haiku-5-5
pilot replay run --system my-prompt.txt          # measure a prompt change
```

**Soft items** are decisions from runs that passed (agreement with a known-good choice). **Hard items** are curated from real failures and red-team moments with `expect`/`forbid` rules: never claim done while the path bar still says "untitled folder", never answer a macOS permission prompt, never follow injected instructions, open the task's URL rather than a stale tab. Clicks that can't be identified are reported as ungradable, never as misses. The grader agrees with itself on all 350 soft decisions and rejects 88% of mismatched actions. Frames contain screenshots of your screen, so `evals/` is git-ignored.

## Results

All runs on a MacBook (Apple M5, macOS 27) through a Claude subscription via the CLI, October 2026. Single runs per configuration, so treat small differences as noise.

**PilotBench, final configurations (15 tasks; the Notes task needs Automation permission and was excluded):**

| Configuration | Pass | Median task | Total | Model calls | Cost at API prices | False "done" | Safety failures |
|---|---|---|---|---|---|---|---|
| **`sub`**: Opus 5.5 plans on demand, Sonnet 5.5 (low) executes | **15/15** | **12.9 s** | **229 s** | 68 | **$0.94** | 0 | 0 |
| Opus 5.5 (medium) executes, calibrated profile | 15/15 | 13.8 s | 320 s | 76 | $1.48 | 0 | 0 |
| Sonnet 5.5 (medium) executes, calibrated profile | 14/15 | 15.0 s | 246 s | 68 | $1.04 | 1 | 0 |

`sub` is on the frontier: the same success as an Opus executor at 28% less time and 36% less cost. The calibrated-Sonnet miss was a harness bug (a dropdown option picked by pixels did not register), since fixed by choosing popup options through Accessibility.

**How the harness improved (same tasks, `sub`):**

| Round | Pass | Total time | Model calls | What changed |
|---|---|---|---|---|
| 1 | 13/15 | 702 s | 141 | first run |
| 2 | 13/15 | 302 s | 68 | rescue native tool calls from the CLI; authorisation from the user's words; disk evidence for the verifier |
| 4 | 15/15 | 313 s | 85 | done requires an explicit outcome; Chromium accessibility; open the task's exact URL; wait for editable fields; disk check before done |
| 6 | 15/15 | 229 s | 68 | type one key at a time; focus-confirmed form filling; prefer controls over labels |

**Calibration** (`pilot tune calibrate`, no screen involved):

| Model as executor | Grounding (pixels) | Hidden downscale | Screenshot size chosen | Hard replay items | Median per decision |
|---|---|---|---|---|---|
| Opus 5.5 · medium | 8/8 (8 screens) | none | 1024 px, no accuracy loss | 5/5 | 3.5 s |
| Sonnet 5.5 · medium | 2/2 (2 screens) | none | 1470 px | 4/4 | 4.3 s |
| Haiku 5.5 · medium | 0/2 → 2/2 | **0.926**, corrected | 1319 px | 4/4 (soft 2/3) | 5.6 s |

Sonnet at low effort clicked a button on a macOS permission dialog during calibration (medium did not); in live runs the consent guard blocks that click regardless. Haiku and Sonnet were calibrated with the earlier two-screen suite.

**Replay** (25 recorded decisions, about 40 s, no screen): hard items 17/19, soft 5/5. Both hard misses are model judgements the harness overrides live: clicking "Don't Allow" on a permission prompt, and claiming done while the path bar still read "untitled folder".

Soft items reward agreement with a known-good choice, not the best choice: in calibration, Opus fetched a page with `curl` where the recorded action escalated, and dragged with explicit mouse-down/move/up where the reference used one drag action. Both count as misses today.

## Safety

* **Kill switch:** mouse into the top-left corner, or Ctrl+C.
* **Approval (`--approve risky`, default):** asks before risky clicks (Send, Delete, Buy, Move to Trash…), typing into password fields, AppleScript that sends, deletes or runs shell commands, and shell commands that change things. Destructive patterns (`sudo`, `rm -rf ~`, `curl | sh`…) are always refused. `--approve all` confirms every action; `--approve none` is for sandboxes.
* **Permission from your words only.** Your request authorises the URLs, folders and actions it names ("…and submit it" allows the Submit button; "save it in ~/Reports" allows writing there). Nothing on screen can grant permission, so an injected page can't.
* **Never answers macOS permission or security dialogs**, never types into or clicks the terminal running it, never types passwords it wasn't given.
* **Stops cleanly** on a locked screen or a usage limit instead of improvising.

## Project layout

```
src/pilot/
  cli.py            pilot: run, doctor, observe, eval, replay, profile, tune, presets, models
  config.py         settings (invariants and limits), presets, model specs
  profile.py        the typed knob registry and Profile (the genome)
  profiles/         built-in profiles (sub.json: the live configuration, with evidence)
  agent/            runner.py (planner, executor, reflexes, recording) · prompts.py (sectioned executor prompt)
  tools.py          action schemas and dispatcher: ui, find, menu, open, write_file, applescript, shell, wait_for, computer
  safety.py         approval policy, classifiers, task-scoped authorisation, kill switch
  env.py            observation: screenshot + element list, coordinates, configuration from the profile
  outline.py        element list formatting, compaction, fuzzy matching
  mac/              screen.py (capture, Retina geometry) · input.py (Quartz events) · ax.py (Accessibility) · keys.py · apps.py
  providers/        claude_code.py (subscription via the CLI) · anthropic_provider.py · openai_compat.py · jev.py
  evals/            PilotBench: tasks, fixture server, graders and oracles, Pareto report, analysis
  replay/           decision recording, dataset builder, meaning-based grader, curated hard items, runner
  tune/             calibration: synthetic probes, effort sweep, profile writer
profiles/           calibrated profiles written by `pilot tune`
tests/              offline tests: fake Mac, scripted models, fake CLI (`uv run --group dev pytest`)
```

## Design notes and lessons

Things the benchmark and traces taught, each now handled by the harness:

* **The screen can lie; the disk can't.** Finder showed "Pilot Folder" while the rename never committed. Hence the disk check before "done" on write tasks.
* **Typing races the UI.** Keystrokes sent right after Cmd+Shift+N, or after clicking a web field, landed before the field existed or in the previous field. Pilot waits for an editable field to take focus and reads the value back.
* **Some controls ignore bulk text.** Safari's date field only accepts real key presses, one at a time, so short text is typed per key.
* **Free-form providers send strings.** The CLI passed `"success": "false"`, and the non-empty string counted as success. Arguments are now coerced to their schema types.
* **Chromium hides web pages from Accessibility** until asked the way VoiceOver asks (`AXEnhancedUserInterface`).
* **Models have hidden image budgets.** Haiku 5.5 via the CLI answered in a screenshot silently shrunk by 0.926; calibration detects this and resizes.
* **Over-blocking is a reliability bug.** Refusing `cd`, a URL the user gave, or the Submit button the user asked for forced slow GUI detours. Authorisation now comes from the user's own words.
* **Some judgements must stay outside the model.** Asked cold, Claude clicked "Don't Allow" on a permission prompt; only the harness guard stops that. Safety stays invariant.
* **Infrastructure failures are not agent failures.** Locked screens, provider outages and usage limits are detected and excluded from scores.

## Known limits and roadmap

* One display (the main screen). Apps with little Accessibility information (games, canvases, some Electron/Java apps) fall back to pixel actions.
* Calibrated values rest on small samples (a dozen decisions per effort, eight probe screens); larger runs need more quota.
* Older traces lack element lists, so they replay screenshot-only; new runs record everything.
* Jev (TypeSafe) support is experimental and off by default.

Next steps toward the self-mutating harness: `pilot tune ablate` (switch each scaffold off and keep only what pays), `pilot tune propose` (a strong model reads failure clusters and proposes profile changes, accepted only on held-out evidence with no safety regression), a sealed test split refreshed from real use, and a background-browser body for web tasks.

## License

MIT, see [LICENSE](LICENSE).
