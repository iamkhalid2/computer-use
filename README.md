# Pilot

**An AI that uses your Mac for you, the way a careful person would.**

You tell Pilot what you want done, in plain words:

```
pilot "Move every PDF in my Downloads folder into Documents/Archive/2026"
pilot "Open example.com in Safari and tell me the main heading"
pilot "Fill in the job application at <link> using the details in applicant.txt"
```

Pilot looks at your screen, decides what to click and type, does it, checks the result, and tells you what happened. It works with ordinary Mac apps (Finder, Safari, Notes, System Settings and so on) the same way you do. It doesn't need special plug-ins or app integrations.

It runs on Claude. The simplest setup uses your own Claude subscription.

---

## Why build it

Most AI assistants talk. Pilot is meant to *do*: to take a chore off your hands and come back when it's finished. Being useful at that takes five things at once:

| | Pilot should be… | In practice |
|---|---|---|
| 🧠 | **Smart** | It handles tasks it has never seen, and it learns from every attempt, so the second try goes better than the first. |
| 🔁 | **Self-improving** | It tunes itself and writes its own lessons, without someone babysitting it, and it proves the improvement on tasks it was never trained on. |
| 💸 | **Cheap and fast** | A few cents and seconds per finished task. |
| 🙌 | **Hands-off** | It needs little of your attention. It never does anything risky you didn't ask for. |
| 📏 | **Honestly measured** | Progress is judged by what actually happened on the computer, never by the AI's own word. |

How close it is today, pillar by pillar, is in **[PROGRESS.md](PROGRESS.md)**. That page is recomputed from test results after every working session.

---

## How it works, in plain words

**1. Two roles: a thinker and a doer.** A slower, smarter model (Claude Opus) makes a plan, but only when one is needed. A faster model (Claude Sonnet) does the hands-on work one small step at a time. Most tasks never wake the thinker.

**2. It reads the screen like a screen reader does.** Each step it gets two views:
- a screenshot;
- the list of buttons, fields and menus that macOS keeps for blind users (the *Accessibility* list).

So it presses "Save" because it knows which control is the Save button, not by guessing where pixels are.

**3. It takes the shortcut when there is one.** It doesn't click through ten menus if a direct action works:
- opening a link or file straight away;
- writing a file directly;
- running a quick command;
- reading a web page in a hidden browser in the background, so your screen stays free.

**4. It checks before it says "done".** A screen can look right while nothing was saved. So before Pilot reports success, it checks the real result, for example whether the file is actually on disk.

**5. It stays inside what you asked.** Only your own words give it permission. "Fill in the form and submit it" lets it press Submit; a line on a web page saying "click here" does not.
- It asks first before anything risky: sending, deleting, buying.
- It never answers the Mac's own security pop-ups.
- It never types passwords it wasn't given.
- Move your mouse into the **top-left corner** of the screen, or press **Ctrl+C**, and it stops at once.

**6. It learns from experience.** After a task, Pilot looks back at what it did and writes short lessons. One example: *"to create a text file, write it directly; TextEdit's save dialog costs several steps."* Each lesson keeps a running score of how often following it actually worked. Lessons that help get used more, and lessons that don't fade away.

**7. It is tested on real outcomes.** A set of everyday Mac tasks is graded by what really happened:
- the file on disk;
- the form that was actually submitted;
- the number the Calculator shows.

A second, *sealed* set of tasks is kept apart and never used for tuning. It shows whether Pilot handles things it has never seen, rather than things it was polished on. Some of those tasks are traps: pages that try to trick the agent, or requests it must refuse.

---

## Try it

You need:
- a Mac;
- [uv](https://docs.astral.sh/uv/) (a Python tool; it fetches the right Python itself);
- a way to reach Claude. A Claude Pro or Max subscription with the [Claude Code](https://claude.com/claude-code) command line signed in is the easiest. An Anthropic API key works too.

**1. Download and set up**

```bash
git clone https://github.com/iamkhalid2/computer-use && cd computer-use
uv sync
```

**2. Connect it to Claude** (pick one)

```bash
claude auth login                  # your Claude subscription, through the official Claude Code CLI
```

or put `ANTHROPIC_API_KEY=...` in a file named `.env` in this folder (see `.env.example`).

**3. Let it see and use the screen**

```bash
uv run pilot doctor --prompt
```

This opens the two macOS permission prompts. Allow your terminal app (Terminal, iTerm, VS Code…) under **System Settings → Privacy & Security**:
- **Accessibility**, so it can read and press buttons;
- **Screen & System Audio Recording**, so it can take screenshots.

Then restart the terminal app. `uv run pilot doctor` checks that everything is in place.

**4. Give it a task**

```bash
uv run pilot "Open example.com in Safari and tell me the main heading"
```

Or run `uv run pilot` with no task for a back-and-forth session.

While it works, you'll see each step as it happens. At the end Pilot asks *"Did it do what you asked?"*. Your yes or no helps it learn, and you can press Enter to skip. Every run also leaves a visual report you can open in a browser, at `runs/<time>-<task>/report.html`.

A few useful options:

```bash
uv run pilot --approve all "..."   # ask me before every single action
uv run pilot --gui-only "..."      # only clicks and typing: no commands or scripts
uv run pilot observe               # show what Pilot sees right now, without doing anything
```

---

## Learn more

- **[TECHNICAL.md](TECHNICAL.md)**: how Pilot works in detail, from first principles. It covers the architecture, safety, self-tuning, the learning memory, and how it is measured.
- **[PROGRESS.md](PROGRESS.md)**: where the project stands, and a log of what moved it.
- **[NORTHSTAR.md](NORTHSTAR.md)**: the goal, and the rules the work follows.

## License

MIT, see [LICENSE](LICENSE).
