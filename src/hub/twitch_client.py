"""
StreamGremlin Twitch EventSub WebSocket & Helix API Client
Features:
- Outbound EventSub WebSocket (`wss://eventsub.wss.twitch.tv/ws`) mit Silent Auto-Reconnect
- 1-Klick OAuth2 Flow mit automatischer Token-Erneuerung (Refresh Token)
- Automatische Kanalpunkte-Erstellung (`POST /helix/channel_points/custom_rewards`)
- BTTV (BetterTTV) & Twitch Emote-Auflösung für Bouncing Emotes
- Strikt nur öffentlicher Kanal-Chat (keine Whispers)
- Wort-Matching: Togglebar pro Trigger zwischen Ganzwort (`\\bwort\\b`) und Substring
"""
import asyncio
import json
import logging
import os
import re
import urllib.parse
from typing import Any
import httpx
import websockets

from src.hub.config_manager import ConfigManager
from src.hub.ws_hub import WebSocketHub

logger = logging.getLogger("streamgremlin.twitch")

TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_HELIX_URL = "https://api.twitch.tv/helix"
TWITCH_EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"

REQUIRED_SCOPES = [
    "user:read:chat",
    "user:write:chat",
    "chat:read",
    "chat:edit",
    "channel:read:redemptions",
    "channel:manage:redemptions",
    "channel:read:subscriptions",
    "bits:read",
    "channel:read:hype_train",
    "moderator:read:followers",
]


class TwitchManager:
    def __init__(self, config_manager: ConfigManager, ws_hub: WebSocketHub) -> None:
        self.cm = config_manager
        self.hub = ws_hub
        self.connected: bool = False
        self.broadcaster_id: str = ""
        self.broadcaster_login: str = ""
        self.session_id: str = ""
        self._ws_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self.bttv_emotes: dict[str, str] = {}

    # ------------------------------------------------------------------
    # OAuth2 & Token Management
    # ------------------------------------------------------------------
    def get_oauth_url(self, redirect_uri: str) -> str:
        env = self.cm.read_env()
        client_id = env.get("TWITCH_CLIENT_ID", "").strip()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(REQUIRED_SCOPES),
            "force_verify": "true",
        }
        return f"{TWITCH_AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        env = self.cm.read_env()
        client_id = env.get("TWITCH_CLIENT_ID", "").strip()
        client_secret = env.get("TWITCH_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise ValueError("TWITCH_CLIENT_ID oder TWITCH_CLIENT_SECRET fehlt.")

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                TWITCH_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            )
            resp.raise_for_status()
            tokens = resp.json()

        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        self.cm.write_env(
            {
                "TWITCH_ACCESS_TOKEN": access_token,
                "TWITCH_REFRESH_TOKEN": refresh_token,
            },
            encrypt_age=True,
        )
        await self.fetch_broadcaster_profile()
        await self.restart_eventsub()
        return {"ok": True, "broadcaster_login": self.broadcaster_login}

    async def refresh_access_token(self) -> bool:
        env = self.cm.read_env()
        client_id = env.get("TWITCH_CLIENT_ID", "").strip()
        client_secret = env.get("TWITCH_CLIENT_SECRET", "").strip()
        refresh_token = env.get("TWITCH_REFRESH_TOKEN", "").strip()
        if not (client_id and client_secret and refresh_token):
            return False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    TWITCH_TOKEN_URL,
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                )
                if resp.status_code != 200:
                    return False
                tokens = resp.json()
                self.cm.write_env(
                    {
                        "TWITCH_ACCESS_TOKEN": tokens.get("access_token", ""),
                        "TWITCH_REFRESH_TOKEN": tokens.get("refresh_token", refresh_token),
                    },
                    encrypt_age=True,
                )
                return True
        except Exception as exc:
            logger.warning("Failed to refresh Twitch token: %s", exc)
            return False

    def _helix_headers(self) -> dict[str, str]:
        env = self.cm.read_env()
        return {
            "Client-Id": env.get("TWITCH_CLIENT_ID", "").strip(),
            "Authorization": f"Bearer {env.get('TWITCH_ACCESS_TOKEN', '').strip()}",
            "Content-Type": "application/json",
        }

    async def fetch_broadcaster_profile(self) -> bool:
        env = self.cm.read_env()
        if not env.get("TWITCH_ACCESS_TOKEN"):
            return False
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"{TWITCH_HELIX_URL}/users", headers=self._helix_headers())
                if resp.status_code == 401 and await self.refresh_access_token():
                    resp = await client.get(f"{TWITCH_HELIX_URL}/users", headers=self._helix_headers())
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    if data:
                        user = data[0]
                        self.broadcaster_id = user["id"]
                        self.broadcaster_login = user["login"]
                        self.cm.write_env(
                            {"TWITCH_BROADCASTER_CHANNEL": self.broadcaster_login},
                            encrypt_age=True,
                        )
                        await self.load_bttv_emotes(self.broadcaster_id)
                        return True
        except Exception as exc:
            logger.warning("Could not fetch Twitch profile: %s", exc)
        return False

    # ------------------------------------------------------------------
    # BetterTTV (BTTV) Emote Integration
    # ------------------------------------------------------------------
    async def load_bttv_emotes(self, twitch_user_id: str = "") -> None:
        emotes: dict[str, str] = {}
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                # 1. Global BTTV Emotes
                r_glob = await client.get("https://api.betterttv.net/3/cached/emotes/global")
                if r_glob.status_code == 200:
                    for item in r_glob.json():
                        code = item.get("code")
                        eid = item.get("id")
                        if code and eid:
                            emotes[code] = f"https://cdn.betterttv.net/emote/{eid}/3x"

                # 2. Channel-Specific BTTV Emotes
                if twitch_user_id:
                    r_chan = await client.get(
                        f"https://api.betterttv.net/3/cached/users/twitch/{twitch_user_id}"
                    )
                    if r_chan.status_code == 200:
                        cdata = r_chan.json()
                        for item in cdata.get("channelEmotes", []) + cdata.get("sharedEmotes", []):
                            code = item.get("code")
                            eid = item.get("id")
                            if code and eid:
                                emotes[code] = f"https://cdn.betterttv.net/emote/{eid}/3x"
        except Exception as exc:
            logger.debug("BTTV emote load warning: %s", exc)
        if emotes:
            self.bttv_emotes = emotes

    # ------------------------------------------------------------------
    # Automatic Channel Point Custom Reward Creation (Option A)
    # ------------------------------------------------------------------
    async def sync_channel_point_rewards(self) -> dict[str, Any]:
        """
        Legt alle in `config.yaml` definierten `channel_point`-Trigger automatisch
        als Custom Rewards im Twitch-Kanal an (Option A).
        """
        if not self.broadcaster_id and not await self.fetch_broadcaster_profile():
            return {"ok": False, "error": "Nicht mit Twitch verbunden."}

        cfg = self.cm.get_config()
        triggers = [
            t
            for t in cfg.get("triggers", [])
            if t.get("type") == "channel_point" and t.get("enabled", True)
        ]
        created = []
        errors = []

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Fetch existing rewards
            existing_resp = await client.get(
                f"{TWITCH_HELIX_URL}/channel_points/custom_rewards",
                params={"broadcaster_id": self.broadcaster_id},
                headers=self._helix_headers(),
            )
            existing_titles = set()
            if existing_resp.status_code == 200:
                for rw in existing_resp.json().get("data", []):
                    existing_titles.add(rw.get("title", "").lower())

            for trig in triggers:
                title = (trig.get("title") or trig.get("name", "")).strip()
                cost = max(1, int(trig.get("cost", 1000)))
                if not title or title.lower() in existing_titles:
                    continue

                resp = await client.post(
                    f"{TWITCH_HELIX_URL}/channel_points/custom_rewards",
                    params={"broadcaster_id": self.broadcaster_id},
                    headers=self._helix_headers(),
                    json={
                        "title": title[:45],
                        "cost": cost,
                        "is_enabled": True,
                        "background_color": "#9146FF",
                        "should_redemptions_skip_request_queue": True,
                    },
                )
                if resp.status_code in (200, 201):
                    created.append(title)
                else:
                    errors.append(f"{title}: HTTP {resp.status_code}")

        return {"ok": True, "created": created, "errors": errors}

    # ------------------------------------------------------------------
    # Send Public Chat Reply
    # ------------------------------------------------------------------
    async def send_chat_message(self, message: str) -> None:
        if not self.broadcaster_id:
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{TWITCH_HELIX_URL}/chat/messages",
                    headers=self._helix_headers(),
                    json={
                        "broadcaster_id": self.broadcaster_id,
                        "sender_id": self.broadcaster_id,
                        "message": message[:480],
                    },
                )
        except Exception as exc:
            logger.debug("Chat reply send failed: %s", exc)

    # ------------------------------------------------------------------
    # Outbound EventSub WebSocket Loop (Silent Auto-Reconnect)
    # ------------------------------------------------------------------
    async def start(self) -> None:
        await self.load_bttv_emotes()
        self._stop_event.clear()
        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._eventsub_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()

    async def restart_eventsub(self) -> None:
        await self.stop()
        await asyncio.sleep(0.2)
        await self.start()

    async def _eventsub_loop(self) -> None:
        backoff = 2.0
        while not self._stop_event.is_set():
            env = self.cm.read_env()
            if not env.get("TWITCH_ACCESS_TOKEN") or not env.get("TWITCH_CLIENT_ID"):
                self.connected = False
                await asyncio.sleep(5.0)
                continue

            if not self.broadcaster_id:
                await self.fetch_broadcaster_profile()
                if not self.broadcaster_id:
                    await asyncio.sleep(10.0)
                    continue

            try:
                async with websockets.connect(TWITCH_EVENTSUB_WS_URL, ping_interval=20) as ws:
                    logger.info("Connected to Twitch EventSub WebSocket.")
                    backoff = 2.0
                    async for raw_msg in ws:
                        data = json.loads(raw_msg)
                        metadata = data.get("metadata", {})
                        msg_type = metadata.get("message_type")

                        if msg_type == "session_welcome":
                            self.session_id = data["payload"]["session"]["id"]
                            self.connected = True
                            await self._subscribe_all_events(self.session_id)
                            await self.hub.broadcast_status()

                        elif msg_type == "notification":
                            sub_type = metadata.get("subscription_type", "")
                            event_data = data.get("payload", {}).get("event", {})
                            await self.handle_eventsub_notification(sub_type, event_data)

                        elif msg_type == "session_reconnect":
                            # Twitch requested reconnect
                            break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Twitch EventSub WebSocket disconnected (%s) - reconnecting in %.1fs", exc, backoff)
                self.connected = False
                await self.hub.broadcast_status()
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.5)

    async def _subscribe_all_events(self, session_id: str) -> None:
        subscriptions = [
            ("channel.chat.message", "1", {"broadcaster_user_id": self.broadcaster_id, "user_id": self.broadcaster_id}),
            ("channel.channel_points_custom_reward_redemption.add", "1", {"broadcaster_user_id": self.broadcaster_id}),
            ("channel.cheer", "1", {"broadcaster_user_id": self.broadcaster_id}),
            ("channel.subscribe", "1", {"broadcaster_user_id": self.broadcaster_id}),
            ("channel.subscription.gift", "1", {"broadcaster_user_id": self.broadcaster_id}),
            ("channel.raid", "1", {"to_broadcaster_user_id": self.broadcaster_id}),
            ("channel.follow", "2", {"broadcaster_user_id": self.broadcaster_id, "moderator_user_id": self.broadcaster_id}),
            ("channel.hype_train.begin", "1", {"broadcaster_user_id": self.broadcaster_id}),
            ("channel.hype_train.progress", "1", {"broadcaster_user_id": self.broadcaster_id}),
            ("channel.hype_train.end", "1", {"broadcaster_user_id": self.broadcaster_id}),
        ]
        async with httpx.AsyncClient(timeout=8.0) as client:
            for sub_type, version, condition in subscriptions:
                try:
                    await client.post(
                        f"{TWITCH_HELIX_URL}/eventsub/subscriptions",
                        headers=self._helix_headers(),
                        json={
                            "type": sub_type,
                            "version": version,
                            "condition": condition,
                            "transport": {"method": "websocket", "session_id": session_id},
                        },
                    )
                except Exception as exc:
                    logger.debug("Subscribing to %s failed: %s", sub_type, exc)

    # ------------------------------------------------------------------
    # Event & Chat Matching Logic
    # ------------------------------------------------------------------
    async def handle_eventsub_notification(
        self, sub_type: str, event: dict[str, Any]
    ) -> None:
        if sub_type == "channel.chat.message":
            # Strikte Regel: Nur öffentlicher Kanal-Chat, niemals Whispers!
            if event.get("message_type") == "whisper":
                return
            user = event.get("chatter_user_name") or event.get("chatter_user_login", "Viewer")
            text = event.get("message", {}).get("text", "")
            fragments = event.get("message", {}).get("fragments", [])
            badges = [b.get("set_id", "") for b in event.get("badges", [])]

            roles = ["everyone"]
            if "broadcaster" in badges:
                roles.extend(["broadcaster", "mod", "subscriber", "vip"])
            elif "moderator" in badges:
                roles.extend(["mod", "subscriber"])
            elif "subscriber" in badges or "founder" in badges:
                roles.append("subscriber")
            elif "vip" in badges:
                roles.append("vip")

            emote_urls: list[str] = []
            for frag in fragments:
                if frag.get("type") == "emote":
                    eid = frag.get("emote", {}).get("id")
                    if eid:
                        emote_urls.append(
                            f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/default/dark/3.0"
                        )
            for word in text.split():
                if word in self.bttv_emotes:
                    emote_urls.append(self.bttv_emotes[word])

            await self.process_chat_message(
                user=user,
                text=text,
                user_roles=roles,
                emote_urls=emote_urls,
            )

        elif sub_type == "channel.channel_points_custom_reward_redemption.add":
            user = event.get("user_name", "Viewer")
            reward_title = event.get("reward", {}).get("title", "")
            user_input = event.get("user_input", "")
            await self.process_twitch_event(
                event_type="channel_point",
                user=user,
                title=reward_title,
                user_input=user_input,
            )

        elif sub_type == "channel.cheer":
            user = event.get("user_name") or "Anonymous"
            bits = int(event.get("bits", 100))
            msg = event.get("message", "")
            await self.process_twitch_event(
                event_type="bits",
                user=user,
                amount=bits,
                user_input=msg,
            )

        elif sub_type in ("channel.subscribe", "channel.subscription.gift", "channel.subscription.message"):
            user = event.get("user_name", "Subscriber")
            await self.process_twitch_event(
                event_type="sub",
                user=user,
                user_input=event.get("message", {}).get("text", ""),
            )

        elif sub_type == "channel.raid":
            raider = event.get("from_broadcaster_user_name", "Raider")
            viewers = int(event.get("viewers", 1))
            await self.process_twitch_event(
                event_type="raid",
                user=raider,
                amount=viewers,
            )

        elif sub_type == "channel.follow":
            follower = event.get("user_name", "Follower")
            await self.process_twitch_event(
                event_type="follow",
                user=follower,
            )

        elif sub_type.startswith("channel.hype_train"):
            level = int(event.get("level", 1))
            await self.process_twitch_event(
                event_type="hype_train",
                user=f"Hype-Train Lv.{level}",
                amount=level,
            )

    async def process_chat_message(
        self,
        user: str,
        text: str,
        user_roles: list[str] | None = None,
        emote_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        if user_roles is None:
            user_roles = ["everyone"]
        if emote_urls is None:
            emote_urls = []
            for word in text.split():
                if word in self.bttv_emotes:
                    emote_urls.append(self.bttv_emotes[word])

        await self.hub.register_chat_activity(user=user, message=text, emote_urls=emote_urls)

        cfg = self.cm.get_config()
        prefix = cfg.get("general", {}).get("prefix", "!")
        if "/" in prefix:
            prefix = "!"

        stripped = text.strip()
        matched_triggers: list[str] = []

        for trig in cfg.get("triggers", []):
            if not trig.get("enabled", True):
                continue

            allowed_roles = trig.get("allowed_roles", ["everyone"])
            if "everyone" not in allowed_roles and not any(r in allowed_roles for r in user_roles):
                continue

            t_type = trig.get("type", "")
            pattern = str(trig.get("pattern", "")).strip()
            if not pattern:
                continue

            # 1. Command Matching (z. B. !bonk oder !tts Hallo)
            if t_type == "command":
                cmd_full = f"{prefix}{pattern}".lower()
                parts = stripped.split(maxsplit=1)
                first_token = parts[0].lower() if parts else ""
                if first_token == cmd_full:
                    arg_text = parts[1] if len(parts) > 1 else ""
                    res = await self.hub.execute_trigger(
                        trigger=trig,
                        user=user,
                        user_input=arg_text,
                        chat_reply_callback=self.send_chat_message,
                    )
                    if res.get("ok"):
                        matched_triggers.append(trig.get("id", ""))

            # 2. Chat-Word Matching (Togglebar: Ganzes Wort vs. Substring, immer case-insensitive)
            elif t_type == "chat_word":
                match_mode = trig.get("match_mode", "word")
                if self.matches_word_rule(stripped, pattern, match_mode):
                    res = await self.hub.execute_trigger(
                        trigger=trig,
                        user=user,
                        user_input=stripped,
                        chat_reply_callback=self.send_chat_message,
                    )
                    if res.get("ok"):
                        matched_triggers.append(trig.get("id", ""))

        return {"matched": matched_triggers}

    @staticmethod
    def matches_word_rule(text: str, pattern: str, match_mode: str = "word") -> bool:
        """
        Prüft case-insensitive:
        - `match_mode == "word"`: Nur ganzes Wort (`\\bpattern\\b`).
          Beispiel "rot": "Ampel ist Rot" -> True | "Brot" -> False | "Rote Ampel" -> False
        - `match_mode == "substring"`: Teilwort enthalten.
          Beispiel "rot": "Brot" -> True | "Rote Ampel" -> True | "Ampel ist Rot" -> True | "Ampel ist grün" -> False
        """
        if not text or not pattern:
            return False
        if match_mode == "substring":
            return pattern.lower() in text.lower()
        regex = rf"(?i)\b{re.escape(pattern)}\b"
        return bool(re.search(regex, text))

    async def process_twitch_event(
        self,
        event_type: str,
        user: str = "Viewer",
        title: str = "",
        amount: int = 0,
        user_input: str = "",
    ) -> dict[str, Any]:
        cfg = self.cm.get_config()
        matched: list[str] = []

        for trig in cfg.get("triggers", []):
            if not trig.get("enabled", True):
                continue
            if trig.get("type") != event_type:
                continue

            if event_type == "channel_point":
                trig_title = (trig.get("title") or trig.get("name", "")).strip().lower()
                if title and trig_title and trig_title != title.strip().lower():
                    if trig.get("id", "").lower() != title.strip().lower():
                        continue

            elif event_type == "bits":
                min_bits = int(trig.get("min_bits", 1))
                if amount < min_bits:
                    continue

            elif event_type == "raid":
                min_viewers = int(trig.get("min_viewers", 1))
                if amount < min_viewers:
                    continue

            res = await self.hub.execute_trigger(
                trigger=trig,
                user=user,
                user_input=user_input,
                chat_reply_callback=self.send_chat_message,
            )
            if res.get("ok"):
                matched.append(trig.get("id", ""))

        return {"matched": matched}
