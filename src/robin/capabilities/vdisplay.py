"""Per-account virtual X displays for headed Chromium without a physical monitor."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VirtualDisplay:
    """One Xvfb screen. Optional x11vnc for remote viewing (localhost only)."""

    display: str
    width: int = 1920
    height: int = 1080
    depth: int = 24
    _xvfb: subprocess.Popen[Any] | None = field(default=None, repr=False)
    _vnc: subprocess.Popen[Any] | None = field(default=None, repr=False)
    vnc_port: int = 0

    def start(self) -> None:
        if self._xvfb is not None:
            return
        if shutil.which("Xvfb") is None:
            raise RuntimeError(
                "Xvfb is not installed. Install xvfb, or set ROBIN_BROWSER_HEADLESS=1, "
                "or export DISPLAY to an existing X server."
            )
        cmd = [
            "Xvfb",
            self.display,
            "-screen",
            "0",
            f"{self.width}x{self.height}x{self.depth}",
            "-nolisten",
            "tcp",
            "-ac",
        ]
        self._xvfb = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._xvfb.poll() is not None:
                raise RuntimeError(f"Xvfb exited early on {self.display}")
            if _socket_ready(self.display) or _display_ready(self.display):
                return
            time.sleep(0.05)
        if self._xvfb.poll() is not None:
            raise RuntimeError(f"Xvfb failed to start on {self.display}")

    def start_vnc(self, *, port: int = 0) -> int:
        """Bind x11vnc to 127.0.0.1. Returns the listening port, or 0 if unavailable."""
        if self._vnc is not None and self.vnc_port:
            return self.vnc_port
        if shutil.which("x11vnc") is None:
            return 0
        self.start()
        chosen = port or _free_port()
        cmd = [
            "x11vnc",
            "-display",
            self.display,
            "-rfbport",
            str(chosen),
            "-localhost",
            "-nopw",
            "-forever",
            "-shared",
            "-quiet",
        ]
        self._vnc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.2)
        if self._vnc.poll() is not None:
            self._vnc = None
            return 0
        self.vnc_port = chosen
        return chosen

    def stop(self) -> None:
        for proc in (self._vnc, self._xvfb):
            if proc is None:
                continue
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        for proc in (self._vnc, self._xvfb):
            if proc is None:
                continue
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        self._vnc = None
        self._xvfb = None
        self.vnc_port = 0

    def environ(self) -> dict[str, str]:
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        return env


def _socket_ready(display: str) -> bool:
    name = display.lstrip(":")
    return Path(f"/tmp/.X11-unix/X{name}").exists()


def _display_ready(display: str) -> bool:
    try:
        check = subprocess.run(
            ["xdpyinfo", "-display", display],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
        return check.returncode == 0
    except Exception:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_DISPLAYS: dict[str, VirtualDisplay] = {}
_NEXT = 100


def display_for(account_id: str) -> VirtualDisplay:
    """Reuse one virtual display per account for the process lifetime."""
    global _NEXT
    key = account_id or "_default"
    existing = _DISPLAYS.get(key)
    if existing is not None:
        return existing
    number = _NEXT
    _NEXT += 1
    created = VirtualDisplay(display=f":{number}")
    _DISPLAYS[key] = created
    return created


def stop_display(account_id: str) -> None:
    key = account_id or "_default"
    display = _DISPLAYS.pop(key, None)
    if display is not None:
        display.stop()


def headless_requested() -> bool:
    raw = os.environ.get("ROBIN_BROWSER_HEADLESS", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def browser_engine() -> str:
    """playwright | patchright | camoufox. Prefer patchright when installed and unset."""
    raw = os.environ.get("ROBIN_BROWSER_ENGINE", "").strip().lower()
    if raw in {"playwright", "patchright", "camoufox"}:
        return raw
    try:
        import importlib.util

        if importlib.util.find_spec("patchright") is not None:
            return "patchright"
    except Exception:
        pass
    return "playwright"
