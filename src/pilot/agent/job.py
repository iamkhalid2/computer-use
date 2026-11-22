"""A job that spans several messages.

People don't finish a big request in one message. They say "continue", "fix it", "make it more complex".
Each of those used to start a new task that knew only the last task's one-line result, so Pilot rebuilt
from what it saw on screen. In one Ableton session it wrote the same drum beat three times, and every
"continue" dropped the "don't ask me for permission" the user had given one message earlier.

A Job keeps an unfinished request going: the user's words so far (which are also what authorises the
work), the steps already finished, and where the last run stopped. A run that finishes closes the job;
the next message then starts a new one, with the finished job as one line of history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_WORDS = 8  # messages of one job kept
MAX_DONE = 30  # finished steps kept
HISTORY = 4  # finished jobs shown as history

CONTINUES = ("The user's latest message continues the unfinished job below, unless it plainly starts something new. "
             "Keep what is finished: do not redo those steps. Plan only what is left, and whatever the latest message "
             "adds or changes. Work until the whole job is done.")


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass
class Job:
    words: list[str] = field(default_factory=list)  # the user's messages for the unfinished job, oldest first
    done: list[str] = field(default_factory=list)  # finished steps across its runs: "goal: what was done"
    left: list[str] = field(default_factory=list)  # steps the last run did not finish
    ended: str = ""  # how the last run ended
    history: list[str] = field(default_factory=list)  # finished jobs and one-off tasks, one line each

    @property
    def open(self) -> bool:
        return bool(self.words)

    def earlier(self) -> list[str]:
        """The user's earlier messages for the job the next message continues."""
        return list(self.words)

    def context(self) -> str:
        """What a run is told about this session before it starts."""
        parts = []
        if self.history:
            parts.append(" | ".join(self.history[-HISTORY:]))
        if self.open:
            lines = [CONTINUES, "The job so far, in the user's words (oldest first):", *(f"  - {w}" for w in self.words)]
            if self.done:
                lines += ["Finished already (keep this work; do not redo it):", *(f"  - {d}" for d in self.done[-MAX_DONE:])]
            if self.left:
                lines += ["Not finished when the last run ended:", *(f"  - {g}" for g in self.left)]
            if self.ended:
                lines.append(f"The last run ended: {self.ended}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def after(self, task: str, out: Any) -> None:
        """Record a run's outcome: a success closes the job, anything else keeps it open for the next message."""
        first = self.words[0] if self.words else task
        if out.success:
            said = first if first == task else f"{_clip(first, 100)} … {_clip(task, 80)}"
            self.history.append(f"{said} → done" + (f": {_clip(out.answer, 120)}" if out.answer else ""))
            self.words, self.done, self.left, self.ended = [], [], [], ""
            return
        self.words = (self.words + [task])[-MAX_WORDS:]
        steps = list(getattr(out, "steps", []) or [])
        self.done += [_clip(f"{s.goal}: {s.summary}" if s.summary else s.goal, 300) for s in steps if s.status == "done"]
        self.left = [_clip(s.goal, 200) for s in steps if s.status != "done"]
        self.ended = _clip(getattr(out, "reason", "") or "not finished", 300)

    def crashed(self, task: str, error: str) -> None:
        """A run that died before it could report: keep the message, so the next one still continues the job."""
        self.words = (self.words + [task])[-MAX_WORDS:]
        self.ended = _clip(error, 300)
