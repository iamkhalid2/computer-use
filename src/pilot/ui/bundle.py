"""Make Pilot.app: the thing a person double-clicks.

The app is a small native launcher that starts this checkout's Python with `pilot app` and waits
for it. That shape matters to macOS: permissions (Accessibility, Screen Recording, Automation)
belong to the app that is responsible for a process, so they are granted to "Pilot" by name and
cover the Python it starts. A shell-script app, or Python started straight from Finder, would
show up in Privacy settings as "zsh" or "python3.13".
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BUNDLE_ID = "app.pilot.mac"
REPO = Path(__file__).resolve().parents[3]

LAUNCHER_C = r"""
#include <fcntl.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;

int main(void) {
    const char *home = getenv("HOME");
    char path[4096], log[4096];
    snprintf(path, sizeof path, "%s/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", home ? home : "");
    setenv("PATH", path, 1);              /* apps opened from Finder get a bare PATH: this is where `claude` lives */
    setenv("PILOT_LAUNCHER", "1", 1);
    setenv("PYTHONUNBUFFERED", "1", 1);
    if (chdir(PILOT_REPO) != 0) return 71;
    snprintf(log, sizeof log, "%s/Library/Logs/Pilot.log", home ? home : "/tmp");
    int fd = open(log, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd >= 0) { dup2(fd, 1); dup2(fd, 2); close(fd); }
    char *argv[] = {PILOT_PYTHON, "-m", "pilot", "app", NULL};
    for (;;) {
        pid_t pid;
        int status;
        if (posix_spawn(&pid, PILOT_PYTHON, NULL, NULL, argv, environ) != 0) return 72;
        while (waitpid(pid, &status, 0) < 0) {}
        if (!(WIFEXITED(status) && WEXITSTATUS(status) == 75)) return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
        /* 75: "start me again", after the user switched on a permission */
    }
}
"""


def _icon(resources: Path) -> str | None:
    """A simple icon: a white pointer on deep blue. Skipped quietly if the tools are missing."""
    try:
        from PIL import Image, ImageDraw

        n = 1024
        img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for y in range(n):  # vertical gradient inside a rounded square
            t = y / n
            d.line([(0, y), (n, y)], fill=(int(36 + 20 * t), int(84 - 30 * t), int(220 - 60 * t), 255))
        mask = Image.new("L", (n, n), 0)
        ImageDraw.Draw(mask).rounded_rectangle([64, 64, n - 64, n - 64], radius=200, fill=255)
        img.putalpha(mask)
        d = ImageDraw.Draw(img)
        d.polygon([(330, 250), (330, 760), (455, 640), (545, 830), (625, 792), (535, 606), (710, 606)], fill=(255, 255, 255, 255))
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "Pilot.iconset"
            iconset.mkdir()
            for size in (16, 32, 128, 256, 512):
                img.resize((size, size), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
                img.resize((size * 2, size * 2), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "Pilot.icns")], check=True, capture_output=True)
        return "Pilot"
    except Exception:  # noqa: BLE001 - an icon is a nicety
        return None


def build(dest_dir: Path | None = None, repo: Path = REPO, python: Path | None = None) -> Path:
    python = python or repo / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    app = (dest_dir or Path.home() / "Applications") / "Pilot.app"
    if app.exists():
        try:
            mine = plistlib.loads((app / "Contents" / "Info.plist").read_bytes()).get("CFBundleIdentifier") == BUNDLE_ID
        except Exception:  # noqa: BLE001
            mine = False
        if not mine:
            raise SystemExit(f"{app} exists and is not an app Pilot made. Move it away, or pass --dest.")
        shutil.rmtree(app)
    macos, resources = app / "Contents" / "MacOS", app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "launcher.c"
        src.write_text(LAUNCHER_C)
        esc = lambda p: str(p).replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
        cmd = ["cc", "-O2", "-o", str(macos / "Pilot"), str(src), f'-DPILOT_REPO="{esc(repo)}"', f'-DPILOT_PYTHON="{esc(python)}"']
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode != 0:
            shutil.rmtree(app, ignore_errors=True)
            raise SystemExit("Could not build the launcher (needs Apple's command line tools: `xcode-select --install`).\n" + done.stderr[:600])
    info = {
        "CFBundleName": "Pilot", "CFBundleDisplayName": "Pilot", "CFBundleIdentifier": BUNDLE_ID, "CFBundleExecutable": "Pilot",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "2.0", "CFBundleVersion": "2.0", "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,  # no Dock icon: the window floats, and the app you were using stays the active one
        "NSHighResolutionCapable": True,
        "NSAppleEventsUsageDescription": "Pilot operates other apps for you when you give it a task.",
    }
    if icon := _icon(resources):
        info["CFBundleIconFile"] = icon
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    # Ad-hoc signing gives macOS one stable identity to attach the permissions to.
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], capture_output=True, check=False)
    return app
