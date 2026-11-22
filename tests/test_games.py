"""The games suite: graders judged on scripted results, with no model and no screen."""

from __future__ import annotations

import pytest

from pilot.evals import games
from pilot.evals.fixtures import FixtureServer
from pilot.evals.runner import suite_tasks
from pilot.evals.tasks import Ctx, post


@pytest.fixture
def ctx():
    server = FixtureServer(port=0).start()
    yield Ctx(server)
    server.stop()


def test_every_game_fails_fresh_and_passes_its_scripted_solution(ctx):
    assert [t.id for t in suite_tasks("games")] == ["timing", "dial", "reaction"]
    for task in games.GAMES:
        ctx.server.reset()
        task.setup(ctx)
        assert task.check(ctx)[0] is False  # nothing played yet
        task.oracle(ctx)
        ok, detail = task.check(ctx)
        assert ok and "presses were real input" in detail, (task.id, detail)


def test_targets_are_drawn_fresh_and_shown_on_the_page(ctx):
    games.setup_timing(ctx)
    first = list(games.STATE["timing"])
    assert all(f"{t:.2f}" in ctx.server.pages["/timing.html"] or str(t) in ctx.server.pages["/timing.html"] for t in first)
    draws = set()
    for _ in range(6):
        games.setup_dial(ctx)
        draws.add(tuple(games.STATE["dial"]))
        assert " · ".join(str(n) for n in games.STATE["dial"]) in ctx.server.pages["/dial.html"]
    assert len(draws) > 1  # nothing to memorise


def test_timing_is_scored_on_distance_from_the_target(ctx):
    games.setup_timing(ctx)
    a, b, c = games.STATE["timing"]
    post(ctx, "timing", {"stops": f"{a + 0.2:.2f},{b - 0.2:.2f},{c + 0.3:.2f}", "trusted": "1,1,0"})
    ok, detail = games.check_timing(ctx)
    assert ok and "mean 0.23s" in detail and "2 of 3 presses were real input" in detail
    post(ctx, "timing", {"stops": f"{a + 2.6:.2f},{b:.2f},{c:.2f}", "trusted": "1,1,1"})  # one round reacted to the screen, late
    assert games.check_timing(ctx)[0] is False
    post(ctx, "timing", {"stops": f"{a:.2f},{b:.2f}", "trusted": "1,1"})
    assert games.check_timing(ctx) == (False, "2 of 3 rounds played")


def test_dial_needs_the_right_numbers_in_time(ctx):
    games.setup_dial(ctx)
    combo = games.STATE["dial"]
    good = ",".join(str(n) for n in combo)
    post(ctx, "dial", {"entered": good, "errors": "0", "seconds": "75.0", "moves": "60", "trusted": "1"})
    assert games.check_dial(ctx)[0] is False  # right, but too slow
    post(ctx, "dial", {"entered": ",".join(str(n) for n in [combo[0], combo[1], (combo[2] + 1) % 40]), "errors": "1", "seconds": "20.0", "moves": "60", "trusted": "1"})
    ok, detail = games.check_dial(ctx)
    assert not ok and "wrong combination" in detail
    post(ctx, "dial", {"entered": good, "errors": "0", "seconds": "20.0", "moves": "60", "trusted": "1"})
    assert games.check_dial(ctx)[0]


def test_reaction_allows_no_foul_and_no_slow_mean(ctx):
    games.setup_reaction(ctx)
    post(ctx, "reaction", {"times": "400,foul,380", "trusted": "1,1,1"})
    ok, detail = games.check_reaction(ctx)
    assert not ok and "1 foul" in detail
    post(ctx, "reaction", {"times": "2400,2600,2500", "trusted": "1,1,1"})  # looked, thought, then pressed
    assert games.check_reaction(ctx)[0] is False
    post(ctx, "reaction", {"times": "640,580,700", "trusted": "1,1,1"})
    ok, detail = games.check_reaction(ctx)
    assert ok and "mean reaction 640 ms" in detail
