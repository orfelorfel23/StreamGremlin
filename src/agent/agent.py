"""StreamGremlin Windows Companion Agent (StreamGremlin.exe).

100% Anti-Cheat-Safe Userland Input Chaos Agent:
- Zero memory reading/writing, zero DLL injection, zero process inspection.
- Uses standard Windows userland keyboard/mouse synthesis and hooks (pynput + ctypes User32).
- Outbound WebSocket connection to StreamGremlin Hub with silent auto-reconnect.
- Emergency Hardware Panic: Press ESC 3x within 1.5 seconds to immediately stop all effects.
- Windows System Tray icon (Green = Connected, Red = Disconnected) with Status & Exit menu.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("streamgremlin.agent")

AGENT_VERSION = "1.0.0"

# Win32 Userland Mouse Speed Constants (SPI_GETMOUSESPEED / SPI_SETMOUSESPEED)
SPI_GETMOUSESPEED = 0x0070
SPI_SETMOUSESPEED = 0x0071
SPIF_SENDCHANGE = 0x0002


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


class InputChaosController:
    """Manages overlapping userland keyboard and mouse troll effects safely."""

    def __init__(self, on_panic_callback=None) -> None:
        self.on_panic_callback = on_panic_callback
        self._lock = threading.RLock()
        self.active_trolls: Dict[str, float] = {}  # action_key -> expiry timestamp
        self.disabled_keys: Dict[str, float] = {}  # key_name -> expiry timestamp
        self.jammed_keys: Dict[str, float] = {}    # key_name -> expiry timestamp
        self.original_mouse_speed: int = get_win32_mouse_speed()
        self.mouse_speed_overridden: bool = False

        self.esc_timestamps: List[float] = []
        self._synthetic_guard = False

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
        logger.info("Userland keyboard/mouse listeners initialized (Anti-Cheat safe).")

    def stop(self) -> None:
        self._running = False
        self.panic_reset(notify_hub=False)
        if self._kb_listener:
            self._kb_listener.stop()
        if self._mouse_listener:
            self._mouse_listener.stop()

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
            # If invert_keys is active, counter-tap opposite movement key
            if self.active_trolls.get("invert_keys", 0) > now:
                swap_map = {"w": "s", "s": "w", "a": "d", "d": "a"}
                if ch in swap_map and self.kb_controller:
                    opposite = swap_map[ch]
                    self._tap_key_safe(opposite, hold_sec=0.06)

            # If disable_key is active for this key, release it immediately
            if self.disabled_keys.get(ch, 0) > now and self.kb_controller:
                try:
                    self._synthetic_guard = True
                    self.kb_controller.release(ch)
                finally:
                    self._synthetic_guard = False

    def _on_mouse_move(self, x: int, y: int) -> None:
        """Apply slight disorienting axis nudge when mouse_invert is active."""
        if self._synthetic_guard or not self.mouse_controller:
            return
        now = time.time()
        with self._lock:
            if self.active_trolls.get("mouse_invert", 0) <= now:
                return
        # Gentle jitter/invert nudge without locking cursor
        try:
            self._synthetic_guard = True
            dx = random.choice([-6, 6])
            dy = random.choice([-6, 6])
            self.mouse_controller.move(dx, dy)
        except Exception:
            pass
        finally:
            self._synthetic_guard = False

    def _tap_key_safe(self, key_Target: Any, hold_sec: float = 0.05) -> None:
        if not self.kb_controller:
            return
        try:
            self._synthetic_guard = True
            self.kb_controller.press(key_Target)
            time.sleep(hold_sec)
            self.kb_controller.release(key_Target)
        except Exception as exc:
            logger.debug("Key tap error: %s", exc)
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
                if self.kb_controller:
                    try:
                        self._synthetic_guard = True
                        self.kb_controller.press(key_name)
                    finally:
                        self._synthetic_guard = False

            elif action == "disable_key":
                key_name = str(params.get("key", "r")).lower()[:1] or "r"
                self.disabled_keys[key_name] = expiry

            elif action == "mouse_sensitivity":
                if not self.mouse_speed_overridden:
                    self.original_mouse_speed = get_win32_mouse_speed()
                    self.mouse_speed_overridden = True
                mode = str(params.get("mode", "snail")).lower()
                multiplier = float(params.get("multiplier", 0.2 if mode == "snail" else 2.5))
                target_speed = 2 if multiplier < 1.0 else 20
                set_win32_mouse_speed(target_speed)
                self.active_trolls["mouse_sensitivity"] = expiry

            else:
                self.active_trolls[action] = expiry

    def panic_reset(self, notify_hub: bool = True) -> None:
        """Immediately cancel all active trolls and restore all held keys & mouse speed."""
        with self._lock:
            jammed = list(self.jammed_keys.keys())
            self.active_trolls.clear()
            self.disabled_keys.clear()
            self.jammed_keys.clear()

            if self.kb_controller:
                try:
                    self._synthetic_guard = True
                    for k in jammed:
                        self.kb_controller.release(k)
                    for k in ("w", "a", "s", "d", "f", "r", "c"):
                        self.kb_controller.release(k)
                    if HAS_PYNPUT:
                        self.kb_controller.release(keyboard.Key.space)
                        self.kb_controller.release(keyboard.Key.ctrl_l)
                except Exception:
                    pass
                finally:
                    self._synthetic_guard = False

            if self.mouse_speed_overridden:
                set_win32_mouse_speed(self.original_mouse_speed)
                self.mouse_speed_overridden = False

        if notify_hub and self.on_panic_callback:
            try:
                self.on_panic_callback()
            except Exception:
                pass

    def _background_tick_loop(self) -> None:
        """Periodic worker that executes periodic troll actions and cleans up expired timers."""
        last_jump = 0.0
        last_reload = 0.0
        last_roulette = 0.0
        last_flashlight = 0.0
        last_crouch = 0.0

        while self._running:
            now = time.time()
            with self._lock:
                # 1. Clean up expired jammed keys
                expired_jams = [k for k, exp in self.jammed_keys.items() if exp <= now]
                for k in expired_jams:
                    del self.jammed_keys[k]
                    if self.kb_controller:
                        try:
                            self._synthetic_guard = True
                            self.kb_controller.release(k)
                        finally:
                            self._synthetic_guard = False
                    logger.info("Released jammed key: %s", k)

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
                        logger.info("Restored Windows mouse speed to %d", self.original_mouse_speed)

                active_snapshot = dict(self.active_trolls)
                jammed_snapshot = dict(self.jammed_keys)

            # Re-assert jammed keys periodically
            for k in jammed_snapshot:
                if self.kb_controller:
                    try:
                        self._synthetic_guard = True
                        self.kb_controller.press(k)
                    finally:
                        self._synthetic_guard = False

            # Execute periodic trolls outside lock
            if "jump_frenzy" in active_snapshot and now - last_jump >= random.uniform(0.75, 1.35):
                last_jump = now
                if HAS_PYNPUT:
                    self._tap_key_safe(keyboard.Key.space, hold_sec=0.06)

            if "reload_addiction" in active_snapshot and now - last_reload >= random.uniform(1.2, 2.5):
                last_reload = now
                self._tap_key_safe("r", hold_sec=0.05)

            if "weapon_roulette" in active_snapshot and now - last_roulette >= random.uniform(0.9, 1.8):
                last_roulette = now
                slot_key = random.choice(["1", "2", "3", "4"])
                self._tap_key_safe(slot_key, hold_sec=0.05)

            if "flashlight_rave" in active_snapshot and now - last_flashlight >= 0.35:
                last_flashlight = now
                self._tap_key_safe("f", hold_sec=0.04)

            if "crouch_spam" in active_snapshot and now - last_crouch >= 0.28:
                last_crouch = now
                if HAS_PYNPUT:
                    self._tap_key_safe(keyboard.Key.ctrl_l, hold_sec=0.08)

            time.sleep(0.1)


class StreamGremlinAgent:
    """Main Companion Agent coordinating WebSocket connection, Auto-Updater check, and System Tray."""

    def __init__(self, hub_url: str, token: str) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.connected = False
        self.update_available: Optional[str] = None
        self._stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[Any] = None
        self.tray_icon: Optional[Any] = None

        self.chaos = InputChaosController(on_panic_callback=self._send_panic_from_thread)

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

    async def check_for_updates(self) -> None:
        """Query GET /api/agent/version on startup."""
        url = f"{self.hub_url}/api/agent/version"
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    remote_ver = str(data.get("latest_version") or data.get("version") or AGENT_VERSION)
                    if remote_ver != AGENT_VERSION:
                        self.update_available = remote_ver
                        logger.warning(
                            "New StreamGremlin Agent version available: v%s (current: v%s). Download: %s",
                            remote_ver,
                            AGENT_VERSION,
                            data.get("download_url", self.hub_url),
                        )
                    else:
                        logger.info("StreamGremlin Agent v%s is up to date.", AGENT_VERSION)
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
                    # Send auth handshake expected by Hub
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
            logger.warning("Hub triggered PANIC! Resetting all active inputs.")
            self.chaos.panic_reset(notify_hub=False)
        elif action == "agent_troll":
            troll_act = str(msg.get("troll_action") or "")
            duration = float(msg.get("duration") or msg.get("duration_sec") or 5.0)
            params = msg.get("params") or msg
            self.chaos.trigger_troll(troll_act, duration, params)
        elif action in (
            "invert_keys",
            "jam_key",
            "disable_key",
            "weapon_roulette",
            "reload_addiction",
            "jump_frenzy",
            "flashlight_rave",
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
        """Build tray icon with Gremlin mascot (if found) + bright Green/Red status badge."""
        size = 64
        img = Image.new("RGBA", (size, size), (13, 15, 18, 255))
        draw = ImageDraw.Draw(img)

        # Try loading icon.png from bundle or parent folder
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
        else:
            # Industrial hexagon fallback
            draw.rectangle([4, 4, 60, 60], fill=(24, 28, 34, 255), outline=(57, 255, 20, 255), width=3)

        # Status dot in bottom-right corner (Green = Connected, Red = Disconnected)
        badge_color = (57, 255, 20, 255) if connected else (255, 42, 42, 255)
        draw.ellipse([40, 40, 62, 62], fill=badge_color, outline=(0, 0, 0, 255), width=3)
        return img

    def _update_tray_icon(self) -> None:
        if not self.tray_icon or not HAS_TRAY:
            return
        try:
            self.tray_icon.icon = self._create_status_image(self.connected)
            status_str = "Connected" if self.connected else "Disconnected (Reconnecting...)"
            self.tray_icon.title = f"StreamGremlin Agent v{AGENT_VERSION} — {status_str}"
        except Exception:
            pass

    def run(self) -> None:
        """Start input listeners, background WebSocket thread, and Windows System Tray."""
        self.chaos.start_listeners()

        ws_thread = threading.Thread(target=lambda: asyncio.run(self.ws_loop()), daemon=True)
        ws_thread.start()

        if HAS_TRAY:
            def on_panic_clicked(icon, item):
                self.chaos.panic_reset(notify_hub=True)

            def on_exit_clicked(icon, item):
                logger.info("Shutting down StreamGremlin Agent...")
                self._stop_event.set()
                self.chaos.stop()
                icon.stop()

            menu = pystray.Menu(
                pystray.MenuItem(
                    lambda item: f"Status: {'ONLINE' if self.connected else 'OFFLINE'} (v{AGENT_VERSION})",
                    None,
                    enabled=False,
                ),
                pystray.MenuItem(
                    lambda item: f"Active Trolls: {len(self.chaos.active_trolls) + len(self.chaos.jammed_keys)}",
                    None,
                    enabled=False,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("PANIC! Reset Inputs (ESC 3x)", on_panic_clicked),
                pystray.MenuItem("Beenden", on_exit_clicked),
            )
            self.tray_icon = pystray.Icon(
                "StreamGremlin",
                self._create_status_image(False),
                f"StreamGremlin Agent v{AGENT_VERSION}",
                menu,
            )
            logger.info("System Tray icon active. Press ESC 3x at any time for Emergency Panic.")
            self.tray_icon.run()
        else:
            logger.info("Running in console mode (Press Ctrl+C to stop, or ESC 3x for Panic).")
            try:
                while not self._stop_event.is_set():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self._stop_event.set()
                self.chaos.stop()


def load_agent_config() -> Dict[str, str]:
    """Load hub_url and token from agent.json, agent_config.json, or environment variables."""
    cfg = {
        "hub_url": os.getenv("STREAMGREMLIN_HUB_URL", "https://streamgremlin.orfel.de"),
        "token": os.getenv("AGENT_TOKEN", ""),
    }
    config_paths = [
        Path("agent.json"),
        Path("agent_config.json"),
        Path(sys.executable).parent / "agent.json",
        Path(sys.executable).parent / "agent_config.json",
    ]
    for path in config_paths:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                hub = data.get("hub_http_url") or data.get("hub_url") or data.get("hub_ws_url")
                if hub:
                    cfg["hub_url"] = hub
                tok = data.get("agent_token") or data.get("token")
                if tok:
                    cfg["token"] = tok
                break
            except Exception:
                pass
    return cfg


def main() -> None:
    defaults = load_agent_config()
    parser = argparse.ArgumentParser(description="StreamGremlin Windows Companion Agent")
    parser.add_argument("--url", default=defaults["hub_url"], help="StreamGremlin Hub URL")
    parser.add_argument("--token", default=defaults["token"], help="AGENT_TOKEN shared secret")
    args = parser.parse_args()

    agent = StreamGremlinAgent(hub_url=args.url, token=args.token)
    agent.run()


if __name__ == "__main__":
    main()
