"""StreamGremlin Windows Companion Agent (StreamGremlin.exe).

- Windows input simulation and transparent desktop overlay.
- Audio playback and hardware panic key (3x ESC).
- System tray integration with automatic updates.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
import datetime
import json
import logging
import math
import os
import platform
import random
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

import httpx
import websockets

try:
    from pynput import keyboard, mouse
    HAS_PYNPUT = True
except Exception:
    HAS_PYNPUT = False

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False

try:
    import winsound
    HAS_WINSOUND = True
except Exception:
    HAS_WINSOUND = False

try:
    import tkinter as tk
    HAS_TKINTER = True
except Exception:
    HAS_TKINTER = False

# Primary log file directly next to StreamGremlin.exe (streamgremlin.log), fallback to %LOCALAPPDATA%
PRIMARY_LOG_FILE: Optional[Path] = None
APPDATA_LOG_FILE: Optional[Path] = None

try:
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    local_log_path = exe_dir / "streamgremlin.log"
    with open(local_log_path, "a", encoding="utf-8") as _f:
        _f.write("")
    PRIMARY_LOG_FILE = local_log_path
except Exception:
    PRIMARY_LOG_FILE = None

try:
    appdata_dir = Path(os.environ.get("LOCALAPPDATA", ".")) / "StreamGremlin"
    appdata_dir.mkdir(parents=True, exist_ok=True)
    APPDATA_LOG_FILE = appdata_dir / "streamgremlin.log"
except Exception:
    APPDATA_LOG_FILE = None

if PRIMARY_LOG_FILE is None:
    PRIMARY_LOG_FILE = APPDATA_LOG_FILE

log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)

# Add file handler for PRIMARY_LOG_FILE (streamgremlin.log)
if PRIMARY_LOG_FILE:
    try:
        fh_primary = logging.FileHandler(str(PRIMARY_LOG_FILE), encoding="utf-8")
        fh_primary.setLevel(logging.DEBUG)
        fh_primary.setFormatter(log_formatter)
        root_logger.addHandler(fh_primary)
    except Exception:
        pass

# Also write to AppData streamgremlin.log & agent.log for backwards compatibility if different
if APPDATA_LOG_FILE and APPDATA_LOG_FILE != PRIMARY_LOG_FILE:
    try:
        fh_appdata = logging.FileHandler(str(APPDATA_LOG_FILE), encoding="utf-8")
        fh_appdata.setLevel(logging.DEBUG)
        fh_appdata.setFormatter(log_formatter)
        root_logger.addHandler(fh_appdata)
    except Exception:
        pass

# Add console handler
if sys.stdout is not None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(log_formatter)
        root_logger.addHandler(console_handler)
    except Exception:
        pass

logger = logging.getLogger("streamgremlin.agent")


def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    """Catch any uncaught exception and ensure full traceback is written to streamgremlin.log."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("FATAL UNCAUGHT EXCEPTION:", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = handle_uncaught_exception
if hasattr(threading, "excepthook"):
    threading.excepthook = lambda args: handle_uncaught_exception(args.exc_type, args.exc_value, args.exc_traceback)


def open_log_file() -> None:
    """Open the main log file in the default text viewer (e.g. Notepad)."""
    for p in [PRIMARY_LOG_FILE, APPDATA_LOG_FILE]:
        if p and p.exists():
            try:
                os.startfile(str(p))
                return
            except Exception as e:
                logger.error("Could not open log file %s: %s", p, e)

AGENT_VERSION = "23.01.01"


def is_newer_version(remote: str, current: str) -> bool:
    """Return True if remote version is strictly greater than current version."""
    def _parse(v: str) -> tuple[int, ...]:
        parts = []
        for x in str(v).lstrip("v").split("."):
            num = ""
            for ch in x:
                if ch.isdigit():
                    num += ch
                else:
                    break
            parts.append(int(num) if num else 0)
        return tuple(parts)

    try:
        return _parse(remote) > _parse(current)
    except Exception:
        return False

# Win32 Constants
SPI_GETMOUSESPEED = 0x0070
SPI_SETMOUSESPEED = 0x0071
SPIF_SENDCHANGE = 0x0002

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOPMOST = 0x8
WS_EX_TOOLWINDOW = 0x80
WS_EX_NOACTIVATE = 0x08000000
LWA_COLORKEY = 0x1


def is_admin() -> bool:
    """Check if agent process has elevated Administrator privileges (UIPI bypass)."""
    if sys.platform != "win32":
        return False
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


# ==============================================================================
# Native Win32 Helpers (Mouse Speed, Gamma Ramp, Magnification, Display Flip)
# ==============================================================================
def get_win32_mouse_speed() -> int:
    """Read current Windows userland mouse speed (1-20)."""
    if sys.platform != "win32":
        return 10
    speed = ctypes.c_int(10)
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETMOUSESPEED, 0, ctypes.byref(speed), 0)
    return int(speed.value)


def set_win32_mouse_speed(speed: int) -> None:
    """Set Windows userland mouse speed (1-20) via standard User32 API."""
    if sys.platform != "win32":
        return
    clamped = max(1, min(20, int(speed)))
    ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETMOUSESPEED,
        0,
        ctypes.c_void_p(clamped),
        SPIF_SENDCHANGE,
    )


def set_display_gamma_ramp(mode: str = "normal") -> None:
    """Hardware Gamma manipulation (Flashbang / Blackout / Normal)."""
    if sys.platform != "win32":
        return
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        ramp = (ctypes.c_ushort * 768)()
        for i in range(256):
            if mode == "flashbang":
                val = 65535
            elif mode == "blackout":
                val = 0
            else:
                val = int((i / 255.0) * 65535)
            ramp[i] = val
            ramp[256 + i] = val
            ramp[512 + i] = val
        ctypes.windll.gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
        ctypes.windll.user32.ReleaseDC(0, hdc)
    except Exception as exc:
        logger.debug("Gamma ramp error: %s", exc)


def set_win32_color_inversion(enable: bool) -> None:
    """Hardware Color Inversion via Windows Magnification API."""
    if sys.platform != "win32":
        return
    try:
        mag = ctypes.windll.magnification
        if enable:
            mag.MagInitialize()
            invert_matrix = (ctypes.c_float * 25)(
                -1.0, 0.0, 0.0, 0.0, 0.0,
                0.0, -1.0, 0.0, 0.0, 0.0,
                0.0, 0.0, -1.0, 0.0, 0.0,
                0.0, 0.0, 0.0, 1.0, 0.0,
                1.0, 1.0, 1.0, 0.0, 1.0,
            )
            mag.MagSetFullscreenColorEffect(ctypes.byref(invert_matrix))
        else:
            id_matrix = (ctypes.c_float * 25)(
                1.0, 0.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 0.0, 1.0,
            )
            mag.MagSetFullscreenColorEffect(ctypes.byref(id_matrix))
            mag.MagUninitialize()
    except Exception as exc:
        logger.debug("Magnification API error: %s", exc)


# ==============================================================================
# Native Hardware Scan Code Injection (Anti-Cheat & DirectInput Safe)
# ==============================================================================
def send_hardware_scancode(key_target: Any, is_press: bool) -> None:
    """Send DirectInput Hardware Scancode via Win32 SendInput.

    Sends keyboard events using DirectInput hardware scancodes.
    """
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        vk = 0
        scan = 0
        is_extended = False

        if isinstance(key_target, str):
            ch = key_target.lower()
            if ch == "space":
                vk, scan = 0x20, 0x39
            elif ch in ("ctrl", "ctrl_l", "control"):
                vk, scan = 0x11, 0x1D
            elif ch == "esc":
                vk, scan = 0x1B, 0x01
            elif len(ch) == 1:
                try:
                    res_vk = user32.VkKeyScanW(wintypes.WCHAR(ch))
                except Exception:
                    res_vk = user32.VkKeyScanW(ord(ch))
                vk = res_vk & 0xFF if res_vk != -1 else 0
                scan = user32.MapVirtualKeyW(vk, 0)
        elif HAS_PYNPUT and key_target == keyboard.Key.space:
            vk, scan = 0x20, 0x39
        elif HAS_PYNPUT and key_target in (keyboard.Key.ctrl, keyboard.Key.ctrl_l):
            vk, scan = 0x11, 0x1D
        elif HAS_PYNPUT and key_target == keyboard.Key.esc:
            vk, scan = 0x1B, 0x01
        elif HAS_PYNPUT and hasattr(key_target, "char") and key_target.char:
            ch = key_target.char.lower()
            try:
                res_vk = user32.VkKeyScanW(wintypes.WCHAR(ch))
            except Exception:
                res_vk = user32.VkKeyScanW(ord(ch))
            vk = res_vk & 0xFF if res_vk != -1 else 0
            scan = user32.MapVirtualKeyW(vk, 0)

        if not scan:
            return

        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_SCANCODE = 0x0008
        KEYEVENTF_EXTENDEDKEY = 0x0001

        flags = KEYEVENTF_SCANCODE
        if not is_press:
            flags |= KEYEVENTF_KEYUP
        if is_extended:
            flags |= KEYEVENTF_EXTENDEDKEY

        if HAS_PYNPUT:
            try:
                from pynput._util.win32 import INPUT, KEYBDINPUT, INPUT_union, SendInput
                ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
                inp = INPUT(type=INPUT.KEYBOARD, value=INPUT_union(ki=ki))
                SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            except Exception:
                pass
    except Exception as exc:
        logger.debug("DirectInput scancode send failed: %s", exc)


# ==============================================================================
# Input Chaos Controller (Keyboard & Mouse Effects)
# ==============================================================================
class InputChaosController:
    """Manages overlapping userland keyboard and mouse troll effects safely."""

    def __init__(self, on_panic_callback=None) -> None:
        self.on_panic_callback = on_panic_callback
        self._lock = threading.RLock()
        self.active_trolls: Dict[str, float] = {}   # action_key -> expiry timestamp
        self.disabled_keys: Dict[str, float] = {}   # key_name -> expiry timestamp
        self.jammed_keys: Dict[str, float] = {}     # key_name -> expiry timestamp
        self.invert_pairs: List[Tuple[str, str]] = [("w", "s"), ("a", "d")]
        self.mouse_invert_mode: str = "y"           # "y", "x", or "both"
        self.original_mouse_speed: int = get_win32_mouse_speed()
        self.mouse_speed_overridden: bool = False
        self.color_inverted: bool = False

        self.esc_timestamps: List[float] = []
        self._synthetic_guard = False
        self._last_mouse_pos: Optional[Tuple[int, int]] = None

        self.kb_controller = keyboard.Controller() if HAS_PYNPUT else None
        self.mouse_controller = mouse.Controller() if HAS_PYNPUT else None
        self._kb_listener: Optional[keyboard.Listener] = None
        self._mouse_listener: Optional[mouse.Listener] = None
        self._running = True

        self._worker_thread = threading.Thread(target=self._background_tick_loop, daemon=True)
        self._worker_thread.start()

    def start_listeners(self) -> None:
        if not HAS_PYNPUT:
            logger.warning("pynput not available; running in dry-run input simulation mode.")
            return
        self._kb_listener = keyboard.Listener(on_press=self._on_key_press)
        self._kb_listener.daemon = True
        self._kb_listener.start()

        self._mouse_listener = mouse.Listener(on_move=self._on_mouse_move)
        self._mouse_listener.daemon = True
        self._mouse_listener.start()
        logger.info("Keyboard and mouse listeners initialized.")

    def stop(self) -> None:
        self._running = False
        self.panic_reset(notify_hub=False)
        if self._kb_listener:
            self._kb_listener.stop()
        if self._mouse_listener:
            self._mouse_listener.stop()

    def _press_key_safe(self, key_target: Any) -> None:
        if not self.kb_controller:
            return
        try:
            self._synthetic_guard = True
            self.kb_controller.press(key_target)
            send_hardware_scancode(key_target, is_press=True)
        except Exception as exc:
            logger.debug("Key press error: %s", exc)
        finally:
            self._synthetic_guard = False

    def _release_key_safe(self, key_target: Any) -> None:
        if not self.kb_controller:
            return
        try:
            self._synthetic_guard = True
            self.kb_controller.release(key_target)
            send_hardware_scancode(key_target, is_press=False)
        except Exception as exc:
            logger.debug("Key release error: %s", exc)
        finally:
            self._synthetic_guard = False

    def _tap_key_safe(self, key_target: Any, hold_sec: float = 0.05) -> None:
        if not self.kb_controller:
            return
        try:
            self._press_key_safe(key_target)
            time.sleep(hold_sec)
            self._release_key_safe(key_target)
        except Exception as exc:
            logger.debug("Key tap error: %s", exc)

    def _on_key_press(self, key: Any) -> None:
        """Detect ESC 3x hardware panic and apply active WASD/key transformations."""
        if self._synthetic_guard:
            return
        now = time.time()

        # Check for ESC 3x hardware panic within 1.5s
        if HAS_PYNPUT and key == keyboard.Key.esc:
            self.esc_timestamps = [t for t in self.esc_timestamps if now - t <= 1.5]
            self.esc_timestamps.append(now)
            if len(self.esc_timestamps) >= 3:
                self.esc_timestamps.clear()
                logger.warning("HARDWARE PANIC (ESC 3x) DETECTED! Stopping all input trolls immediately.")
                self.panic_reset(notify_hub=True)
                return

        # Extract character if standard key
        char = getattr(key, "char", None)
        if not char:
            return
        ch = char.lower()

        with self._lock:
            # If invert_keys / invert_ws / invert_ad is active, counter-tap opposite key asynchronously
            # (Running in background daemon thread ensures WH_KEYBOARD_LL hook callback returns in <0.1ms!)
            if any(self.active_trolls.get(k, 0) > now for k in ("invert_keys", "invert_ws", "invert_ad")):
                swap_map = {}
                for k1, k2 in self.invert_pairs:
                    swap_map[k1] = k2
                    swap_map[k2] = k1
                if ch in swap_map and self.kb_controller:
                    opposite = swap_map[ch]
                    threading.Thread(target=self._tap_key_safe, args=(opposite, 0.06), daemon=True).start()

            # If disable_key is active for this key, release it immediately
            if self.disabled_keys.get(ch, 0) > now and self.kb_controller:
                self._release_key_safe(ch)

    def _on_mouse_move(self, x: int, y: int) -> None:
        """Apply genuine responsive axis inversion when mouse_invert is active."""
        if self._synthetic_guard or not self.mouse_controller:
            return

        now = time.time()
        with self._lock:
            is_inverted = self.active_trolls.get("mouse_invert", 0) > now
            invert_mode = self.mouse_invert_mode

        if not self._last_mouse_pos:
            self._last_mouse_pos = (x, y)
            return

        last_x, last_y = self._last_mouse_pos
        dx = x - last_x
        dy = y - last_y
        self._last_mouse_pos = (x, y)

        if not is_inverted or (dx == 0 and dy == 0):
            return

        # Counter-move to invert hardware axis
        try:
            self._synthetic_guard = True
            if invert_mode == "y":
                self.mouse_controller.move(0, -2 * dy)
            elif invert_mode == "x":
                self.mouse_controller.move(-2 * dx, 0)
            else:  # "both"
                self.mouse_controller.move(-2 * dx, -2 * dy)
        except Exception:
            pass
        finally:
            self._synthetic_guard = False

    def trigger_troll(self, action: str, duration: float, params: Dict[str, Any]) -> None:
        """Activate or extend an input troll effect."""
        now = time.time()
        expiry = now + max(0.5, float(duration or 5.0))

        with self._lock:
            logger.info("Activating input troll: %s (duration=%.1fs, params=%s)", action, duration, params)

            if action == "jam_key":
                key_name = str(params.get("key", "w")).lower()[:1] or "w"
                self.jammed_keys[key_name] = expiry
                self._press_key_safe(key_name)

            elif action == "disable_key":
                key_name = str(params.get("key", "r")).lower()[:1] or "r"
                self.disabled_keys[key_name] = expiry

            elif action == "invert_ws":
                self.invert_pairs = [("w", "s")]
                self.active_trolls["invert_ws"] = expiry

            elif action == "invert_ad":
                self.invert_pairs = [("a", "d")]
                self.active_trolls["invert_ad"] = expiry

            elif action == "invert_keys":
                raw_pairs = params.get("pairs") or [["w", "s"], ["a", "d"]]
                self.invert_pairs = [(str(p[0]).lower(), str(p[1]).lower()) for p in raw_pairs if len(p) >= 2]
                self.active_trolls["invert_keys"] = expiry

            elif action == "mouse_invert":
                self.mouse_invert_mode = str(params.get("mode", "y")).lower()
                self.active_trolls["mouse_invert"] = expiry

            elif action == "mouse_sensitivity":
                if not self.mouse_speed_overridden:
                    self.original_mouse_speed = get_win32_mouse_speed()
                    self.mouse_speed_overridden = True
                mode = str(params.get("mode", "snail")).lower()
                multiplier = float(params.get("multiplier", 0.2 if mode == "snail" else 2.5))
                target_speed = 2 if multiplier < 1.0 or mode == "snail" else 20
                set_win32_mouse_speed(target_speed)
                self.active_trolls["mouse_sensitivity"] = expiry

            elif action == "invert_colors":
                set_win32_color_inversion(True)
                self.color_inverted = True
                self.active_trolls["invert_colors"] = expiry

            elif action == "flashbang":
                set_display_gamma_ramp("flashbang")
                self.active_trolls["gamma_flash"] = now + 1.2

            elif action == "blackout":
                set_display_gamma_ramp("blackout")
                self.active_trolls["gamma_blackout"] = expiry

            else:
                self.active_trolls[action] = expiry

    def panic_reset(self, notify_hub: bool = True) -> None:
        """Immediately cancel all active trolls and restore all held keys, mouse speed & gamma."""
        with self._lock:
            jammed = list(self.jammed_keys.keys())
            self.active_trolls.clear()
            self.disabled_keys.clear()
            self.jammed_keys.clear()

            if self.kb_controller:
                for k in jammed:
                    self._release_key_safe(k)
                for k in ("w", "a", "s", "d", "r", "c", "e", "f", "1", "2", "3", "4"):
                    try:
                        self._release_key_safe(k)
                    except Exception:
                        pass
                if HAS_PYNPUT:
                    self._release_key_safe(keyboard.Key.space)
                    self._release_key_safe(keyboard.Key.ctrl_l)

            if self.mouse_speed_overridden:
                set_win32_mouse_speed(self.original_mouse_speed)
                self.mouse_speed_overridden = False

            if self.color_inverted:
                set_win32_color_inversion(False)
                self.color_inverted = False

            set_display_gamma_ramp("normal")

        if notify_hub and self.on_panic_callback:
            try:
                self.on_panic_callback()
            except Exception:
                pass

    def _background_tick_loop(self) -> None:
        """Periodic worker executing repetitive trolls (Space hop, R tap, slot cycling, sway drift)."""
        last_jump = 0.0
        last_reload = 0.0
        last_roulette = 0.0
        last_crouch = 0.0
        last_shake = 0.0

        while self._running:
            now = time.time()
            with self._lock:
                # 1. Clean up expired jammed keys
                expired_jams = [k for k, exp in self.jammed_keys.items() if exp <= now]
                for k in expired_jams:
                    del self.jammed_keys[k]
                    self._release_key_safe(k)

                # 2. Clean up expired disabled keys
                expired_dis = [k for k, exp in self.disabled_keys.items() if exp <= now]
                for k in expired_dis:
                    del self.disabled_keys[k]

                # 3. Clean up expired active_trolls
                expired_trolls = [act for act, exp in self.active_trolls.items() if exp <= now]
                for act in expired_trolls:
                    del self.active_trolls[act]
                    if act == "mouse_sensitivity" and self.mouse_speed_overridden:
                        set_win32_mouse_speed(self.original_mouse_speed)
                        self.mouse_speed_overridden = False
                    if act == "invert_colors" and self.color_inverted:
                        set_win32_color_inversion(False)
                        self.color_inverted = False
                    if act in ("gamma_flash", "gamma_blackout"):
                        set_display_gamma_ramp("normal")

                active_snapshot = dict(self.active_trolls)
                jammed_snapshot = dict(self.jammed_keys)

            # Re-assert jammed keys periodically
            for k in jammed_snapshot:
                self._press_key_safe(k)

            # Execute periodic trolls outside lock
            if "jump_frenzy" in active_snapshot and now - last_jump >= random.uniform(0.75, 1.25):
                last_jump = now
                if HAS_PYNPUT:
                    self._tap_key_safe(keyboard.Key.space, hold_sec=0.06)

            if "reload_addiction" in active_snapshot and now - last_reload >= random.uniform(1.3, 2.4):
                last_reload = now
                self._tap_key_safe("r", hold_sec=0.05)

            if "weapon_roulette" in active_snapshot and now - last_roulette >= random.uniform(0.9, 1.7):
                last_roulette = now
                slot_key = random.choice(["1", "2", "3", "4"])
                self._tap_key_safe(slot_key, hold_sec=0.05)

            if "crouch_spam" in active_snapshot and now - last_crouch >= 0.28:
                last_crouch = now
                if HAS_PYNPUT:
                    self._tap_key_safe(keyboard.Key.ctrl_l, hold_sec=0.08)

            # In-game Camera Shake
            if "screen_shake" in active_snapshot and now - last_shake >= 0.04 and self.mouse_controller:
                last_shake = now
                try:
                    self._synthetic_guard = True
                    self.mouse_controller.move(random.choice([-8, 8]), random.choice([-8, 8]))
                finally:
                    self._synthetic_guard = False

            # In-game Drunk Sway Drift
            if "drunk_mode" in active_snapshot and self.mouse_controller:
                drift_x = int(5.0 * math.sin(now * 2.2))
                try:
                    self._synthetic_guard = True
                    self.mouse_controller.move(drift_x, 0)
                finally:
                    self._synthetic_guard = False

            time.sleep(0.04)


# ==============================================================================
# Transparent Click-Through Desktop Visual Overlay (Dual-Mode on Gaming-PC)
# ==============================================================================
class DesktopVisualOverlay:
    """Renders visual chaos (Flashbang, Blackout, Tunnel, Glitch, BSOD, Emotes) directly over the game."""

    def __init__(self) -> None:
        self.root: Optional[tk.Tk] = None
        self.canvas: Optional[tk.Canvas] = None
        self._thread: Optional[threading.Thread] = None
        self.active_overlay_eff: Optional[str] = None
        self.overlay_expiry: float = 0.0
        self.bouncing_emotes: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        if not HAS_TKINTER:
            return
        self._thread = threading.Thread(target=self._run_ui, daemon=True)
        self._thread.start()

    def _run_ui(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.title("StreamGremlin Desktop Overlay")
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self.root.geometry(f"{sw}x{sh}+0+0")
            self.root.overrideredirect(True)
            self.root.wm_attributes("-topmost", True)

            # Set chroma-key transparency (#000001 is glass-transparent)
            trans_color = "#000001"
            self.root.wm_attributes("-transparentcolor", trans_color)
            self.root.config(bg=trans_color)

            self.canvas = tk.Canvas(self.root, width=sw, height=sh, bg=trans_color, highlightthickness=0)
            self.canvas.pack(fill="both", expand=True)

            # Apply Win32 WS_EX_TRANSPARENT, WS_EX_NOACTIVATE & WS_EX_TOOLWINDOW so mouse clicks pass through and game never loses focus!
            if sys.platform == "win32":
                self.root.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(
                    hwnd,
                    GWL_EXSTYLE,
                    style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
                )

            self._animate_loop()
            self.root.mainloop()
        except Exception as exc:
            logger.debug("Desktop overlay initialization skipped: %s", exc)

    def trigger_visual(self, effect: str, duration_ms: int, payload: Dict[str, Any]) -> None:
        with self._lock:
            self.active_overlay_eff = effect
            self.overlay_expiry = time.time() + (duration_ms / 1000.0)

    def add_emotes(self, count: int = 5, is_hype: bool = False) -> None:
        with self._lock:
            now = time.time()
            emojis = ["🔥", "👑", "💀", "😂", "🚀", "💣", "💜", "⚡", "🍀", "👾"]
            for _ in range(count):
                self.bouncing_emotes.append({
                    "x": random.randint(100, 1800),
                    "y": -random.randint(20, 200),
                    "vx": random.uniform(-4, 4),
                    "vy": random.uniform(2, 7),
                    "emoji": random.choice(emojis),
                    "born": now,
                })

    def _animate_loop(self) -> None:
        if not self.root or not self.canvas:
            return

        now = time.time()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        with self._lock:
            eff = self.active_overlay_eff
            expiry = self.overlay_expiry
            emotes = list(self.bouncing_emotes)

        self.canvas.delete("all")

        # 1. Render Active Screen Visuals
        if eff and expiry > now:
            rem_sec = expiry - now
            if eff == "flashbang":
                alpha = min(1.0, rem_sec / 1.5)
                # Strobe flash on canvas
                self.canvas.create_rectangle(0, 0, sw, sh, fill="#ffffff", outline="")

            elif eff == "blackout":
                self.canvas.create_rectangle(0, 0, sw, sh, fill="#000000", outline="")

            elif eff == "tunnel_vision":
                # Dark vignette leaving central circular hole
                cx, cy, r = sw // 2, sh // 2, 160
                self.canvas.create_rectangle(0, 0, sw, cy - r, fill="#090a0f", outline="")
                self.canvas.create_rectangle(0, cy + r, sw, sh, fill="#090a0f", outline="")
                self.canvas.create_rectangle(0, cy - r, cx - r, cy + r, fill="#090a0f", outline="")
                self.canvas.create_rectangle(cx + r, cy - r, sw, cy + r, fill="#090a0f", outline="")

            elif eff == "screen_glitch":
                for _ in range(12):
                    gy = random.randint(0, sh)
                    gh = random.randint(10, 45)
                    col = random.choice(["#22c55e", "#a855f7", "#ef4444", "#38bdf8"])
                    self.canvas.create_rectangle(0, gy, sw, gy + gh, fill=col, outline="")

            elif eff == "fake_bsod":
                self.canvas.create_rectangle(0, 0, sw, sh, fill="#0078d7", outline="")
                self.canvas.create_text(sw // 4, sh // 3, text=":(", fill="#ffffff", font=("Segoe UI", 80), anchor="w")
                msg = "Auf dem PC ist ein Problem aufgetreten.\nEr muss neu gestartet werden.\n\nStillstandcode: STREAM_GREMLIN_OVERLOAD"
                self.canvas.create_text(sw // 4, sh // 2, text=msg, fill="#ffffff", font=("Segoe UI", 24), anchor="w")

            elif eff == "matrix_rain":
                chars = "0101XYZSTREAMGREMLINｦｱｳｴｵｶｷｹｺ"
                for col in range(0, sw, 35):
                    ry = int((now * 400 + col * 17) % sh)
                    self.canvas.create_text(col, ry, text=random.choice(chars), fill="#22c55e", font=("Consolas", 18))

            elif eff == "gif_jumpscare":
                self.canvas.create_rectangle(0, 0, sw, sh, fill="#000000", outline="")
                self.canvas.create_text(sw // 2, sh // 2, text="👻 JUMPSCARE! 💀", fill="#ef4444", font=("Arial Black", 56), anchor="center")

        elif eff and expiry <= now:
            with self._lock:
                self.active_overlay_eff = None

        # 2. Render 2D Bouncing Emotes
        surviving_emotes = []
        for em in emotes:
            if now - em["born"] > 10.0:
                continue
            em["vy"] += 0.42
            em["x"] += em["vx"]
            em["y"] += em["vy"]
            if em["y"] > sh - 40:
                em["y"] = sh - 40
                em["vy"] = -em["vy"] * 0.72
                em["vx"] *= 0.94
            self.canvas.create_text(em["x"], em["y"], text=em["emoji"], font=("Segoe UI Emoji", 26))
            surviving_emotes.append(em)

        with self._lock:
            self.bouncing_emotes = surviving_emotes[:50]

        self.root.after(33, self._animate_loop)


# ==============================================================================
# Main Companion Agent
# ==============================================================================
class StreamGremlinAgent:
    """Main Companion Agent coordinating WebSocket, Local Audio, Input Chaos, Overlay & Auto-Update."""

    def __init__(self, hub_url: str, token: str, auto_update: bool = True) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.auto_update = auto_update
        self.connected = False
        self.update_available: Optional[str] = None
        self.download_url: str = "/api/agent/download"
        self._updating: bool = False
        self._stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[Any] = None
        self.tray_icon: Optional[Any] = None

        self.chaos = InputChaosController(on_panic_callback=self._send_panic_from_thread)
        self.overlay = DesktopVisualOverlay()
        self.sound_cache_dir = Path(os.environ.get("LOCALAPPDATA", ".")) / "StreamGremlin" / "sounds"
        self.sound_cache_dir.mkdir(parents=True, exist_ok=True)

    def _ws_url(self) -> str:
        base = self.hub_url
        if base.startswith("https://"):
            ws_base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            ws_base = "ws://" + base[len("http://"):]
        else:
            ws_base = base
        if ws_base.endswith("/ws/agent"):
            return ws_base
        return f"{ws_base}/ws/agent"

    def _send_panic_from_thread(self) -> None:
        if self._loop and self._ws:
            asyncio.run_coroutine_threadsafe(self._send_panic_async(), self._loop)

    async def _send_panic_async(self) -> None:
        if self._ws:
            try:
                await self._ws.send(json.dumps({"action": "panic_pressed", "source": "agent_esc_3x"}))
            except Exception:
                pass

    def play_local_sound(self, filename: str, sound_url: str) -> None:
        """Download sound from Hub if missing and play asynchronously through streamer's headset."""
        if sys.platform != "win32":
            return
        local_path = self.sound_cache_dir / Path(filename).name

        def _play_worker() -> None:
            try:
                if not local_path.exists():
                    full_url = f"{self.hub_url}{sound_url}" if sound_url.startswith("/") else sound_url
                    urllib.request.urlretrieve(full_url, str(local_path))

                ext = local_path.suffix.lower()
                if ext == ".wav" and HAS_WINSOUND:
                    winsound.PlaySound(str(local_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
                else:
                    alias = f"sg_snd_{int(time.time() * 1000) % 100000}"
                    ctypes.windll.winmm.mciSendStringW(f'open "{local_path}" type mpegvideo alias {alias}', None, 0, None)
                    ctypes.windll.winmm.mciSendStringW(f'play {alias} wait', None, 0, None)
                    ctypes.windll.winmm.mciSendStringW(f'close {alias}', None, 0, None)
            except Exception as exc:
                logger.debug("Local sound play error: %s", exc)

        threading.Thread(target=_play_worker, daemon=True).start()

    def perform_self_update(self) -> bool:
        """Download latest StreamGremlin.exe and execute atomic self-replacement via detached helper."""
        if self._updating:
            return False
        self._updating = True

        is_frozen = getattr(sys, "frozen", False)
        if not is_frozen:
            logger.info("Running from source (python script). Auto-updater only replaces compiled executables.")
            self._updating = False
            return False

        target_exe = Path(sys.executable).resolve()
        tmp_exe = target_exe.with_name(f"{target_exe.stem}_new.exe")
        bat_file = Path(tempfile.gettempdir()) / f"sg_update_{os.getpid()}.bat"

        logger.info("Starting self-update: downloading latest binary from %s...", self.hub_url)
        if HAS_TRAY and self.tray_icon:
            try:
                self.tray_icon.title = "StreamGremlin (Update wird heruntergeladen...)"
            except Exception:
                pass

        dl_url = f"{self.hub_url}{self.download_url}" if self.download_url.startswith("/") else self.download_url
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                with client.stream("GET", dl_url) as resp:
                    if resp.status_code != 200:
                        logger.error("Download failed with HTTP %d", resp.status_code)
                        self._updating = False
                        return False
                    with open(tmp_exe, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            f.write(chunk)
        except Exception as exc:
            logger.error("Download error during update: %s", exc)
            tmp_exe.unlink(missing_ok=True)
            self._updating = False
            return False

        # Validate downloaded file
        if not tmp_exe.exists() or tmp_exe.stat().st_size < 1_000_000:
            logger.error("Downloaded update file is too small or missing.")
            tmp_exe.unlink(missing_ok=True)
            self._updating = False
            return False

        with open(tmp_exe, "rb") as f:
            if f.read(2) != b"MZ":
                logger.error("Downloaded file is not a valid PE executable.")
                tmp_exe.unlink(missing_ok=True)
                self._updating = False
                return False

        logger.info("Update binary verified (%d bytes). Spawning detached updater batch process...", tmp_exe.stat().st_size)

        # Generate self-replacing batch script with retry loop and clean PyInstaller environment
        target_dir = target_exe.parent
        updater_log = target_dir / "updater.log"
        bat_script = f"""@echo off
set _MEIPASS2=
set _MEIPASS=
echo [%date% %time%] StreamGremlin updater starting >> "{updater_log}"
timeout /t 2 /nobreak >nul
:retry
echo [%date% %time%] Replacing "{target_exe}" with "{tmp_exe}" >> "{updater_log}"
move /y "{tmp_exe}" "{target_exe}" >> "{updater_log}" 2>&1
if errorlevel 1 (
    echo [%date% %time%] Target locked or error, retrying in 1s... >> "{updater_log}"
    timeout /t 1 /nobreak >nul
    goto retry
)
echo [%date% %time%] Starting new executable from "{target_dir}"... >> "{updater_log}"
start "" /d "{target_dir}" "{target_exe}"
echo [%date% %time%] Updater finished successfully. >> "{updater_log}"
del "%~f0"
"""
        bat_file.write_text(bat_script, encoding="utf-8")

        clean_env = os.environ.copy()
        clean_env.pop("_MEIPASS2", None)
        clean_env.pop("_MEIPASS", None)

        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            ["cmd.exe", "/c", str(bat_file)],
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            close_fds=True,
            env=clean_env,
        )

        # Gracefully shut down current instance
        self.chaos.stop()
        if HAS_TRAY and self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        os._exit(0)

    def _notify_update_available(self, version: str) -> None:
        if not HAS_TRAY or not self.tray_icon:
            return
        try:
            self.tray_icon.menu = self._build_tray_menu()
            self.tray_icon.notify(
                f"Version v{version} steht bereit! Klicke im Menü zum Aktualisieren.",
                "StreamGremlin Update verfügbar",
            )
        except Exception as exc:
            logger.debug("Tray update notification failed: %s", exc)

    def _build_tray_menu(self) -> Any:
        if not HAS_TRAY:
            return None

        def on_panic(icon, item):
            self.chaos.panic_reset(notify_hub=True)

        def on_exit(icon, item):
            self._stop_event.set()
            self.chaos.stop()
            icon.stop()

        items = [
            pystray.MenuItem(f"StreamGremlin v{AGENT_VERSION}", None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]
        if self.update_available:
            def on_update_click(icon, item):
                threading.Thread(target=self.perform_self_update, daemon=True).start()
            items.append(pystray.MenuItem(f"⚡ Update auf v{self.update_available} installieren", on_update_click))
            items.append(pystray.Menu.SEPARATOR)

        items.append(pystray.MenuItem("PANIC! Reset Inputs (ESC 3x)", on_panic))
        items.append(pystray.MenuItem("📄 Logs anzeigen (streamgremlin.log)", lambda icon, item: open_log_file()))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Beenden", on_exit))
        return pystray.Menu(*items)

    async def check_for_updates(self) -> None:
        """Query GET /api/agent/version on startup and periodically."""
        url = f"{self.hub_url}/api/agent/version"
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    remote_ver = str(data.get("latest_version") or data.get("version") or AGENT_VERSION)
                    dl_url = str(data.get("download_url") or "/api/agent/download")
                    if is_newer_version(remote_ver, AGENT_VERSION):
                        self.update_available = remote_ver
                        self.download_url = dl_url
                        logger.warning(
                            "New StreamGremlin Agent version available: v%s (current: v%s).",
                            remote_ver,
                            AGENT_VERSION,
                        )
                        self._notify_update_available(remote_ver)
                        if self.auto_update:
                            logger.info("Auto-update enabled: triggering automatic background update in 2s...")
                            await asyncio.sleep(2.0)
                            threading.Thread(target=self.perform_self_update, daemon=True).start()
        except Exception as exc:
            logger.debug("Update check skipped/failed: %s", exc)

    async def ws_loop(self) -> None:
        """Persistent WebSocket loop with silent auto-reconnect."""
        import socket
        self._loop = asyncio.get_running_loop()
        await self.check_for_updates()

        ws_url = self._ws_url()
        hostname = socket.gethostname()
        while not self._stop_event.is_set():
            try:
                logger.info("Connecting to StreamGremlin Hub: %s", self.hub_url)
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(
                        json.dumps({
                            "action": "auth",
                            "token": self.token,
                            "version": AGENT_VERSION,
                            "hostname": hostname,
                        })
                    )
                    auth_raw = await ws.recv()
                    auth_resp = json.loads(auth_raw)
                    if auth_resp.get("status") != "authenticated":
                        logger.error("Agent authentication rejected: %s", auth_resp.get("message"))
                        await asyncio.sleep(8)
                        continue

                    if auth_resp.get("update_available"):
                        remote_ver = str(auth_resp.get("latest_version") or AGENT_VERSION)
                        if is_newer_version(remote_ver, AGENT_VERSION):
                            self.update_available = remote_ver
                            self.download_url = str(auth_resp.get("download_url") or "/api/agent/download")
                            logger.warning("Hub reports newer Agent version v%s available.", remote_ver)
                            self._notify_update_available(remote_ver)
                            if self.auto_update:
                                logger.info("Auto-update enabled: triggering automatic background update in 2s...")
                                threading.Thread(target=self.perform_self_update, daemon=True).start()

                    self._ws = ws
                    self._set_connected(True)
                    logger.info("Connected & authenticated to StreamGremlin Hub!")

                    async for raw_msg in ws:
                        if self._stop_event.is_set():
                            break
                        try:
                            msg = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            continue
                        self._handle_hub_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._set_connected(False)
                self._ws = None
                logger.debug("Hub connection lost (%s). Reconnecting silently in 4s...", exc)
                await asyncio.sleep(4)

        self._set_connected(False)

    def _handle_hub_message(self, msg: Dict[str, Any]) -> None:
        action = msg.get("action") or msg.get("type")

        if action == "panic":
            logger.warning("Hub triggered PANIC! Resetting all active inputs & overlays.")
            self.chaos.panic_reset(notify_hub=False)

        elif action == "update_agent":
            logger.info("Received update_agent command from Hub.")
            threading.Thread(target=self.perform_self_update, daemon=True).start()

        elif action == "play_sound":
            filename = msg.get("filename", "")
            sound_url = msg.get("sound_url", f"/sounds/{filename}")
            if filename:
                self.play_local_sound(filename, sound_url)

        elif action == "visual":
            effect = msg.get("effect", "")
            duration_ms = int(msg.get("duration_ms", 4000))
            self.overlay.trigger_visual(effect, duration_ms, msg)
            if effect in ("flashbang", "blackout", "invert_colors", "screen_shake", "drunk_mode"):
                self.chaos.trigger_troll(effect, duration_ms / 1000.0, msg)

        elif action == "emote":
            count = int(msg.get("count", 5))
            is_hype = bool(msg.get("is_hype", False))
            self.overlay.add_emotes(count=count, is_hype=is_hype)

        elif action in (
            "invert_keys",
            "invert_ws",
            "invert_ad",
            "jam_key",
            "disable_key",
            "weapon_roulette",
            "reload_addiction",
            "jump_frenzy",
            "crouch_spam",
            "mouse_sensitivity",
            "mouse_invert",
        ):
            duration = float(msg.get("duration_sec") or msg.get("duration") or 8.0)
            params = dict(msg)
            if "factor" in params and "multiplier" not in params:
                params["multiplier"] = params["factor"]
            self.chaos.trigger_troll(action, duration, params)

    def _set_connected(self, state: bool) -> None:
        if self.connected != state:
            self.connected = state
            self._update_tray_icon()

    def _create_status_image(self, connected: bool) -> "Image.Image":
        size = 64
        img = Image.new("RGBA", (size, size), (13, 15, 18, 255))
        draw = ImageDraw.Draw(img)

        icon_candidates = [
            Path(getattr(sys, "_MEIPASS", ".")) / "icon.png",
            Path(__file__).resolve().parents[2] / "icon.png",
            Path("icon.png"),
        ]
        for candidate in icon_candidates:
            if candidate.exists():
                try:
                    base = Image.open(candidate).convert("RGBA").resize((size, size))
                    img.paste(base, (0, 0), base)
                    break
                except Exception:
                    pass

        dot_color = (34, 197, 94, 255) if connected else (239, 68, 68, 255)
        draw.ellipse([42, 42, 60, 60], fill=dot_color, outline=(0, 0, 0, 255), width=2)
        return img

    def _update_tray_icon(self) -> None:
        if not HAS_TRAY or not self.tray_icon:
            return
        try:
            self.tray_icon.icon = self._create_status_image(self.connected)
            status_text = "Verbunden" if self.connected else "Getrennt"
            self.tray_icon.title = f"StreamGremlin v{AGENT_VERSION} ({status_text})"
            self.tray_icon.menu = self._build_tray_menu()
        except Exception:
            pass

    def run_tray(self) -> None:
        if not HAS_TRAY:
            logger.info("pystray not installed; running in console mode.")
            self._stop_event.wait()
            return

        self.tray_icon = pystray.Icon(
            "StreamGremlin",
            self._create_status_image(False),
            f"StreamGremlin Companion Agent v{AGENT_VERSION}",
            self._build_tray_menu(),
        )
        self.tray_icon.run()

    def start(self) -> None:
        self.chaos.start_listeners()
        self.overlay.start()
        ws_thread = threading.Thread(target=lambda: asyncio.run(self.ws_loop()), daemon=True)
        ws_thread.start()
        self.run_tray()


def load_agent_config() -> Tuple[str, str, bool]:
    hub_url = os.environ.get("STREAMGREMLIN_HUB_URL", "").strip()
    token = os.environ.get("STREAMGREMLIN_AGENT_TOKEN", "").strip()
    auto_update = os.environ.get("STREAMGREMLIN_AUTO_UPDATE", "true").lower() in ("true", "1", "yes")

    cfg_candidates = [
        Path(getattr(sys, "_MEIPASS", ".")) / "agent_config.json",
        Path("agent_config.json"),
        Path("agent.json"),
        Path(__file__).resolve().parent / "agent_config.json",
        Path(__file__).resolve().parent / "agent.json",
    ]
    for p in cfg_candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                hub_url = hub_url or data.get("hub_url") or data.get("url") or data.get("hub_http_url") or ""
                token = token or data.get("token") or data.get("agent_token") or ""
                if "auto_update" in data:
                    auto_update = bool(data["auto_update"])
                break
            except Exception:
                pass

    return hub_url or "https://streamgremlin.orfel.de", token, auto_update


def main() -> None:
    parser = argparse.ArgumentParser(description=f"StreamGremlin Windows Companion Agent v{AGENT_VERSION}")
    parser.add_argument("-v", "--version", action="version", version=f"StreamGremlin v{AGENT_VERSION}")
    parser.add_argument("--url", default="", help="StreamGremlin Hub Base URL")
    parser.add_argument("--token", default="", help="Agent Auth Token")
    parser.add_argument("--auto-update", dest="auto_update", action="store_true", default=None, help="Automatisches Update aktivieren")
    parser.add_argument("--no-auto-update", dest="auto_update", action="store_false", help="Automatisches Update deaktivieren")
    parser.add_argument("--update", action="store_true", help="Auf Updates prüfen und sofort installieren")
    args = parser.parse_args()

    default_url, default_token, default_auto_update = load_agent_config()
    hub_url = args.url or default_url
    token = args.token or default_token
    auto_update = default_auto_update if args.auto_update is None else args.auto_update

    admin_mode = is_admin()
    priv_info = "Administrator (Full UIPI bypass)" if admin_mode else "Standard User (Tip: Run as Admin if game runs as Admin)"

    logger.info("=" * 65)
    logger.info("StreamGremlin Companion Agent v%s starting", AGENT_VERSION)
    logger.info("Timestamp: %s", datetime.datetime.now().isoformat())
    logger.info("Python: %s (%s)", sys.version.replace("\n", " "), platform.architecture()[0])
    logger.info("Platform: %s %s (Release: %s, Version: %s)", platform.system(), platform.release(), platform.version(), platform.win32_ver()[1] if hasattr(platform, "win32_ver") else "")
    logger.info("Executable: %s", sys.executable)
    logger.info("Working Directory: %s", os.getcwd())
    logger.info("PyInstaller Frozen: %s", getattr(sys, "frozen", False))
    logger.info("PyInstaller MEIPASS: %s", getattr(sys, "_MEIPASS", "None"))
    logger.info("Privilege Level: %s", priv_info)
    logger.info("Primary Log File: %s", PRIMARY_LOG_FILE)
    logger.info("Hub URL: %s", hub_url)
    logger.info("Auto-Update: %s", "Aktiviert" if auto_update else "Deaktiviert")
    logger.info("Hardware Panic Hotkey: Press ESC 3x within 1.5s")
    logger.info("=" * 65)

    print(f"=== StreamGremlin Companion Agent v{AGENT_VERSION} ===")
    print(f"Hub URL: {hub_url}")
    print(f"Privilege Level: {priv_info}")
    print(f"Auto-Update: {'Aktiviert' if auto_update else 'Deaktiviert'}")
    print("Emergency Panic Hotkey: Press ESC 3x within 1.5s")

    agent = StreamGremlinAgent(hub_url=hub_url, token=token, auto_update=auto_update)

    if args.update:
        print("Prüfe auf Updates...")
        asyncio.run(agent.check_for_updates())
        if agent.update_available:
            print(f"Update auf v{agent.update_available} wird durchgeführt...")
            agent.perform_self_update()
        else:
            print(f"Bereits auf der neuesten Version (v{AGENT_VERSION}).")
            sys.exit(0)

    agent.start()


if __name__ == "__main__":
    main()
