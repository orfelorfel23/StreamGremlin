"""
StreamGremlin Smart-Home (Home Assistant) & n8n Automatisierungs-Service
1. Outbound Event-Forwarding an n8n (`n8n_webhook_url`)
2. Direkte Home Assistant Lichtsteuerung (`smart_home`) mit automatischem
   Zustands-Backup und zeitgesteuertem Restore zur vorherigen Lichtszene.
"""
import asyncio
import logging
import os
import time
from typing import Any
import httpx

from src.hub.config_manager import ConfigManager

logger = logging.getLogger("streamgremlin.automation")


class AutomationService:
    def __init__(self, config_manager: ConfigManager) -> None:
        self.cm = config_manager
        self._restore_tasks: dict[str, asyncio.Task[None]] = {}

    async def forward_event_to_n8n(self, event_type: str, data: dict[str, Any]) -> None:
        """Pusht ausgelöste StreamGremlin-Events in Echtzeit an n8n."""
        cfg = self.cm.get_config()
        webhook_url = (
            os.environ.get("N8N_WEBHOOK_URL")
            or cfg.get("general", {}).get("n8n_webhook_url", "")
        ).strip()
        if not webhook_url:
            return

        payload = {
            "source": "StreamGremlin",
            "timestamp": int(time.time()),
            "event_type": event_type,
            "data": data,
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(webhook_url, json=payload)
        except Exception as exc:
            logger.warning("n8n webhook forwarding failed: %s", exc)

    async def execute_smart_home_action(self, action: dict[str, Any], user: str) -> dict[str, Any]:
        """
        Schaltet eine Home Assistant Entität (z. B. `light.gaming_zimmer`) auf
        die gewünschte Farbe/Helligkeit und stellt nach `duration_sec` den
        ursprünglichen Zustand wieder her.
        """
        cfg = self.cm.get_config()
        ha_cfg = cfg.get("general", {}).get("home_assistant", {})
        base_url = (
            os.environ.get("HA_BASE_URL")
            or ha_cfg.get("url")
            or "https://home.orfel.de"
        ).rstrip("/")
        token = os.environ.get("HA_ACCESS_TOKEN", "").strip()
        entity_id = action.get("entity_id") or ha_cfg.get("default_entity", "light.gaming_zimmer")
        rgb_color = action.get("rgb_color", [255, 0, 0])
        brightness = int(action.get("brightness", 255))
        duration_sec = int(action.get("duration_sec", 15))

        # Auch an n8n weiterleiten (falls Smart-Home über n8n geroutet wird)
        await self.forward_event_to_n8n(
            "smart_home_light",
            {
                "entity_id": entity_id,
                "rgb_color": rgb_color,
                "brightness": brightness,
                "duration_sec": duration_sec,
                "triggered_by": user,
            },
        )

        if not token:
            logger.info(
                "Smart-Home Action (%s -> %s for %ds) dispatched via webhook/simulator (no direct HA_ACCESS_TOKEN set).",
                entity_id,
                rgb_color,
                duration_sec,
            )
            return {"status": "dispatched_via_webhook", "entity_id": entity_id}

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        prev_state: dict[str, Any] | None = None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # 1. Aktuellen Zustand sichern
                state_resp = await client.get(f"{base_url}/api/states/{entity_id}", headers=headers)
                if state_resp.status_code == 200:
                    prev_state = state_resp.json()

                # 2. Neue Farbe setzen
                domain = entity_id.split(".")[0] if "." in entity_id else "light"
                await client.post(
                    f"{base_url}/api/services/{domain}/turn_on",
                    headers=headers,
                    json={
                        "entity_id": entity_id,
                        "rgb_color": rgb_color,
                        "brightness": brightness,
                    },
                )
        except Exception as exc:
            logger.warning("Home Assistant API call failed: %s", exc)
            return {"status": "error", "error": str(exc)}

        # 3. Nach Ablauf von duration_sec automatisch vorherigen Zustand wiederherstellen
        if duration_sec > 0 and prev_state is not None:
            if entity_id in self._restore_tasks:
                self._restore_tasks[entity_id].cancel()

            async def _restore() -> None:
                await asyncio.sleep(duration_sec)
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        domain = entity_id.split(".")[0] if "." in entity_id else "light"
                        if prev_state.get("state") == "off":
                            await client.post(
                                f"{base_url}/api/services/{domain}/turn_off",
                                headers=headers,
                                json={"entity_id": entity_id},
                            )
                        else:
                            attrs = prev_state.get("attributes", {})
                            restore_payload: dict[str, Any] = {"entity_id": entity_id}
                            if "rgb_color" in attrs:
                                restore_payload["rgb_color"] = attrs["rgb_color"]
                            if "brightness" in attrs:
                                restore_payload["brightness"] = attrs["brightness"]
                            await client.post(
                                f"{base_url}/api/services/{domain}/turn_on",
                                headers=headers,
                                json=restore_payload,
                            )
                except Exception as exc:
                    logger.warning("Failed to restore HA state for %s: %s", entity_id, exc)

            self._restore_tasks[entity_id] = asyncio.create_task(_restore())

        return {"status": "ok", "entity_id": entity_id, "duration_sec": duration_sec}
