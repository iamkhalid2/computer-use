"""Pointer work that went wrong in Ableton Live (runs of 21 Nov 2026), rebuilt without the app.

* Pilot pressed its own Stop button: a track header it aimed at sat under its floating window.
* A knob covered its whole range in a 10-point drag: the app holds the pointer at the knob, and moves
  laid out from the press point read as jumps that add up.
* `set_value` on a knob sent text to a number-valued control, then clicked it and typed.
"""

from __future__ import annotations

from pilot.config import ModelSpec, Settings
from pilot.mac.ax import Node
from pilot.mac.input import Input
from pilot.safety import Policy
from pilot.tools import Toolbox
from pilot.types import ToolCall
from test_agent import FakeEnv, FakeInput


def toolbox(env):
    return Toolbox(env, Settings(planner=None, executor=ModelSpec("fake", "m"), interactive=False), Policy("none"), "pixels")


class PointEnv(FakeEnv):
    """Model coordinates are screen points, and a Pilot window covers a rectangle of the screen."""

    def __init__(self, covered=(1054, 30, 400, 100)):
        super().__init__(use_ax=True)
        self.host_pid = 99
        self.covered = covered
        self.moves = 0

    def to_points(self, x, y, space="pixels"):
        return (x, y)

    def host_window_at(self, x, y):
        cx, cy, w, h = self.covered
        return cx <= x < cx + w and cy <= y < cy + h


class NoPressAX:
    """An app whose header can't be pressed through Accessibility, so the tool falls back to a click."""

    def __init__(self):
        self.values = {}

    def trusted(self, prompt=False):
        return True

    def element_at(self, x, y):
        return None

    def pid_of(self, el):
        return 7  # the app's, not Pilot's

    def attr(self, el, name):
        if name == "AXRole":
            return "AXTextField"
        return self.values.get((el, name))

    def perform(self, el, action):
        return -25205  # kAXErrorActionUnsupported

    def set_attr(self, el, name, value):
        return 0


def header(env):
    node = Node(role="AXTextField", label="Main Track Title Bar", frame=(1380.0, 80.0, 60.0, 16.0), ref="main", id=134)
    env.registry[134] = node
    env.ax = NoPressAX()
    return node


# -- Pilot's own window ---------------------------------------------------------------------------
def test_a_press_that_falls_back_to_a_click_never_lands_on_pilots_window():
    env = PointEnv()
    header(env)
    res = toolbox(env).execute(ToolCall("1", "ui", {"id": 134, "action": "press"}))
    assert not res.ok and "Pilot's own window" in res.text
    assert env.input.log == []  # the click that pressed Stop in three runs is never sent


def test_pilots_window_steps_aside_and_the_click_goes_through():
    env = PointEnv()
    header(env)

    def make_room(x, y):
        env.covered = (16, 30, 400, 100)  # the window moved to the other side
        env.moves += 1
        return True

    env.make_room = make_room
    res = toolbox(env).execute(ToolCall("1", "ui", {"id": 134, "action": "press"}))
    assert res.ok and env.moves == 1
    assert env.input.log == [("click", 1410, 88, "left", 1)]


def test_clicks_drags_and_scrolls_under_pilots_window_are_refused():
    env = PointEnv()
    env.input.drag = lambda *a, **k: env.input.log.append(("drag",) + a)
    env.input.scroll = lambda *a, **k: env.input.log.append(("scroll",) + a)
    tb = toolbox(env)
    for args in ({"action": "left_click", "coordinate": [1410, 88]},
                 {"action": "left_click_drag", "start_coordinate": [200, 400], "coordinate": [1400, 60]},
                 {"action": "scroll", "coordinate": [1200, 50], "scroll_direction": "down"}):
        res = tb.execute(ToolCall("1", "computer", args))
        assert not res.ok and "Pilot's own window" in res.text, args
    assert env.input.log == []
    assert tb.execute(ToolCall("2", "computer", {"action": "left_click", "coordinate": [500, 400]})).ok  # elsewhere is fine


# -- drags: the pointer an app holds in place ------------------------------------------------------
class SimInput(Input):
    """Input with the window server replaced: `held` apps put the pointer back where it was pressed after
    every move and read each move from there (as audio apps do with knobs); other apps follow the pointer."""

    def __init__(self, held):
        self._q = __import__("Quartz")
        self.held = held
        self.cursor = (0.0, 0.0)
        self.anchor = None
        self.read = 0.0  # the distance the app took the drag to be, along y
        self.ups = []
        self.deltas = []

    def _post(self, event):
        pass

    def position(self):
        return self.cursor

    def _mouse(self, kind, x, y, button, flags=0, clicks=1, delta=None):
        q = self._q
        if kind == q.kCGEventLeftMouseDown:
            self.anchor, self.cursor = (x, y), (x, y)
        elif kind == q.kCGEventLeftMouseDragged:
            self.deltas.append(delta)
            if self.held:
                self.read += y - self.anchor[1]  # each move measured from where the app keeps the pointer
                self.cursor = self.anchor
            else:
                self.read = y - self.anchor[1]
                self.cursor = (x, y)
        elif kind == q.kCGEventLeftMouseUp:
            self.ups.append((x, y))
        else:
            self.cursor = (x, y)


def test_a_drag_moves_a_held_pointer_by_the_distance_asked_for():
    for dy in (-8.0, -26.0, 12.0, -120.0):
        sim = SimInput(held=True)
        assert sim.drag(348, 556, 348, 556 + dy, duration=0.05) is True
        assert abs(sim.read - dy) <= 1.0, (dy, sim.read)  # was ~12x the distance: 8 points read as ~90
        assert sum(d[1] for d in sim.deltas) == round(dy)  # every move carries its delta, adding up exactly


def test_a_drag_in_an_ordinary_app_still_ends_where_it_was_aimed():
    sim = SimInput(held=False)
    assert sim.drag(175, 190, 556, 179, duration=0.05) is False  # a file dragged onto a clip slot
    assert sim.ups == [(556, 179)] and sim.cursor == (556, 179)
    assert sum(d[0] for d in sim.deltas) == 556 - 175 and sum(d[1] for d in sim.deltas) == 179 - 190


# -- knobs and sliders hold numbers -----------------------------------------------------------------
class Knob:
    """A send knob like Ableton's: a number from 0.0003 to 1.0, writes through Accessibility ignored,
    curved response to a vertical drag."""

    def __init__(self, value=0.000316, writable=False, moves=True):
        self.value, self.writable, self.moves = value, writable, moves
        self.position = (value - 0.000316) ** 0.5 * 100  # drag points from the bottom of its travel

    def drag(self, x0, y0, x1, y1, modifiers=None, duration=0.35):
        if self.moves:
            self.position = min(max(self.position - (y1 - y0), 0.0), 100.0)  # up raises
            self.value = 0.000316 + (self.position / 100) ** 2 * (1 - 0.000316)
        return True


class KnobAX(NoPressAX):
    def __init__(self, knob):
        super().__init__()
        self.knob = knob

    def attr(self, el, name):
        return {"AXRole": "AXSlider", "AXValue": self.knob.value, "AXMinValue": 0.000316, "AXMaxValue": 1.0}.get(name)

    def set_attr(self, el, name, value):
        if name == "AXValue" and self.knob.writable and isinstance(value, float):
            self.knob.value = value
            return 0
        return -25200


def knob_env(knob):
    env = PointEnv()
    env.ax = KnobAX(knob)
    env.input = FakeInput()
    env.input.drag = knob.drag
    env.registry[163] = Node(role="AXSlider", label="A-Reverb", value="0.000316", frame=(335.0, 540.0, 27.0, 32.0), ref="k", id=163)
    return env


def test_set_value_on_a_knob_that_ignores_writes_drags_it_there_by_measuring():
    knob = Knob()
    res = toolbox(knob_env(knob)).execute(ToolCall("1", "ui", {"id": 163, "action": "set_value", "value": "0.335"}))
    assert res.ok, res.text
    assert abs(knob.value - 0.335) <= 0.01 and "by dragging it" in res.text and "→" in res.text


def test_set_value_writes_a_number_where_the_app_takes_one():
    knob = Knob(writable=True)
    res = toolbox(knob_env(knob)).execute(ToolCall("1", "ui", {"id": 163, "action": "set_value", "value": "0.15"}))
    assert res.ok and knob.value == 0.15 and "by dragging" not in res.text  # the string "0.15" used to be refused


def test_set_value_explains_the_scale_instead_of_typing_units_into_a_knob():
    env = knob_env(Knob())
    tb = toolbox(env)
    for value in ("-18 dB", "1/8", "40"):
        res = tb.execute(ToolCall("1", "ui", {"id": 163, "action": "set_value", "value": value}))
        assert not res.ok and "range is 0.000316 to 1" in res.text, value
    assert env.input.log == []  # no click, no Cmd+A, no typing into a knob


def test_a_knob_that_will_not_move_says_so():
    knob = Knob(moves=False)
    res = toolbox(knob_env(knob)).execute(ToolCall("1", "ui", {"id": 163, "action": "set_value", "value": "0.5"}))
    assert not res.ok and "did not change it" in res.text


def test_a_drag_reports_what_it_did_to_the_knob_it_grabbed():
    knob = Knob()
    env = knob_env(knob)
    env.ax.element_at = lambda x, y: env.registry[163]
    res = toolbox(env).execute(ToolCall("1", "computer", {"action": "left_click_drag", "start_coordinate": [348, 556], "coordinate": [348, 548]}))
    assert res.ok and "0.000316 → 0.00671" in res.text and "range is 0.000316 to 1" in res.text
