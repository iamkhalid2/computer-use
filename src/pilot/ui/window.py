"""Pilot's window: a small floating panel that stays out of the agent's way.

Three things make it safe for an agent that drives the same screen:
  * It never becomes the active app (a non-activating panel in an app with no Dock icon), so the
    app the user was in stays frontmost and is what the agent sees and types into.
  * It takes the keyboard only while it has something to ask. While Pilot works the panel refuses
    key status, so pressing Stop never steals typing from the app being driven.
  * It shrinks to a strip while Pilot works, and Pilot's own guards refuse any click or keystroke
    aimed at its own process (`env.host_pid`).
"""

from __future__ import annotations

from typing import Any

import AppKit
import objc
import WebKit
from Foundation import NSURL, NSMakeRect, NSObject, NSURLRequest
from PyObjCTools import AppHelper

FULL = (400.0, 640.0)
STRIP = (400.0, 70.0)
SETTLE = 0.9  # seconds of uninterrupted work before the window shrinks: questions often come in a row
MARGIN = 16.0


class Panel(AppKit.NSPanel):
    wants_keys = True

    def canBecomeKeyWindow(self) -> bool:  # noqa: N802
        return bool(self.wants_keys)

    def canBecomeMainWindow(self) -> bool:  # noqa: N802
        return False


class Delegate(NSObject):
    session = objc.ivar()

    def windowWillClose_(self, _note: Any) -> None:  # noqa: N802
        if self.session is not None:
            self.session.request_stop()
        AppKit.NSApp.terminate_(None)


def _edit_menu(app: Any) -> None:
    """Copy, paste and undo are menu commands: without an Edit menu they do nothing in a text box."""
    bar = AppKit.NSMenu.alloc().init()
    item = AppKit.NSMenuItem.alloc().init()
    bar.addItem_(item)
    edit = AppKit.NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                               ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")]:
        edit.addItemWithTitle_action_keyEquivalent_(title, action, key)
    item.setSubmenu_(edit)
    app.setMainMenu_(bar)


class Window:
    def __init__(self, url: str, session: Any):
        self.session = session
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        _edit_menu(app)
        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable | AppKit.NSWindowStyleMaskResizable
                 | AppKit.NSWindowStyleMaskNonactivatingPanel)
        screen = AppKit.NSScreen.mainScreen().visibleFrame()
        x = screen.origin.x + screen.size.width - FULL[0] - MARGIN
        y = screen.origin.y + screen.size.height - FULL[1] - MARGIN
        self.panel = Panel.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(x, y, *FULL), style, AppKit.NSBackingStoreBuffered, False)
        p = self.panel
        p.setTitle_("Pilot")
        p.setFloatingPanel_(True)
        p.setLevel_(AppKit.NSFloatingWindowLevel)
        p.setHidesOnDeactivate_(False)
        p.setBecomesKeyOnlyIfNeeded_(False)
        p.setReleasedWhenClosed_(False)
        p.setMinSize_((320.0, STRIP[1]))
        p.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.delegate = Delegate.alloc().init()
        self.delegate.session = session
        p.setDelegate_(self.delegate)
        self.web = WebKit.WKWebView.alloc().initWithFrame_configuration_(p.contentView().bounds(), WebKit.WKWebViewConfiguration.alloc().init())
        self.web.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        p.setContentView_(self.web)
        self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        self.full = FULL
        self.compact = False
        self.state = "idle"
        p.makeKeyAndOrderFront_(None)
        p.orderFrontRegardless()
        session.on_state = lambda state: AppHelper.callAfter(self.apply, state)

    def _resize(self, compact: bool) -> None:
        """Between the full window and the strip, keeping the top-right corner where the user left it."""
        if compact == self.compact:
            return
        f = self.panel.frame()
        if not self.compact:
            content = self.panel.contentRectForFrameRect_(f).size
            self.full = (content.width, content.height)
        w, h = (self.full[0], STRIP[1]) if compact else self.full
        target = self.panel.frameRectForContentRect_(NSMakeRect(0, 0, w, h)).size
        top, right = f.origin.y + f.size.height, f.origin.x + f.size.width
        self.panel.setFrame_display_animate_(NSMakeRect(right - target.width, top - target.height, target.width, target.height), True, True)
        self.compact = compact

    def apply(self, state: str) -> None:
        p = self.panel
        print(f"window: {state} (had the keyboard: {bool(p.isKeyWindow())})", flush=True)
        self.state = state
        if state == "working":
            p.wants_keys = False
            if p.isKeyWindow():  # hand the keyboard back to the app the user was in, at once
                p.orderOut_(None)
            p.orderFrontRegardless()
            AppHelper.callLater(SETTLE, self._shrink_if_still_working)
        else:
            p.wants_keys = True
            self._resize(False)
            if state == "asking":
                p.makeKeyAndOrderFront_(None)
            else:
                p.orderFrontRegardless()
            if state in {"asking", "done"}:
                sound = AppKit.NSSound.soundNamed_("Glass" if state == "done" else "Tink")
                if sound is not None:
                    sound.play()

    def _shrink_if_still_working(self) -> None:
        if self.state == "working":
            self._resize(True)

    def run(self) -> None:
        AppHelper.runEventLoop(installInterrupt=True)


def quit_app() -> None:
    AppHelper.callAfter(AppKit.NSApp.terminate_, None)
