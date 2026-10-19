"""
StreamGremlin Real-Time WebSocket Dispatcher & Action Engine
Verwaltet:
- OBS Overlay WebSockets (`/ws/overlay`)
- Windows Companion Agent WebSockets (`/ws/agent`)
- Dashboard Live-Feed WebSockets (`/ws/dashboard`)
- Sound-Queue vs. überlagernde Input-Trolls
- Globaler Cooldown & Per-User Cooldown
"""
import asyncio
from collections import deque
import logging
import os
import random
import secrets
import time
from typing import Any
from fastapi import WebSocket

from src.hub.config_manager import ConfigManager
from src.hub.tts_service import TTSService
from src.hub.automation import AutomationService

logger = logging.getLogger("streamgremlin.ws_hub")

AGENT_VERSION = "1.0.0"


class WebSocketHub:
    def __init__(
        self,
        config_manager: ConfigManager,
        tts_service: TTSService,
        automation_service: AutomationService,
    ) -> None:
        self.cm = config_manager
        self.tts = tts_service
        self.automation = automation_service

        self.overlay_clients: set[WebSocket] = set()
        self.agent_clients: dict[WebSocket, dict[str, Any]] = {}
        self.dashboard_clients: set[WebSocket] = set()

        self.recent_events: deque[dict[str, Any]] = deque(maxlen=100)
        self.master_muted: bool = False

        # Cooldown tracking
        self._last_global_trigger: float = 0.0
        self._user_last_trigger: dict[str, float] = {}
        self._trigger_last_used: dict[str, float] = {}

        # Chat message timestamps for chat_velocity spike detection
        self._chat_timestamps: deque[float] = deque(maxlen=200)
        self._last_velocity_hype: float = 0.0

        # Active Timebomb state (if running)
        self.active_timebomb: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Connection Management
    # ------------------------------------------------------------------
    async def connect_overlay(self, ws: WebSocket) -> None:
        await ws.accept()
        self.overlay_clients.add(ws)
        await self.broadcast_status()

    def disconnect_overlay(self, ws: WebSocket) -> None:
        self.overlay_clients.discard(ws)

    async def connect_dashboard(self, ws: WebSocket) -> None:
        await ws.accept()
        self.dashboard_clients.add(ws)
        await ws.send_json({
            "type": "init",
            "status": self.get_status_summary(),
            "recent_events": list(self.recent_events),
        })

    def disconnect_dashboard(self, ws: WebSocket) -> None:
        self.dashboard_clients.discard(ws)

    async def connect_agent(self, ws: WebSocket, info: dict[str, Any]) -> None:
        self.agent_clients[ws] = info
        await self.broadcast_status()
        await self.log_event(
            event_type="system",
            title="Gaming-PC Agent verbunden",
            user=info.get("hostname", "Windows-PC"),
            details=f"Version {info.get('version', '1.0.0')}",
        )

    def disconnect_agent(self, ws: WebSocket) -> None:
        self.agent_clients.pop(ws, None)

    def get_status_summary(self, twitch_connected: bool | None = None) -> dict[str, Any]:
        cfg = self.cm.get_config()
        env = self.cm.read_env()
        return {
            "overlay_count": len(self.overlay_clients),
            "agent_count": len(self.agent_clients),
            "agents": [
                {"hostname": v.get("hostname", "PC"), "version": v.get("version", "1.0.0")}
                for v in self.agent_clients.values()
            ],
            "master_muted": self.master_muted,
            "twitch_channel": env.get("TWITCH_BROADCASTER_CHANNEL", ""),
            "twitch_configured": bool(env.get("TWITCH_CLIENT_ID")),
            "prefix": cfg.get("general", {}).get("prefix", "!"),
            "agent_version": AGENT_VERSION,
        }

    # ------------------------------------------------------------------
    # Broadcasting Helpers
    # ------------------------------------------------------------------
    async def broadcast_overlay(self, event: str, payload: dict[str, Any]) -> None:
        msg = {"event": event, "payload": payload}
        dead: list[WebSocket] = []
        for ws in list(self.overlay_clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.overlay_clients.discard(ws)

    async def broadcast_agent(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.agent_clients.keys()):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.agent_clients.pop(ws, None)

    async def broadcast_dashboard(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.dashboard_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.dashboard_clients.discard(ws)

    async def broadcast_status(self) -> None:
        await self.broadcast_dashboard({
            "type": "status",
            "status": self.get_status_summary(),
        })

    async def log_event(
        self,
        event_type: str,
        title: str,
        user: str = "System",
        details: str = "",
    ) -> dict[str, Any]:
        entry = {
            "id": f"evt_{secrets.token_hex(4)}",
            "timestamp": int(time.time()),
            "time_str": time.strftime("%H:%M:%S"),
            "type": event_type,
            "title": title,
            "user": user,
            "details": details,
        }
        self.recent_events.appendleft(entry)
        await self.broadcast_dashboard({"type": "event", "event": entry})
        await self.automation.forward_event_to_n8n(event_type, entry)
        return entry

    # ------------------------------------------------------------------
    # Panic Switch (Notfall-Aus)
    # ------------------------------------------------------------------
    async def trigger_panic(self, source: str = "Dashboard") -> dict[str, Any]:
        """Stoppt sofort alle aktiven Sounds, TTS, Overlays, Minigames und Tasten-Trolls."""
        self.active_timebomb = None
        await self.broadcast_overlay("panic", {"reason": source})
        await self.broadcast_agent({"action": "panic", "reason": source})
        entry = await self.log_event(
            event_type="panic",
            title="Not-Aus ausgelöst",
            user=source,
            details="Alle Sounds, Overlays und Steuerungs-Hooks gestoppt.",
        )
        return {"ok": True, "event": entry}

    # ------------------------------------------------------------------
    # Cooldown Check
    # ------------------------------------------------------------------
    def check_cooldowns(
        self,
        trigger: dict[str, Any],
        user: str,
        bypass_cooldown: bool = False,
    ) -> tuple[bool, float]:
        if bypass_cooldown:
            return True, 0.0

        now = time.time()
        cfg = self.cm.get_config()
        general = cfg.get("general", {})
        global_cd = float(general.get("global_cooldown_sec", 3))
        user_cd = float(general.get("user_cooldown_sec", 15))
        trig_cd = float(trigger.get("cooldown_sec", 5))
        trig_id = trigger.get("id", "unknown")

        # 1. Global Cooldown
        if now - self._last_global_trigger < global_cd:
            return False, round(global_cd - (now - self._last_global_trigger), 1)

        # 2. Per-User Cooldown (gegen Kanalpunkte-Wale)
        user_key = user.lower()
        if user_key not in ("broadcaster", "system", "admin", "simulator"):
            last_u = self._user_last_trigger.get(user_key, 0.0)
            if now - last_u < user_cd:
                return False, round(user_cd - (now - last_u), 1)

        # 3. Per-Trigger Cooldown
        last_t = self._trigger_last_used.get(trig_id, 0.0)
        if now - last_t < trig_cd:
            return False, round(trig_cd - (now - last_t), 1)

        self._last_global_trigger = now
        self._user_last_trigger[user_key] = now
        self._trigger_last_used[trig_id] = now
        return True, 0.0

    # ------------------------------------------------------------------
    # Action Execution Engine
    # ------------------------------------------------------------------
    async def execute_trigger(
        self,
        trigger: dict[str, Any],
        user: str = "Viewer",
        user_input: str = "",
        bypass_cooldown: bool = False,
        chat_reply_callback: Any = None,
    ) -> dict[str, Any]:
        allowed, remaining = self.check_cooldowns(trigger, user, bypass_cooldown=bypass_cooldown)
        if not allowed:
            return {"ok": False, "reason": "cooldown", "remaining_sec": remaining}

        trig_name = trigger.get("name") or trigger.get("title") or trigger.get("id", "Trigger")
        actions = trigger.get("actions", [])
        cfg = self.cm.get_config()

        await self.log_event(
            event_type=trigger.get("type", "trigger"),
            title=trig_name,
            user=user,
            details=user_input[:80] if user_input else f"{len(actions)} Aktion(en)",
        )

        for action in actions:
            await self.execute_single_action(
                action=action,
                user=user,
                user_input=user_input,
                cfg=cfg,
                chat_reply_callback=chat_reply_callback,
            )

        return {"ok": True, "trigger": trig_name}

    def _should_run_on_overlay(self, action: dict[str, Any], default: bool = True) -> bool:
        target = str(action.get("target", "both")).lower().strip()
        if target in ("overlay", "overlay_only", "stream", "stream_only"):
            return True
        if target in ("agent", "agent_only", "pc", "pc_only"):
            return False
        return default

    def _should_run_on_agent(self, action: dict[str, Any], default: bool = True) -> bool:
        target = str(action.get("target", "both")).lower().strip()
        if target in ("agent", "agent_only", "pc", "pc_only"):
            return True
        if target in ("overlay", "overlay_only", "stream", "stream_only"):
            return False
        return default

    async def execute_single_action(
        self,
        action: dict[str, Any],
        user: str = "Viewer",
        user_input: str = "",
        cfg: dict[str, Any] | None = None,
        chat_reply_callback: Any = None,
    ) -> None:
        if cfg is None:
            cfg = self.cm.get_config()

        act_type = action.get("type", "")
        troll_id = f"troll_{secrets.token_hex(4)}"

        # --- 1. AUDIO ACTIONS (Queued in Overlay & Agent) ---
        if act_type == "sound":
            if self.master_muted:
                return
            pool = action.get("pool") or []
            if isinstance(pool, str):
                pool = [pool]
            if not pool:
                return
            chosen = random.choice(pool)
            sound_cfg = cfg.get("sounds", {}).get(chosen, {})
            vol = float(action.get("volume", sound_cfg.get("volume", 0.85)))
            payload = {
                "id": troll_id,
                "sound_url": f"/sounds/{chosen}",
                "filename": chosen,
                "volume": max(0.0, min(1.0, vol)),
                "user": user,
            }
            if self._should_run_on_overlay(action, default=True):
                await self.broadcast_overlay("sound", payload)
            if self._should_run_on_agent(action, default=True):
                await self.broadcast_agent({"action": "play_sound", **payload})

        elif act_type == "tts":
            if self.master_muted:
                return
            tts_cfg = cfg.get("tts", {})
            if not tts_cfg.get("enabled", True):
                return
            text_to_speak = user_input.strip() or action.get("default_text", f"Hallo von {user}!")
            voice = action.get("voice") or tts_cfg.get("default_voice", "de-DE-KatjaNeural")
            vol = float(action.get("volume", tts_cfg.get("volume", 0.90)))
            max_chars = int(tts_cfg.get("max_characters", 200))
            engine = tts_cfg.get("engine", "edge-tts")

            res = await self.tts.generate_audio(
                text=text_to_speak,
                voice=voice,
                max_chars=max_chars,
                engine=engine,
            )
            if res.get("ok"):
                payload = {
                    "id": troll_id,
                    "audio_url": res.get("audio_url", ""),
                    "text": res.get("text", ""),
                    "user": user,
                    "voice": voice,
                    "volume": vol,
                    "fallback_browser": res.get("fallback_browser", False),
                }
                if self._should_run_on_overlay(action, default=True):
                    await self.broadcast_overlay("tts", payload)
                if self._should_run_on_agent(action, default=True):
                    await self.broadcast_agent({"action": "tts", **payload})

        elif act_type == "stop_audio":
            await self.broadcast_overlay("stop_audio", {})
            await self.broadcast_agent({"action": "stop_audio"})

        # --- 2. VISUAL OVERLAY & DUAL-DELIVERY ACTIONS ---
        elif act_type == "bouncing_emotes":
            payload = {
                "url": action.get("url", ""),
                "emote_list": action.get("emote_list", []),
                "count": int(action.get("count", 15)),
                "is_hype": bool(action.get("is_hype", True)),
            }
            if self._should_run_on_overlay(action, default=True):
                await self.broadcast_overlay("emote", payload)
            if self._should_run_on_agent(action, default=True):
                await self.broadcast_agent({"action": "emote", **payload})

        elif act_type in (
            "flashbang",
            "screen_shake",
            "screen_tilt",
            "screen_glitch",
            "fake_bsod",
            "blackout",
            "drunk_mode",
            "pixelate",
            "invert_colors",
            "tunnel_vision",
            "matrix_rain",
            "rave_mode",
            "censored_bar",
            "mirror_mode",
            "fake_alert",
            "confetti_burst",
            "gif_jumpscare",
        ):
            # Special user rule: censored_bar defaults to overlay only!
            default_overlay = True
            default_agent = False if act_type == "censored_bar" else True

            # Rich pool of hilarious troll alerts
            alert_pools = [
                {"title": "🎁 500 COMMUNITY SUBS", "msg": f"ElonMusk hat 500 Gift-Subs für {user} verschenkt!"},
                {"title": "💎 50.000 BITS SPENDE", "msg": "Papaplatte: 'Kauf dir mal Aim! GG!'"},
                {"title": "🚨 RAID VON MONTANABLACK", "msg": "MontanaBlack raidet mit 42.150 Zuschauern!"},
                {"title": "⚠️ TWITCH TRUST & SAFETY", "msg": "Dein Partnerstatus steht unter Beobachtung wegen anhaltender Unfähigkeit."},
                {"title": "👑 NEUER FOLLOWER", "msg": "Finanzamt Hamburg-Nord folgt dir jetzt!"},
                {"title": "💎 100.000 BITS SPENDE", "msg": "JeffBezos: 'Hier dein Taschengeld für diesen Monat!'"},
                {"title": "🚨 RAID VON TRYMACS", "msg": "Trymacs raidet mit 28.900 Zuschauern!"},
                {"title": "🔒 SICHERHEITSHINWEIS", "msg": "BND Cyber-Abteilung: Wir sehen alles. Vor allem deine Missed Shots."},
            ]
            chosen_alert = random.choice(alert_pools)
            alert_title = action.get("title", chosen_alert["title"])
            alert_msg = action.get("text", chosen_alert["msg"])

            visual_payload = {
                "id": troll_id,
                "effect": act_type,
                "duration_ms": int(action.get("duration_ms", 4000)),
                "angle": int(action.get("angle", 18)),
                "title": alert_title,
                "text": alert_msg,
                "image_url": action.get("image_url", "/assets/icon.png"),
                "user": user,
            }

            if self._should_run_on_overlay(action, default=default_overlay):
                await self.broadcast_overlay("visual", visual_payload)
            if self._should_run_on_agent(action, default=default_agent):
                await self.broadcast_agent({"action": "visual", **visual_payload})

            # Auto-Companion Sounds (if not explicitly disabled)
            companion_sound_map = {
                "flashbang": "flashbang.wav",
                "blackout": "fuse_switch.wav",
                "tunnel_vision": "heartbeat.wav",
                "screen_shake": "earthquake_rumble.wav",
                "screen_tilt": "tilt_creak.wav",
                "drunk_mode": "bottle_pop.wav",
                "screen_glitch": "glitch_static.wav",
                "invert_colors": "dimension_warp.wav",
                "pixelate": "bitcrush_8bit.wav",
                "rave_mode": "rave_drop.wav",
                "mirror_mode": "portal_gun.wav",
                "fake_bsod": "windows_critical.wav",
                "censored_bar": "censor_beep.wav",
                "matrix_rain": "keyboard_clatter.wav",
                "confetti_burst": "party_horn.wav",
                "gif_jumpscare": "jumpscare_screech.wav",
            }
            comp_sound = companion_sound_map.get(act_type)
            if comp_sound and not action.get("mute_sound", False) and not self.master_muted:
                sound_payload = {
                    "id": f"snd_{secrets.token_hex(4)}",
                    "sound_url": f"/sounds/{comp_sound}",
                    "filename": comp_sound,
                    "volume": float(action.get("sound_volume", 0.85)),
                    "user": user,
                }
                if self._should_run_on_overlay(action, default=default_overlay):
                    await self.broadcast_overlay("sound", sound_payload)
                if self._should_run_on_agent(action, default=default_agent):
                    await self.broadcast_agent({"action": "play_sound", **sound_payload})

        elif act_type == "troll_hud":
            # User decision: Countdown bar is ONLY on the stream!
            if cfg.get("general", {}).get("troll_hud_enabled", True):
                await self.broadcast_overlay(
                    "troll_hud",
                    {
                        "action": "start",
                        "troll_id": troll_id,
                        "title": action.get("title", "Troll aktiv"),
                        "user": user,
                        "duration_sec": int(action.get("duration_sec", 10)),
                    },
                )

        # --- 3. INTERACTIVE MINIGAMES ---
        elif act_type == "wheel_of_fortune":
            outcomes = action.get("outcomes") or [
                {"label": "🔄 Steuerung Invertiert (8s)", "effect": "invert_keys"},
                {"label": "💣 Flashbang Granate!", "effect": "flashbang"},
                {"label": "🍺 5 Promille Modus (10s)", "effect": "drunk_mode"},
                {"label": "🔫 Waffen-Roulette (6s)", "effect": "weapon_roulette"},
                {"label": "🍀 Niete! Nichts passiert", "effect": "none"},
                {"label": "🕺 Rave Party & Konfetti!", "effect": "rave_mode"},
                {"label": "🐌 Schneckentempo Maus (8s)", "effect": "mouse_sensitivity", "mode": "snail"},
                {"label": "🚀 Quake-Pro Maus (8s)", "effect": "mouse_sensitivity", "mode": "quake"},
                {"label": "🐰 Häschen-Hüpfen (8s)", "effect": "jump_frenzy"},
                {"label": "❌ Niete! Punkte verloren", "effect": "none"},
            ]
            winner_idx = random.randrange(len(outcomes))
            winner = outcomes[winner_idx]
            wheel_payload = {
                "game": "wheel_of_fortune",
                "outcomes": outcomes,
                "winner_index": winner_idx,
                "winner": winner,
                "user": user,
                "duration_ms": int(action.get("duration_ms", 5500)),
            }
            if self._should_run_on_overlay(action, default=True):
                await self.broadcast_overlay("minigame", wheel_payload)
            if self._should_run_on_agent(action, default=True):
                await self.broadcast_agent({"action": "minigame", **wheel_payload})

            # Trigger the winning outcome after wheel spin finishes (4.2s)
            async def _delayed_outcome() -> None:
                await asyncio.sleep(4.2)
                eff = winner.get("effect")
                if not eff or eff == "none":
                    return
                if eff in ("invert_keys", "invert_ws", "invert_ad"):
                    await self.execute_single_action(
                        {"type": eff, "duration_sec": 8, "title": winner["label"]},
                        user=user,
                    )
                elif eff in ("flashbang", "drunk_mode", "rave_mode", "pixelate", "screen_shake"):
                    await self.execute_single_action(
                        {"type": eff, "duration_ms": 6000}, user=user
                    )
                elif eff in ("weapon_roulette", "jump_frenzy", "crouch_spam", "reload_addiction"):
                    await self.execute_single_action(
                        {"type": eff, "duration_sec": 8, "title": winner["label"]}, user=user
                    )
                elif eff == "mouse_sensitivity":
                    await self.execute_single_action(
                        {"type": "mouse_sensitivity", "mode": winner.get("mode", "snail"), "duration_sec": 8, "title": winner["label"]},
                        user=user,
                    )

            asyncio.create_task(_delayed_outcome())

        elif act_type == "timebomb":
            bomb_mode = str(action.get("mode", "spam")).lower()
            defuse_word = str(action.get("defuse_word", "cut")).lower().strip()
            req_count = int(action.get("required_count", 8))
            duration_sec = int(action.get("duration_sec", 20))
            bomb_id = f"bomb_{secrets.token_hex(4)}"

            # Wire roulette setup (Rot, Blau, Grün)
            wire_colors = ["rot", "blau", "grün"]
            correct_wire = random.choice(wire_colors)
            remaining_wires = [w for w in wire_colors if w != correct_wire]
            exploding_wire = remaining_wires[0]
            penalty_wire = remaining_wires[1]

            self.active_timebomb = {
                "id": bomb_id,
                "mode": bomb_mode,
                "defuse_word": defuse_word,
                "required_count": req_count,
                "current_count": 0,
                "correct_wire": correct_wire,
                "exploding_wire": exploding_wire,
                "penalty_wire": penalty_wire,
                "expires_at": time.time() + duration_sec,
            }

            bomb_payload = {
                "game": "timebomb",
                "action": "start",
                "bomb_id": bomb_id,
                "mode": bomb_mode,
                "defuse_word": defuse_word,
                "required_count": req_count,
                "duration_sec": duration_sec,
                "user": user,
            }
            if self._should_run_on_overlay(action, default=True):
                await self.broadcast_overlay("minigame", bomb_payload)
            if self._should_run_on_agent(action, default=True):
                await self.broadcast_agent({"action": "minigame", **bomb_payload})

            async def _bomb_timer() -> None:
                await asyncio.sleep(duration_sec)
                if self.active_timebomb and self.active_timebomb.get("id") == bomb_id:
                    self.active_timebomb = None
                    await self.broadcast_overlay(
                        "minigame", {"game": "timebomb", "action": "explode", "bomb_id": bomb_id}
                    )
                    await self.broadcast_agent(
                        {"action": "minigame", "game": "timebomb", "sub_action": "explode", "bomb_id": bomb_id}
                    )
                    await self.execute_single_action({"type": "flashbang", "duration_ms": 3500}, user="Zeitbombe")
                    await self.execute_single_action({"type": "screen_shake", "duration_ms": 3500}, user="Zeitbombe")

            asyncio.create_task(_bomb_timer())

        # --- 4. GAMING-PC AGENT INPUT TROLLS (100% Anti-Cheat Safe) ---
        elif act_type in (
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
            dur_sec = int(action.get("duration_sec", 10))

            # Specific pairs for W/S vs A/D
            if act_type == "invert_ws":
                pairs = [["w", "s"]]
                hud_title = "W/S Invertiert"
            elif act_type == "invert_ad":
                pairs = [["a", "d"]]
                hud_title = "A/D Invertiert"
            elif act_type == "invert_keys":
                pairs = action.get("pairs", [["w", "s"], ["a", "d"]])
                hud_title = "WASD Invertiert"
            else:
                pairs = action.get("pairs", [["w", "s"], ["a", "d"]])
                hud_title = action.get("title", act_type.replace("_", " ").title())

            agent_payload = {
                "action": act_type,
                "troll_id": troll_id,
                "user": user,
                "duration_sec": dur_sec,
                "pairs": pairs,
                "key": action.get("key", "w"),
                "keys": action.get("keys", ["1", "2", "3", "4"]),
                "mode": action.get("mode", "snail" if act_type == "mouse_sensitivity" else "y"),
                "factor": float(action.get("factor", 0.3)),
            }
            # Send hardware troll to Gaming-PC Agent
            await self.broadcast_agent(agent_payload)

            # User decision: Countdown bar is ONLY on the stream!
            if cfg.get("general", {}).get("troll_hud_enabled", True):
                await self.broadcast_overlay(
                    "troll_hud",
                    {
                        "action": "start",
                        "troll_id": troll_id,
                        "title": action.get("title", hud_title),
                        "user": user,
                        "duration_sec": dur_sec,
                    },
                )

            # Audio decisions:
            # - invert_keys / ws / ad: evil_laugh.wav
            # - jam_key: key_jam_click.wav
            # - disable_key: access_denied_buzzer.wav
            # - jump_frenzy: boing.wav
            # - mouse_sensitivity: snail_slurp.wav (snail) vs f1_whoosh.wav (quake)
            # - mouse_invert: whip_whistle.wav
            # - weapon_roulette, reload_addiction, crouch_spam: KEIN SOUND
            troll_sound_map = {
                "invert_keys": "evil_laugh.wav",
                "invert_ws": "evil_laugh.wav",
                "invert_ad": "evil_laugh.wav",
                "jam_key": "key_jam_click.wav",
                "disable_key": "access_denied_buzzer.wav",
                "jump_frenzy": "boing.wav",
                "mouse_invert": "whip_whistle.wav",
            }
            if act_type == "mouse_sensitivity":
                mode = str(action.get("mode", "snail")).lower()
                chosen_snd = "snail_slurp.wav" if mode == "snail" else "f1_whoosh.wav"
            else:
                chosen_snd = troll_sound_map.get(act_type)

            if chosen_snd and not self.master_muted:
                sound_payload = {
                    "id": f"snd_{secrets.token_hex(4)}",
                    "sound_url": f"/sounds/{chosen_snd}",
                    "filename": chosen_snd,
                    "volume": float(action.get("sound_volume", 0.85)),
                    "user": user,
                }
                await self.broadcast_overlay("sound", sound_payload)
                await self.broadcast_agent({"action": "play_sound", **sound_payload})

        # --- 5. SMART HOME / HOME ASSISTANT (Kein Sound) ---
        elif act_type in ("smart_home", "ha_action"):
            await self.automation.execute_smart_home_action(action, user=user)

        # --- 6. CHAT REPLY ---
        elif act_type == "chat_reply":
            if cfg.get("general", {}).get("bot_chat_reply", True) and chat_reply_callback:
                template = action.get("message", "@{user} hat {title} ausgelöst.")
                formatted = template.replace("{user}", user)
                await chat_reply_callback(formatted)

    # ------------------------------------------------------------------
    # Incoming Chat Message Processor (Emotes, Timebomb, Velocity)
    # ------------------------------------------------------------------
    async def register_chat_activity(
        self,
        user: str,
        message: str,
        emote_urls: list[str],
    ) -> None:
        cfg = self.cm.get_config()
        general = cfg.get("general", {})

        # 1. Active Timebomb Defuse Check (Both Spam and Wire modes)
        if self.active_timebomb:
            tb = self.active_timebomb
            bomb_mode = tb.get("mode", "spam")
            msg_lower = message.lower().strip()

            if bomb_mode == "wire":
                # Chat votes wire: "!cut rot", "!cut blau", "!cut grün"
                for wire in ("rot", "blau", "grün"):
                    if wire in msg_lower and ("cut" in msg_lower or "schneide" in msg_lower):
                        if wire == tb["correct_wire"]:
                            # Defused!
                            self.active_timebomb = None
                            await self.broadcast_overlay(
                                "minigame", {"game": "timebomb", "action": "defused", "wire": wire, "user": user}
                            )
                            await self.broadcast_agent(
                                {"action": "minigame", "game": "timebomb", "sub_action": "defused", "wire": wire, "user": user}
                            )
                            await self.execute_single_action(
                                {"type": "confetti_burst", "duration_ms": 4000}, user=user
                            )
                        elif wire == tb["exploding_wire"]:
                            # Instant explosion!
                            self.active_timebomb = None
                            await self.broadcast_overlay(
                                "minigame", {"game": "timebomb", "action": "explode", "wire": wire, "user": user}
                            )
                            await self.broadcast_agent(
                                {"action": "minigame", "game": "timebomb", "sub_action": "explode", "wire": wire, "user": user}
                            )
                            await self.execute_single_action({"type": "flashbang", "duration_ms": 3500}, user="Zeitbombe")
                            await self.execute_single_action({"type": "screen_shake", "duration_ms": 3500}, user="Zeitbombe")
                        elif wire == tb["penalty_wire"]:
                            # Halve remaining time!
                            rem = max(3.0, (tb["expires_at"] - time.time()) / 2.0)
                            tb["expires_at"] = time.time() + rem
                            await self.broadcast_overlay(
                                "minigame", {"game": "timebomb", "action": "penalty", "wire": wire, "seconds_left": int(rem), "user": user}
                            )
                        break
            else:
                # Spam-cut mode
                word = tb["defuse_word"]
                if word in msg_lower.split():
                    tb["current_count"] += 1
                    cur = tb["current_count"]
                    req = tb["required_count"]
                    await self.broadcast_overlay(
                        "minigame",
                        {
                            "game": "timebomb",
                            "action": "progress",
                            "current_count": cur,
                            "required_count": req,
                            "user": user,
                        },
                    )
                    if cur >= req:
                        self.active_timebomb = None
                        await self.broadcast_overlay(
                            "minigame",
                            {"game": "timebomb", "action": "defused", "user": user},
                        )
                        await self.broadcast_agent(
                            {"action": "minigame", "game": "timebomb", "sub_action": "defused", "user": user}
                        )
                        await self.execute_single_action(
                            {"type": "confetti_burst", "duration_ms": 3500}, user=user
                        )

        # 2. Individual Chat Emote Drop (Dual-Mode: Stream + PC)
        if general.get("chat_emotes_auto_bounce", True) and emote_urls:
            for url in emote_urls[:8]:
                emote_payload = {"url": url, "count": 1, "is_hype": False}
                await self.broadcast_overlay("emote", emote_payload)
                await self.broadcast_agent({"action": "emote", **emote_payload})

        # 3. Chat Velocity Spike Detection (Dual-Mode: Stream + PC)
        now = time.time()
        self._chat_timestamps.append(now)
        recent_5s = [t for t in self._chat_timestamps if now - t <= 5.0]
        threshold = int(general.get("chat_velocity_threshold", 15))
        if len(recent_5s) >= threshold and (now - self._last_velocity_hype > 20.0):
            self._last_velocity_hype = now
            hype_payload = {"url": "", "count": 35, "is_hype": True}
            await self.broadcast_overlay("emote", hype_payload)
            await self.broadcast_agent({"action": "emote", **hype_payload})
            await self.log_event(
                event_type="chat_velocity",
                title="Chat-Hype erkannt",
                user="Chat",
                details=f"{len(recent_5s)} Nachrichten in 5s",
            )

