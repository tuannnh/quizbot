"""QuizBot desktop agent (Windows + macOS).

Press Ctrl + ` (configurable): captures the window under the mouse pointer and sends it to the
QuizBot server, which reads the question and posts the answer to the dashboard.

Config comes from `quizbot.env` next to this file (falling back to environment variables).
The file is re-read on every capture, so edits apply immediately, without a restart:
    QUIZBOT_SERVER   http://10.1.1.99:11138
    QUIZBOT_TOKEN    shared secret (same as server)
    QUIZBOT_SESSION  label for this quiz; empty = the session set on the dashboard
    QUIZBOT_MODEL    model for this machine; empty = the model set on the dashboard
    QUIZBOT_MACHINE  label for this computer (default: hostname)
    QUIZBOT_NOTIFY   1 = show a desktop notification with the answer (default 1)
    QUIZBOT_COPY_CODE 1 = put the solution on the clipboard for coding problems (default 1)
    QUIZBOT_HOTKEY   modifiers + `, e.g. ctrl+` (default), alt+`, ctrl+shift+`  (restart to apply)
"""
from __future__ import annotations

import io
import logging
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

import mss
import requests
from PIL import Image
from pynput import keyboard

HERE = Path(__file__).resolve().parent
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


# ---------------------------------------------------------------- config
ENV_FILE = HERE / "quizbot.env"


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _env_value(value)
    return values


def _env_value(raw: str) -> str:
    """dotenv-style value: quotes keep everything inside; otherwise ` # comment` is dropped."""
    raw = raw.strip()
    if raw.startswith("#"):  # KEY=   # comment  -> empty
        return ""
    if len(raw) >= 2 and raw[0] in "\"'":
        end = raw.find(raw[0], 1)
        if end != -1:
            return raw[1:end]
    for i, ch in enumerate(raw):
        if ch == "#" and i > 0 and raw[i - 1].isspace():
            return raw[:i].strip()
    return raw


@dataclass(frozen=True)
class Config:
    server: str
    token: str
    session: str   # "" = let the server pick (dashboard setting, else today's date)
    model: str     # "" = let the server pick (dashboard setting, else server default)
    machine: str
    notify: bool
    copy_code: bool
    hotkey: str    # read at startup only; restart the agent after changing it


def load_config() -> Config:
    """quizbot.env wins over process environment; re-read on every capture."""
    file = read_env_file(ENV_FILE)

    def get(key: str, default: str = "") -> str:
        return (file.get(key) if key in file else os.getenv(key, default)) or default

    return Config(
        server=get("QUIZBOT_SERVER", "http://127.0.0.1:8765").rstrip("/"),
        token=get("QUIZBOT_TOKEN"),
        session=get("QUIZBOT_SESSION").strip(),
        model=get("QUIZBOT_MODEL").strip(),
        machine=get("QUIZBOT_MACHINE").strip() or socket.gethostname(),
        notify=get("QUIZBOT_NOTIFY", "1") == "1",
        copy_code=get("QUIZBOT_COPY_CODE", "1") == "1",
        hotkey=get("QUIZBOT_HOTKEY", "ctrl+`").strip(),
    )


HTTP = requests.Session()  # keep-alive: avoids a new TCP handshake per capture
DEBOUNCE_S = 1.0

log = logging.getLogger("quizbot-agent")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_file = RotatingFileHandler(HERE / "agent.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
_file.setFormatter(_fmt)
log.addHandler(_file)
if sys.stdout and sys.stdout.isatty():
    _console = logging.StreamHandler()
    _console.setFormatter(_fmt)
    log.addHandler(_console)


# ---------------------------------------------------------------- window under pointer
Rect = tuple[int, int, int, int]  # left, top, right, bottom


def _win_pointer_window() -> tuple[tuple[int, int], Rect | None]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND

    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return (pt.x, pt.y), None
    root = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT: top-level window, not a child control
    rect = wintypes.RECT()
    # Extended frame bounds exclude the invisible resize border/shadow on Windows 10/11.
    got = ctypes.windll.dwmapi.DwmGetWindowAttribute(root, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0
    if not got:
        user32.GetWindowRect(root, ctypes.byref(rect))
    return (pt.x, pt.y), (rect.left, rect.top, rect.right, rect.bottom)


def _mac_pointer_window() -> tuple[tuple[int, int], Rect | None]:
    import Quartz

    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    x, y = int(loc.x), int(loc.y)
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    for w in windows:  # ordered front to back
        if w.get("kCGWindowLayer", 0) != 0:  # skip menu bar, dock, overlays
            continue
        b = w.get("kCGWindowBounds", {})
        left, top, width, height = int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"])
        if width < 80 or height < 80:
            continue
        if left <= x < left + width and top <= y < top + height:
            return (x, y), (left, top, left + width, top + height)
    return (x, y), None


def pointer_window() -> tuple[tuple[int, int], Rect | None]:
    try:
        if IS_WIN:
            return _win_pointer_window()
        if IS_MAC:
            return _mac_pointer_window()
    except Exception:
        log.exception("could not resolve window under pointer")
    return (0, 0), None


# ---------------------------------------------------------------- capture
def _monitor_at(sct: mss.base.MSSBase, x: int, y: int) -> Rect:
    for m in sct.monitors[1:]:
        if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
            return m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"]
    m = sct.monitors[1]
    return m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"]


def _clip(rect: Rect, bounds: Rect) -> Rect:
    l, t, r, b = rect
    L, T, R, B = bounds
    return max(l, L), max(t, T), min(r, R), min(b, B)


def _capture_mac(rect: Rect) -> bytes:
    # The built-in `screencapture` is more reliable on recent macOS than the deprecated CG image API.
    l, t, r, b = rect
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    try:
        subprocess.run(["screencapture", "-x", "-t", "png", f"-R{l},{t},{r - l},{b - t}", path],
                       check=True, timeout=10)
        return Path(path).read_bytes()
    finally:
        Path(path).unlink(missing_ok=True)


def capture() -> tuple[bytes, str]:
    (x, y), win = pointer_window()
    with mss.mss() as sct:
        monitor = _monitor_at(sct, x, y)
        if win and (win[2] - win[0]) > 80 and (win[3] - win[1]) > 80:
            rect, what = _clip(win, monitor), "window"
        else:
            rect, what = monitor, "monitor"

        if IS_MAC:
            raw = _capture_mac(rect)
        else:
            l, t, r, b = rect
            shot = sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            raw = buf.getvalue()
    return raw, what


# ---------------------------------------------------------------- send + notify
def notify(cfg: Config, title: str, message: str) -> None:
    if not cfg.notify:
        return
    try:
        if IS_MAC:
            safe = message.replace('"', "'")
            subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{title}"'], timeout=5)
        elif IS_WIN:
            import winsound
            winsound.MessageBeep()
    except Exception:
        log.debug("notify failed", exc_info=True)


def copy_to_clipboard(text: str) -> bool:
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        log.warning("could not copy code to the clipboard", exc_info=True)
        return False


def _headers(cfg: Config) -> dict:
    return {"X-QuizBot-Token": cfg.token} if cfg.token else {}


def send(cfg: Config, image: bytes, capture_ms: int) -> dict:
    data = {"machine": cfg.machine, "source": f"desktop-{platform.system().lower()}", "capture_ms": str(capture_ms)}
    if cfg.session:  # otherwise the server uses the dashboard's active session
        data["session"] = cfg.session
    if cfg.model:    # otherwise the server uses the dashboard's model
        data["model"] = cfg.model
    resp = HTTP.post(f"{cfg.server}/api/capture", files={"image": ("capture.png", image, "image/png")},
                     data=data, headers=_headers(cfg), timeout=90)
    resp.raise_for_status()
    return resp.json()


def report_roundtrip(cfg: Config, row_id: int, roundtrip_ms: int, capture_ms: int) -> None:
    try:
        HTTP.post(f"{cfg.server}/api/answers/{row_id}/client-timing", headers=_headers(cfg), timeout=10,
                  json={"roundtrip_ms": roundtrip_ms, "capture_ms": capture_ms})
    except Exception:
        log.debug("could not report timing", exc_info=True)


_busy = threading.Lock()
_last = 0.0


def on_trigger() -> None:
    global _last
    now = time.monotonic()
    if now - _last < DEBOUNCE_S or not _busy.acquire(blocking=False):
        return
    _last = now
    cfg = load_config()  # picks up quizbot.env edits without a restart
    try:
        started = time.perf_counter()
        image, what = capture()
        capture_ms = int((time.perf_counter() - started) * 1000)
        log.info("captured %s (%d KB) in %dms, sending to %s", what, len(image) // 1024, capture_ms, cfg.server)
        sent = time.perf_counter()
        result = send(cfg, image, capture_ms)
        roundtrip_ms = int((time.perf_counter() - sent) * 1000)
        display = result.get("display", "done")
        if result.get("code") and cfg.copy_code and copy_to_clipboard(result["code"]):
            display = f"Code copied to clipboard. {display}"
        notify(cfg, "QuizBot", display)  # show the answer before any bookkeeping
        t = result.get("timings") or {}
        log.info("answer #%s [%s, %s]: %s | total %dms = capture %d + network %d + server %d (prepare %s, model %s)",
                 result.get("id"), result.get("session"), result.get("model"), result.get("display"),
                 capture_ms + roundtrip_ms, capture_ms, max(0, roundtrip_ms - t.get("server_ms", 0)),
                 t.get("server_ms", 0), t.get("prepare_ms"), t.get("model_ms"))
        if result.get("id"):
            report_roundtrip(cfg, result["id"], roundtrip_ms, capture_ms)
    except Exception as exc:
        log.exception("capture/send failed")
        notify(cfg, "QuizBot error", str(exc)[:120])
    finally:
        _busy.release()


# ---------------------------------------------------------------- hotkey (default Ctrl + `)
# The ` key's virtual key code: VK_OEM_3 on Windows; kVK_ANSI_Grave (50) or the ISO-keyboard
# key left of 1 (10) on macOS. With Ctrl held the reported char is often a control code, so the
# key code is what we match on.
GRAVE_VKS = {192} if IS_WIN else {50, 10}
DEFAULT_HOTKEY = "ctrl+`"


def _modifier_keys() -> dict[str, set]:
    K = keyboard.Key
    names = {
        "ctrl": ("ctrl", "ctrl_l", "ctrl_r"),
        "shift": ("shift", "shift_l", "shift_r"),
        "alt": ("alt", "alt_l", "alt_r", "alt_gr"),
        "cmd": ("cmd", "cmd_l", "cmd_r"),
    }
    return {mod: {getattr(K, n) for n in attrs if hasattr(K, n)} for mod, attrs in names.items()}


def parse_hotkey(spec: str) -> frozenset[str]:
    """'ctrl+`' -> {'ctrl'}. Only the ` key is supported as the final key; modifiers: ctrl, shift, alt, cmd."""
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    aliases = {"control": "ctrl", "option": "alt", "command": "cmd", "win": "cmd", "super": "cmd"}
    if not parts or parts[-1] not in ("`", "grave", "backtick"):
        raise ValueError(f"hotkey must end with ` (got {spec!r})")
    mods = frozenset(aliases.get(p, p) for p in parts[:-1])
    unknown = mods - {"ctrl", "shift", "alt", "cmd"}
    if unknown or not mods:
        raise ValueError(f"hotkey needs at least one of ctrl/shift/alt/cmd (got {spec!r})")
    return mods


class Hotkey:
    """Fires when exactly the configured modifiers are held and ` is pressed."""

    def __init__(self, spec: str) -> None:
        self.spec = spec
        self.required = parse_hotkey(spec)
        self.mod_keys = _modifier_keys()
        self.held: set[str] = set()

    def _mod_of(self, key) -> str | None:
        for name, keys in self.mod_keys.items():
            if any(key == k for k in keys):
                return name
        return None

    @staticmethod
    def _is_grave(key) -> bool:
        return getattr(key, "vk", None) in GRAVE_VKS or getattr(key, "char", None) in ("`", "~")

    def on_press(self, key) -> None:
        mod = self._mod_of(key)
        if mod:
            self.held.add(mod)
        elif self.held == self.required and self._is_grave(key):
            threading.Thread(target=on_trigger, daemon=True).start()

    def on_release(self, key) -> None:
        mod = self._mod_of(key)
        if mod:
            self.held.discard(mod)


def main() -> None:
    if IS_WIN:
        try:  # capture real pixels on high-DPI displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    cfg = load_config()
    try:
        ok = requests.get(f"{cfg.server}/healthz", timeout=5).json()
        log.info("server reachable: %s", ok)
    except Exception as exc:
        log.warning("server %s not reachable yet (%s); will retry on each capture", cfg.server, exc)
    try:
        hotkey = Hotkey(cfg.hotkey)
    except ValueError as exc:
        log.error("%s; falling back to %s", exc, DEFAULT_HOTKEY)
        hotkey = Hotkey(DEFAULT_HOTKEY)
    log.info("QuizBot agent ready on %s (session=%s, model=%s). Hotkey: %s. Config: %s",
             cfg.machine, cfg.session or "from dashboard", cfg.model or "from dashboard", hotkey.spec, ENV_FILE)
    with keyboard.Listener(on_press=hotkey.on_press, on_release=hotkey.on_release) as listener:
        listener.join()


if __name__ == "__main__":
    if "--once" in sys.argv:  # test capture + send without the hotkey
        time.sleep(float(os.getenv("QUIZBOT_ONCE_DELAY", "3")))
        on_trigger()
    else:
        main()
