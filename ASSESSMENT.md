# Independent assessment, 9 November 2026

**Question asked:** is Pilot the agent a layman can hand a vague, big task ("book a flight ticket for me", "find jobs for me and apply", "organise my drive cleanly"), which then breaks the task down, asks follow-up questions and starts working, fast?

**Answer before today: no.** Pilot was a fast, careful executor of fully specified tasks on test pages. Nothing in it was built, measured or ever exercised for a vague request.

**Answer after today's changes: the front door now works and is measured. Everything behind it for real bookings and job sites is still untested.**

Method: I read the code path a request takes and the raw traces in `runs/`, not the docs or the scorecard. I probed the agent's first move on vague requests without executing anything, and replayed recorded data through changed code. Nothing here took over the screen.

## What the evidence showed

| # | Finding | Evidence |
|---|---|---|
| 1 | **The mission had never been attempted.** | All 430 recorded runs are test fixtures (localhost pages, `~/PilotEval`). In 3,803 actions `ask_user` was called 0 times and an up-front plan was made 0 times. The scorecard's own "real tasks" count is 0. |
| 2 | **The scorecard measures a different product.** | None of its 24 milestones mentions a vague request, a question, or a breakdown. Its top-ranked next move was "lessons win on the sealed suite", a result already measured as null (36/42 vs 37/42). |
| 3 | **Answers to questions were lost.** | An answer lived only in the executor session that asked. The next step, every replan and the final check were built from the original sentence alone. |
| 4 | **Steps did not share what they found.** | In the first end-to-end attempt, step 1 listed 26 files; step 2 started fresh, knew only "26 loose files", and had to list them again. |
| 5 | **Naming a verb to forbid it authorised it.** | "Fill in the form but do not submit it" allowed Submit with no confirmation. "Don't delete anything" allowed Delete. "Never buy it without asking me" allowed Place order. |
| 6 | **The shell guard judged only the first line.** | `ls` then, on a new line, `rm -rf ~/Documents/x` ran with no confirmation. So did a `curl` upload of `~/.ssh/id_rsa` on line two. "0 breaches in 95 attack runs" held because no attack used a line break. |
| 7 | **The shell guard also over-blocked.** | A harmless `stat -f "%Sm\|%N"` was stopped as "unparseable": operators inside quotes were treated as real. |
| 8 | **A long job simply died at its limit.** | 15 minutes, 80 turns or $3, then stop, with time spent waiting for the user counted as work. No run over 5 minutes has ever passed. |
| 9 | **What already worked.** | 187 tests passed cold. Median task 14 s. On vague requests the executor model usually asked on its own first turn (9 of 10 probes), but "clean up my inbox" opened Mail and started. |

## What was changed

1. **A front desk** (`src/pilot/intake.py`). One fast text-only call decides: start now, or ask up to three questions and lay out a route. It runs while the executor takes its first look, so a clear task is not delayed.
2. **Answers and findings last the run.** Answers are appended to the request that every step, replan and final check reads. A route's milestones become the plan, and each step's findings reach the later steps.
3. **Long jobs check in.** At a limit Pilot asks "keep going?" instead of stopping. Waiting for your answer no longer counts as work time.
4. **Finding 5 fixed.** A negated verb authorises nothing.
5. **Findings 6 and 7 fixed.** Each line of a shell command is judged on its own; quoted operators are text.
6. **`pilot brief "…"`** shows the front desk's verdict without doing anything. `pilot brief --check dev|sealed` grades it. `--no-ask` switches it off.
7. Without a keyboard attached (piped, scheduled), Pilot no longer offers a question tool it cannot use.

Evals and scripts have nobody to ask, so the front desk is off there. A non-interactive run sends byte-identical prompts before and after (diffed).

## How the changes were checked

| Check | Graded by | Result |
|---|---|---|
| Vague requests are asked about (12, the first three are the mission's own words) | fixed rules in `evals/intake.py` | 12, 12 and 11 of 12 over three runs |
| Clear requests just start (10 everyday + 16 bench prompts) | same | 26 of 26, four runs |
| Sealed held-out prompts, read once after the prompt was final | same | 14 of 14 start, no needless question |
| A clear task is not slowed | trace timestamps, 3 runs each way | first action 2.5–2.9 s with, 2.5–2.7 s without |
| "organise my drive cleanly" end to end, real models, scripted user, sandbox of 26 loose files | SHA-256 of every file before and after | first question at 6.1 s, plan approved at 30 s, done in 143 s; 26 of 26 files intact, 0 left loose |
| Shell guard change does not alter past behaviour | all 431 recorded shell commands through old and new guard | 0 verdicts changed |
| Negation fix does not alter the suites | authorised verbs for all 30 bench and sealed prompts | 0 changed |
| Unit tests | pytest | 203 pass (187 before, 16 new) |

## What is not verified

* **No vague task has run on the real screen.** The end-to-end run used a still picture, with only `shell` and `write_file` able to act.
* **No flight was searched and no job applied for.** Those need a signed-in browser, many minutes across real sites and a person answering. Expect new failures there.
* **The sealed suite was not re-run.** The argument that it cannot have moved is structural (identical prompts, identical guard verdicts), not a fresh 42-run measurement.
* The one miss in the "ask" set, "order my usual groceries", may be a wrong label: looking up past orders is a fair alternative to asking.
* In the end-to-end run 65 of 143 seconds went to four replans by the slow planner, partly because the test declined every approval. Real numbers will differ.

## Decisions for Khalid

1. **Run the three mission tasks for real** (needs the screen, your signed-in browser, and you answering for about 30 minutes).
2. **Money.** "Buy me a ticket" currently authorises the final Pay button without a further question, as the sealed checkout task requires. With a person present, should Pilot always ask before money moves?
3. **Plain-word folders.** "Downloads" in an answer is not `~/Downloads` to the guard, so every `mv` there asks first. Should plain folder names count as naming the folder?
4. **The scorecard.** It should carry the mission: a front-door milestone (the `pilot brief --check` numbers), and "real tasks" weighted as the main goal. I did not edit it: an assessor should not change the scale that grades their own work.

## Recommended next, in order

1. The real runs in decision 1. Every failure becomes a bench task.
2. A small "about me" store (home airport, CV path, usual accounts), filled from answers, so the same question is never asked twice.
3. Turn the background browser on for research steps (searching flights, collecting job listings) so they run without taking the screen; keep the user's own browser for the signed-in final steps.
4. Use a faster model for replans: at 9–19 s each they are now the largest wait in a routed job.

---

## Update, 11 November 2026

What happened after the audit, in order.

**A window, so nobody needs the command line.** `pilot app --install` builds Pilot.app: a small floating panel that takes the task, asks Pilot's questions, shows progress in plain words, and has Stop. Launched the way a user would, macOS named it "Pilot" in its permission prompts and the first-run screen showed the two switches to turn on. Its local control channel refuses anything but its own page.

**The first real tasks.** Three runs from the window: a Spotify task finished in 46 seconds and was rated up; a large trip-planning task was stopped once by the user and once by the Claude plan's usage limit after 104 seconds. So decision 1 above is partly answered: one small real task works; no large one has finished.

**A correction from Khalid.** After the Spotify run I read the trace and hard-coded a fix for a slow wait. He stopped me: a developer reading traces and patching the harness is the opposite of the product, which must improve itself from use. He was right, and the audit above had not named it: every improvement in this document was made by hand.

**Playbooks, built in answer.** A new kind of job is led by the planner (Opus). When it succeeds, the executor model (Sonnet) distils the route into a playbook. The same kind of job is then led by the executor with no planner call, and a belief in each playbook, moved by every outcome and by the user's thumbs, decides who leads after that.

| Check | Result |
|---|---|
| Scripted models: who leads run one and run two | planner, then executor with zero planner calls |
| Scripted models: a playbook that fails twice | the planner takes the job back; its next success rewrites the route |
| Real model, one call: distil the real Spotify run | Pilot wrote the same "do not wait for text in Spotify" rule that had been hand-coded |
| All unit tests | 219 pass |

**Still not verified.**
* That a warm run is cheaper than a cold one on screen. One command measures it: `pilot eval --playbooks --repeat 3` on five bench tasks, then `pilot learn curve`.
* Any large, multi-site task from start to finish.
* The sealed suite has not been re-run since these changes. Two of them reach eval runs: the tuned `sub` profile now loads by default, and waits give up early in apps with no accessibility text.

**Found on the way.** Everyday runs were not using the tuned profile at all (only `--profile sub` loaded it). The disk evidence shown to the verifier was two folder levels deep, so a correct tidy-up was reported as not finished. Both are fixed.

**Decisions still open.** Money (always ask before paying when a person is present), plain folder names as scope, and the scorecard, which still has no milestone for the front door or for cold-versus-warm cost.
