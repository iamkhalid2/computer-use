"""Key-name parsing for macOS virtual keycodes.

Models are trained on xdotool-style names ("Return", "ctrl+s", "super+Tab",
"Page_Down"). This module turns those into (modifier flags, keycode) pairs for
Quartz events. It is pure Python so it can be unit-tested anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

# Quartz CGEventFlags
FLAG_SHIFT = 1 << 17
FLAG_CONTROL = 1 << 18
FLAG_OPTION = 1 << 19
FLAG_COMMAND = 1 << 20
FLAG_FN = 1 << 23

MODIFIERS: dict[str, tuple[int, int]] = {
    # name -> (flag, keycode)
    "cmd": (FLAG_COMMAND, 0x37),
    "command": (FLAG_COMMAND, 0x37),
    "super": (FLAG_COMMAND, 0x37),
    "meta": (FLAG_COMMAND, 0x37),
    "win": (FLAG_COMMAND, 0x37),
    "windows": (FLAG_COMMAND, 0x37),
    "ctrl": (FLAG_CONTROL, 0x3B),
    "control": (FLAG_CONTROL, 0x3B),
    "alt": (FLAG_OPTION, 0x3A),
    "option": (FLAG_OPTION, 0x3A),
    "opt": (FLAG_OPTION, 0x3A),
    "shift": (FLAG_SHIFT, 0x38),
    "fn": (FLAG_FN, 0x3F),
}

# US-ANSI virtual keycodes (Carbon kVK_*).
KEYCODES: dict[str, int] = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06,
    "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E,
    "r": 0x0F, "y": 0x10, "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15,
    "6": 0x16, "5": 0x17, "=": 0x18, "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C,
    "0": 0x1D, "]": 0x1E, "o": 0x1F, "u": 0x20, "[": 0x21, "i": 0x22, "p": 0x23,
    "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29, "\\": 0x2A, ",": 0x2B,
    "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, "`": 0x32,
    "return": 0x24, "enter": 0x24, "kp_enter": 0x4C,
    "tab": 0x30, "space": 0x31, " ": 0x31,
    "backspace": 0x33, "delete": 0x75, "forwarddelete": 0x75,
    "escape": 0x35, "esc": 0x35,
    "capslock": 0x39, "caps_lock": 0x39,
    "home": 0x73, "end": 0x77, "pageup": 0x74, "page_up": 0x74, "prior": 0x74,
    "pagedown": 0x79, "page_down": 0x79, "next": 0x79,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
    "help": 0x72, "insert": 0x72,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61,
    "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
    "f13": 0x69, "f14": 0x6B, "f15": 0x71, "f16": 0x6A, "f17": 0x40, "f18": 0x4F,
    "f19": 0x50, "f20": 0x5A,
}

# xdotool keysym names for punctuation.
ALIASES: dict[str, str] = {
    "minus": "-", "equal": "=", "equals": "=", "bracketleft": "[", "bracketright": "]",
    "semicolon": ";", "apostrophe": "'", "quote": "'", "grave": "`", "backslash": "\\",
    "comma": ",", "period": ".", "slash": "/", "arrowleft": "left", "arrowright": "right",
    "arrowup": "up", "arrowdown": "down", "del": "delete", "bksp": "backspace",
    "spacebar": "space",
}

# Characters that need shift on a US layout -> unshifted key.
SHIFTED: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";",
    '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}
SHIFTED_NAMES: dict[str, str] = {
    "plus": "+", "exclam": "!", "at": "@", "numbersign": "#", "dollar": "$",
    "percent": "%", "asciicircum": "^", "ampersand": "&", "asterisk": "*",
    "parenleft": "(", "parenright": ")", "underscore": "_", "braceleft": "{",
    "braceright": "}", "bar": "|", "colon": ":", "quotedbl": '"', "less": "<",
    "greater": ">", "question": "?", "asciitilde": "~",
}


@dataclass(frozen=True)
class KeyStroke:
    flags: int
    keycode: int
    modifier_keycodes: tuple[int, ...] = ()

    @property
    def is_modifier_only(self) -> bool:
        return self.keycode in {code for _, code in MODIFIERS.values()}


class KeyParseError(ValueError):
    pass


def _split_combo(combo: str) -> list[str]:
    """Split on '+' while letting a trailing '+' be a key ("cmd++" -> cmd, +)."""
    combo = combo.strip()
    if not combo:
        raise KeyParseError("empty key")
    if combo == "+":
        return ["+"]
    parts: list[str] = []
    buf = ""
    for i, ch in enumerate(combo):
        if ch == "+" and buf:
            parts.append(buf)
            buf = ""
        elif ch == "+" and not buf and i == len(combo) - 1:
            parts.append("+")
        else:
            buf += ch
    if buf:
        parts.append(buf)
    return [p.strip() for p in parts if p.strip() or p == " "]


def parse_combo(combo: str) -> KeyStroke:
    """Parse "cmd+shift+t", "Return", "ctrl+alt+Delete", "cmd+?" etc."""
    parts = _split_combo(combo)
    flags = 0
    mod_codes: list[int] = []
    for raw in parts[:-1]:
        if raw.lower() not in MODIFIERS:
            raise KeyParseError(f"unknown modifier {raw!r} in {combo!r}")
        flag, code = MODIFIERS[raw.lower()]
        flags |= flag
        mod_codes.append(code)
    key_part = parts[-1]

    name = key_part if len(key_part) == 1 else key_part.lower()
    if name.lower() in MODIFIERS:
        flag, code = MODIFIERS[name.lower()]
        return KeyStroke(flags | flag, code, tuple(mod_codes))
    name = ALIASES.get(name, name)
    name = SHIFTED_NAMES.get(name, name)
    if len(name) == 1 and name.isalpha() and name.isupper():
        flags |= FLAG_SHIFT
        name = name.lower()
    if name in SHIFTED:
        flags |= FLAG_SHIFT
        name = SHIFTED[name]
    code = KEYCODES.get(name)
    if code is None:
        raise KeyParseError(f"unknown key {key_part!r} in {combo!r}")
    return KeyStroke(flags, code, tuple(mod_codes))


def parse_modifiers(text: str | None) -> int:
    """Modifier text for clicks/scrolls: "shift", "cmd+shift"."""
    if not text:
        return 0
    flags = 0
    for raw in text.replace(" ", "").split("+"):
        if not raw:
            continue
        if raw.lower() not in MODIFIERS:
            raise KeyParseError(f"unknown modifier {raw!r}")
        flags |= MODIFIERS[raw.lower()][0]
    return flags
