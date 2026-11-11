# North Star

## The product

In Khalid's words, November 2026:

> A computer-use agent that a layman can send even a vague, big task to: book a flight ticket for me, find jobs for me and apply, organise my drive cleanly. It breaks the task down, asks follow-up questions, starts working, and works fast.
>
> A harness that is the best at general computer use. Slower and costlier on the first runs of novel tasks, because those are coordinated by Opus. Once that is done, the general model, Sonnet, learns from the Opus runs and does them where needed, with Opus stepping in intelligently when needed. As one keeps using the harness it keeps getting cheaper and faster, while being extremely resilient and reliable.

Two rules follow from it:
- **Pilot improves itself.** When a real run exposes a weakness in how to use an app or site, the fix should arrive as something Pilot learned (a playbook, a tuned setting), not as a developer reading the trace and patching the harness. Hand edits are for bugs and safety holes.
- **The scorecard must measure this product.** It does not yet: it has no milestone for the front door (asking, routing) or for the cost of the n-th run of a job against the first. Until it does, judge work against the words above first and the percentage second.

## The five pillars

Pilot should be a Mac agent on the Pareto frontier. That means five things at once:
- **intelligent**: it solves tasks it has never seen and updates itself, in a Bayesian way, from every attempt;
- **self-improving**: it gets better without a human in the loop, and proves it on tasks it was never tuned on;
- **cheap**: few dollars and seconds per solved task;
- **autonomous**: it needs little of your attention and delivers a lot of value;
- **measured honestly**: on every axis, against something other than itself.

## The scorecard

`python3 scripts/northstar.py` computes progress from evidence, never from claims:
- eval results in `evals/` (bench and sealed held-out);
- run traces in `runs/`;
- the tuning ledger;
- the lesson store in `experience/`;
- transfer and retry reports in `evals/learning/`.

Each pillar has weighted milestones. A milestone earns credit only for a measured outcome. Code that exists but has not moved a number earns nothing.

The scorecard also ranks the **biggest jumps**, in overall points gained if finished. [PROGRESS.md](PROGRESS.md) is the same scorecard for people, with the log of every move; the Stop hook rewrites it after every turn.

## Operating rules

This is a startup, not a corporation.

1. **Start from the scorecard.** Work on the biggest jump, usually in the weakest pillar.
2. **One big bet per session.** Build it end to end, then run one decisive test against outside evidence: the sealed held-out suite, the graders, the tz database. Never grade it by our own opinion.
3. **Details only when they block the bet.** A fix that moves no milestone is a detail. Do it fast, say so, and move on.
4. **Ask once, batched.** Only screen or quota runs, policy decisions and pushes need Khalid.
5. **Account for every session that changes code.** Run `python3 scripts/northstar.py log "<what moved>"`. The delta is computed, and anything under 1 point is refused. Otherwise run `log --blocked "<the decision Khalid must make>"`. The log is `northstar/log.jsonl`.

## The hooks (`.claude/settings.json`)

| Event | Command | Effect |
|---|---|---|
| SessionStart | `northstar.py start` | puts the full scorecard and these rules in context, and records the session's baseline |
| UserPromptSubmit | `northstar.py prompt` | one line before each request: the score, the biggest jump, and "does this move a pillar?" |
| Stop | `northstar.py gate` | rewrites [PROGRESS.md](PROGRESS.md) from the evidence; blocks stopping once if code changed with no log entry, then lets the session stop and tells you |
