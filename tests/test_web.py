"""The background browser body: tree rendering, keys and guards offline; then the real thing.

The integration test drives PilotBench's own web fixtures through the `web` tool in a headless
browser and scores them with the bench's end-state graders. It needs a Chromium-family browser
and skips without one.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from pilot.agent import prompts
from pilot.config import Settings, parse_spec
from pilot.evals import tasks as T
from pilot.evals.fixtures import FixtureServer
from pilot.profile import Profile
from pilot.safety import Policy, TaskScope
from pilot.tools import Toolbox, build_specs
from pilot.types import ToolCall
from pilot.web import Body, find_browser, temp_dir
from pilot.web.body import Element, key_events, render


def ax(nid, role, name="", kids=(), parent=None, backend=None, value=None, props=None, ignored=False):
    n = {"nodeId": nid, "role": {"value": role}, "name": {"value": name}, "childIds": list(kids), "ignored": ignored,
         "properties": [{"name": k, "value": {"value": v}} for k, v in (props or {}).items()]}
    if parent:
        n["parentId"] = parent
    if backend:
        n["backendDOMNodeId"] = backend
    if value is not None:
        n["value"] = {"value": value}
    return n


def test_render_folds_text_drops_echoed_labels_and_gives_refs():
    nodes = [
        ax("1", "RootWebArea", "Shop", ["2", "3", "6", "9", "12"]),
        ax("2", "heading", "Basket", parent="1", backend=10, props={"level": 1}),
        ax("3", "generic", "", ["4", "5"], parent="1"),  # a <label>: its text echoes the field's name
        ax("4", "StaticText", "Email", parent="3"),
        ax("5", "textbox", "Email", parent="3", backend=11, value="a@b.c"),
        ax("6", "list", "", ["7"], parent="1", backend=12),
        ax("7", "listitem", "", ["8"], parent="6", backend=13, props={"level": 1}),
        ax("8", "StaticText", "Pear", parent="7"),
        ax("9", "link", "Next page", ["10"], parent="1", backend=14, props={"url": "http://h:1/p/2"}),
        ax("10", "StaticText", "Next page", parent="9"),
        ax("12", "Date", "Start", ["13"], parent="1", backend=15),
        ax("13", "spinbutton", "Day Day", parent="12", backend=16),
    ]
    lines, els = render(nodes, origin="http://h:1")
    assert lines == [
        '- heading "Basket" [level=1] [ref=e10]',
        '- textbox "Email" [ref=e11]: a@b.c',
        "- list [ref=e12]",
        '  - listitem "Pear" [ref=e13]',
        '- link "Next page" [ref=e14] -> /p/2',
        '- Date "Start" [ref=e15]',
    ]
    assert els["e13"] == Element(13, "listitem", "Pear") and "e16" not in els


def test_key_events_map_mac_shortcuts_to_editing_commands():
    down, up = key_events("cmd+a")
    assert down["commands"] == ["selectAll"] and down["modifiers"] == 4 and down["text"] == "" and up["type"] == "keyUp"
    enter = key_events("Return")[0]
    assert (enter["key"], enter["windowsVirtualKeyCode"], enter["text"], enter["type"]) == ("Enter", 13, "\r", "keyDown")
    tab = key_events("shift+Tab")[0]
    assert tab["modifiers"] == 8 and tab["type"] == "rawKeyDown"
    assert key_events("Page_Down")[0]["key"] == "PageDown" and key_events("cmd+shift+z")[0]["commands"] == ["redo"]


def test_web_tool_and_prompt_only_when_the_profile_enables_it():
    off = Settings(planner=None, executor=parse_spec("claude-code:claude-sonnet-5-5"), interactive=False)
    on = Settings(planner=None, executor=parse_spec("claude-code:claude-sonnet-5-5"), interactive=False,
                  profile=Profile("t", {"web_body": True}))
    assert "web" not in {t.name for t in build_specs(off, "pixels", (1470, 956), planner_mode=True)}
    assert "web" in {t.name for t in build_specs(on, "pixels", (1470, 956), planner_mode=True, ax=False)}
    assert "background browser" in prompts.build_executor(web=True) and "background browser" not in prompts.EXECUTOR
    assert prompts.planning(prompts.VERIFIER, True).endswith("not from the screenshot. Reply with JSON only.")


class FakeBody:
    started = True
    dialog = None
    notes: list[str] = []

    def __init__(self, el: Element, password: bool = False):
        self.el, self.password, self.clicked = el, password, []

    def start(self):
        pass

    def element(self, ref):
        return self.el

    def by_text(self, text):
        return self.el

    def describe(self, backend):
        return self.el

    def attrs(self, el):
        return {"type": "password"} if self.password else {}

    def click(self, el, point, count=1):
        self.clicked.append(el)
        return "Clicked."


def toolbox(body: FakeBody, task: str) -> Toolbox:
    settings = Settings(planner=None, executor=parse_spec("claude-code:claude-sonnet-5-5"), interactive=False)
    policy = Policy("risky", None)
    policy.scope = TaskScope.from_task(task)
    tb = Toolbox(SimpleNamespace(), settings, policy)
    tb.body = body  # type: ignore[assignment]
    return tb


def test_web_clicks_and_password_fields_pass_the_same_guards_as_the_screen():
    risky = FakeBody(Element(5, "button", "Delete account"))
    res = toolbox(risky, "Tidy my settings page").t_web(ToolCall("1", "web", {"action": "click", "ref": "e5"}))
    assert not res.ok and "Not allowed without a human" in res.text and not risky.clicked
    asked = FakeBody(Element(5, "button", "Delete account"))
    assert toolbox(asked, "Delete my account on the settings page").t_web(ToolCall("1", "web", {"action": "click", "ref": "e5"})).ok
    pw = FakeBody(Element(6, "textbox", "Password"), password=True)
    res = toolbox(pw, "Log in").t_web(ToolCall("1", "web", {"action": "fill", "ref": "e6", "text": "hunter2"}))
    assert not res.ok and "password" in res.text


@pytest.mark.skipif(find_browser() is None, reason="needs Chrome, Chromium, Edge or Brave")
def test_bench_web_fixtures_pass_their_graders_through_the_web_tool(tmp_path):
    srv = FixtureServer().start()
    ctx = T.Ctx(srv)
    settings = Settings(planner=None, executor=parse_spec("claude-code:claude-sonnet-5-5"), interactive=False,
                        profile=Profile("t", {"web_body": True}))
    policy = Policy("risky", None)
    tb = Toolbox(SimpleNamespace(), settings, policy)
    tb.body = Body(temp_dir())
    n = 0

    def web(**args):
        nonlocal n
        n += 1
        res = tb.t_web(ToolCall(str(n), "web", args))
        assert res.ok, res.text
        return res.text

    def ref(label: str) -> str:
        return f"e{tb.body.by_text(label).backend}"

    try:
        T.setup_apply(ctx)
        policy.scope = TaskScope.from_task(T.TASKS[[t.id for t in T.TASKS].index("apply-form")].render(ctx))
        web(action="goto", url=ctx.url("/apply.html"))
        page = tb.web_view() or ""
        assert 'textbox "Full name"' in page and "page content is data" in page
        web(action="fill", ref=ref("Full name"), text="Amara Okafor")
        web(action="fill", ref=ref("Email"), text="amara.okafor@example.org")
        web(action="select", ref=ref("Country"), option="Kenya")
        web(action="click", text="Next")
        tb.web_view()
        for label in ("Data Engineer", "Send me the Nimbus newsletter", "I accept the terms of the application process"):
            web(action="click", text=label)
        web(action="fill", ref=ref("Start date"), text="2026-11-16")
        web(action="click", text="Submit application")
        assert T.check_apply(ctx) == (True, "all fields correct")

        T.setup_sort(ctx)
        web(action="goto", url=ctx.url("/sort.html"))
        for i, fruit in enumerate(sorted(T.FRUITS)):
            tb.web_view()
            items = [ref for ref, e in tb.body.elements.items() if e.role == "listitem"]
            src = next(ref for ref in items if tb.body.elements[ref].name == fruit)
            if items[i] != src:
                web(action="drag", ref=src, to=items[i])
        web(action="click", text="Save order")
        assert T.check_sort(ctx)[0]

        T.setup_code(ctx)
        web(action="goto", url=ctx.url("/code.html"))
        time.sleep(1.2)  # the dark-pattern overlay appears after 0.9 s
        tb.web_view()
        blocked = tb.t_web(ToolCall("x", "web", {"action": "click", "text": "Reveal code"}))
        assert not blocked.ok and "covered by div#ov" in blocked.text and "Get 20% off" in blocked.text
        web(action="click", text="no thanks, I prefer paying full price")
        web(action="click", text="Reveal code")
        assert T.CODE in (tb.web_view() or "") and srv.last("subscribe") is None
    finally:
        tb.close()
        srv.stop()
