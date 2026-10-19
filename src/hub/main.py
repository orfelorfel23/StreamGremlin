"""
StreamGremlin Hub - Main FastAPI Application (Port 4736)
Stellt das Dashboard (`/admin`), das transparente OBS-Overlay (`/overlay`),
die REST-API, den n8n-Webhook und die Echtzeit-WebSockets bereit.
"""
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.hub.auth import AuthManager, COOKIE_NAME, SESSION_TTL_SEC
from src.hub.automation import AutomationService
from src.hub.config_manager import ConfigManager
from src.hub.sound_synth import ensure_default_sounds
from src.hub.tts_service import TTSService
from src.hub.twitch_client import TwitchManager
from src.hub.ws_hub import AGENT_VERSION, WebSocketHub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("streamgremlin.main")

REPO_ROOT = Path(__file__).resolve().parents[2]
SOUNDS_DIR = Path(os.environ.get("SOUNDS_DIR") or (REPO_ROOT / "sounds"))
TTS_CACHE_DIR = REPO_ROOT / "tts_cache"
WEB_DIR = REPO_ROOT / "src" / "web"

config_manager = ConfigManager(repo_root=REPO_ROOT)
auth_manager = AuthManager(config_manager)
tts_service = TTSService(TTS_CACHE_DIR)
automation_service = AutomationService(config_manager)
ws_hub = WebSocketHub(config_manager, tts_service, automation_service)
twitch_manager = TwitchManager(config_manager, ws_hub)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_default_sounds(SOUNDS_DIR)
    await twitch_manager.start()
    yield
    await twitch_manager.stop()


app = FastAPI(
    title="StreamGremlin Hub",
    description="Interactive Twitch Chat, Overlay & Input Chaos Engine",
    version="1.0.0",
    lifespan=lifespan,
)

SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/sounds", StaticFiles(directory=str(SOUNDS_DIR)), name="sounds")
app.mount("/api/tts/cache", StaticFiles(directory=str(TTS_CACHE_DIR)), name="tts_cache")


# ==============================================================================
# HTML & Asset Routes
# ==============================================================================
@app.get("/api/health")
async def health_check() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "StreamGremlin",
        "version": "1.0.0",
        "port": 4736,
    }


@app.get("/assets/icon.png")
@app.get("/favicon.ico")
async def serve_icon() -> Response:
    icon_path = REPO_ROOT / "icon.png"
    if icon_path.exists():
        return FileResponse(str(icon_path), media_type="image/png")
    raise HTTPException(status_code=404, detail="Icon not found")


@app.get("/", response_class=HTMLResponse)
@app.get("/login", response_class=HTMLResponse)
@app.get("/deck", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
async def serve_dashboard() -> HTMLResponse:
    index_file = WEB_DIR / "dashboard" / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>StreamGremlin Dashboard loading...</h1>")
    return HTMLResponse(
        content=index_file.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/overlay", response_class=HTMLResponse)
async def serve_overlay() -> HTMLResponse:
    overlay_file = WEB_DIR / "overlay" / "index.html"
    if not overlay_file.exists():
        return HTMLResponse("<h1>StreamGremlin Overlay loading...</h1>")
    return HTMLResponse(
        content=overlay_file.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


# ==============================================================================
# Authentication & 3-Role Hierarchy Endpoints
# ==============================================================================
@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    setup_required = auth_manager.is_setup_required()
    token = request.cookies.get(COOKIE_NAME)
    session = auth_manager.verify_session_token(token)
    if not session:
        return {
            "authenticated": False,
            "role": None,
            "setup_required": setup_required,
        }
    role = session["role"]
    cfg = config_manager.get_config()
    perms = cfg.get("auth", {}).get("permissions", {}).get(role, {})
    return {
        "authenticated": True,
        "role": role,
        "setup_required": setup_required,
        "permissions": perms if role != "admin" else {"full_access": True},
    }


@app.post("/api/auth/setup")
async def auth_initial_setup(request: Request, response: Response) -> dict[str, Any]:
    body = await request.json()
    try:
        creds = auth_manager.initial_setup(
            admin_password=body.get("admin_password", ""),
            streamer_password=body.get("streamer_password"),
            mod_password=body.get("mod_password"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    token = auth_manager.create_session_token("admin")
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SEC,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True, "role": "admin", "generated_credentials": creds}


@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response) -> dict[str, Any]:
    body = await request.json()
    password = body.get("password", "")
    role = auth_manager.authenticate_by_password(password)
    if not role:
        raise HTTPException(status_code=401, detail="Ungültiges Passwort.")

    token = auth_manager.create_session_token(role)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SEC,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True, "role": role}


@app.post("/api/auth/logout")
async def auth_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.post("/api/auth/change-password")
async def auth_change_password(request: Request) -> dict[str, Any]:
    caller_role = auth_manager.require_roles(request, ["admin", "streamer"])
    body = await request.json()
    target_role = body.get("target_role", "")
    new_password = body.get("new_password", "")
    current_password = body.get("current_password")

    try:
        auth_manager.change_password(
            caller_role=caller_role,
            target_role=target_role,
            new_password=new_password,
            current_password=current_password,
        )
        return {"ok": True, "target_role": target_role}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ==============================================================================
# Configuration, Secrets & Sound Manager
# ==============================================================================
@app.get("/api/config")
async def get_config(request: Request) -> dict[str, Any]:
    role = auth_manager.require_roles(request, ["admin", "streamer", "mod"])
    return {
        "role": role,
        "instance_id": config_manager.instance_id,
        "config": config_manager.filter_for_role(role),
        "sounds_list": sorted(
            [f.name for f in SOUNDS_DIR.iterdir() if f.suffix.lower() in (".mp3", ".wav")]
        ),
        "status": {
            **ws_hub.get_status_summary(),
            "twitch_connected": twitch_manager.connected,
            "twitch_broadcaster": twitch_manager.broadcaster_login,
        },
    }


@app.put("/api/config")
async def update_config(request: Request) -> dict[str, Any]:
    role = auth_manager.require_roles(request, ["admin", "streamer"])
    body = await request.json()
    current = config_manager.get_config(force_reload=True)

    if role == "streamer":
        perms = current.get("auth", {}).get("permissions", {}).get("streamer", {})
        if not perms.get("can_edit_cooldowns", True):
            raise HTTPException(status_code=403, detail="Cooldown-Bearbeitung nicht gestattet.")
        config_manager.save_streamer_overrides(body)
        return {"ok": True, "role": role, "config": config_manager.filter_for_role(role)}

    # Admin: updates global config.yaml
    auth_hashes = {
        "admin_password_hash": current.get("auth", {}).get("admin_password_hash", ""),
        "streamer_password_hash": current.get("auth", {}).get("streamer_password_hash", ""),
        "mod_password_hash": current.get("auth", {}).get("mod_password_hash", ""),
    }

    body.setdefault("auth", {}).update(auth_hashes)
    try:
        saved = config_manager.save_config(body)
        return {"ok": True, "role": role, "config": config_manager.filter_for_role(role)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/secrets")
async def update_secrets(request: Request) -> dict[str, Any]:
    role = auth_manager.require_roles(request, ["admin", "streamer"])
    body = await request.json()
    allowed_keys = {
        "TWITCH_CLIENT_ID",
        "TWITCH_CLIENT_SECRET",
        "TWITCH_BROADCASTER_CHANNEL",
        "TWITCH_BOT_CHANNEL",
    }
    if role == "admin":
        allowed_keys.update({"AGENT_TOKEN", "WEBHOOK_TOKEN", "HA_BASE_URL", "HA_ACCESS_TOKEN", "N8N_WEBHOOK_URL"})

    updates = {k: str(v).strip() for k, v in body.items() if k in allowed_keys and v is not None}
    age_res = config_manager.write_env(updates, encrypt_age=True)
    return {"ok": True, "age_encryption": age_res}


@app.get("/api/sounds")
async def list_sounds(request: Request) -> dict[str, Any]:
    auth_manager.require_roles(request, ["admin", "streamer", "mod"])
    cfg = config_manager.get_config()
    sound_Meta = cfg.get("sounds", {})
    files = []
    for f in sorted(SOUNDS_DIR.iterdir()):
        if f.suffix.lower() in (".mp3", ".wav"):
            files.append({
                "name": f.name,
                "size_kb": round(f.stat().st_size / 1024, 1),
                "volume": float(sound_Meta.get(f.name, {}).get("volume", 0.85)),
                "url": f"/sounds/{f.name}",
            })
    return {"sounds": files}


@app.post("/api/sounds/upload")
async def upload_sound(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    role = auth_manager.require_roles(request, ["admin", "streamer"])
    cfg = config_manager.get_config()
    if role == "streamer" and not cfg.get("auth", {}).get("permissions", {}).get("streamer", {}).get("can_upload_sounds", True):
        raise HTTPException(status_code=403, detail="Sound-Upload nicht gestattet.")

    raw_name = file.filename or "sound.mp3"
    ext = Path(raw_name).suffix.lower()
    if ext not in (".mp3", ".wav"):
        raise HTTPException(status_code=400, detail="Nur .mp3 und .wav Dateien sind erlaubt.")

    safe_stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", Path(raw_name).stem)
    safe_name = f"{safe_stem}{ext}"
    dest = SOUNDS_DIR / safe_name

    content = await file.read()
    dest.write_bytes(content)

    cfg.setdefault("sounds", {}).setdefault(safe_name, {"volume": 0.85})
    config_manager.save_config(cfg)

    await ws_hub.log_event("sound_upload", f"Sound hochgeladen: {safe_name}", user="Dashboard")
    return {"ok": True, "filename": safe_name, "size_kb": round(len(content) / 1024, 1)}


@app.delete("/api/sounds/{filename}")
async def delete_sound(filename: str, request: Request) -> dict[str, Any]:
    auth_manager.require_roles(request, ["admin", "streamer"])
    safe_name = Path(filename).name
    target = SOUNDS_DIR / safe_name
    if target.exists():
        target.unlink()
    cfg = config_manager.get_config()
    cfg.get("sounds", {}).pop(safe_name, None)
    config_manager.save_config(cfg)
    return {"ok": True, "deleted": safe_name}


# ==============================================================================
# Live Controls, Simulator, Panic & Webhooks
# ==============================================================================
@app.post("/api/panic")
async def api_panic(request: Request) -> dict[str, Any]:
    auth_manager.require_roles(request, ["admin", "streamer", "mod"])
    return await ws_hub.trigger_panic(source="Dashboard")


@app.post("/api/mute")
async def api_toggle_mute(request: Request) -> dict[str, Any]:
    auth_manager.require_roles(request, ["admin", "streamer", "mod"])
    ws_hub.master_muted = not ws_hub.master_muted
    if ws_hub.master_muted:
        await ws_hub.broadcast_overlay("stop_audio", {})
    await ws_hub.broadcast_status()
    return {"ok": True, "master_muted": ws_hub.master_muted}


@app.post("/api/test/trigger")
async def api_test_trigger(request: Request) -> dict[str, Any]:
    """Offline-Simulator: Löst entweder einen gespeicherten Trigger oder eine direkte Einzel-Aktion aus."""
    role = auth_manager.require_roles(request, ["admin", "streamer", "mod"])
    cfg = config_manager.get_config()
    if role == "mod" and not cfg.get("auth", {}).get("permissions", {}).get("mod", {}).get("can_trigger_tests", True):
        raise HTTPException(status_code=403, detail="Test-Trigger nicht gestattet.")

    body = await request.json()
    trigger_id = body.get("trigger_id")
    direct_action = body.get("action")
    user_name = body.get("user", "Simulator")
    user_input = body.get("user_input", "Hallo StreamGremlin!")

    if trigger_id:
        for trig in cfg.get("triggers", []):
            if trig.get("id") == trigger_id:
                return await ws_hub.execute_trigger(
                    trigger=trig,
                    user=user_name,
                    user_input=user_input,
                    bypass_cooldown=True,
                )
        raise HTTPException(status_code=404, detail=f"Trigger '{trigger_id}' nicht gefunden.")

    if direct_action:
        await ws_hub.log_event(
            event_type="simulator",
            title=f"Test: {direct_action.get('type')}",
            user=user_name,
            details=user_input[:60],
        )
        await ws_hub.execute_single_action(
            action=direct_action,
            user=user_name,
            user_input=user_input,
            cfg=cfg,
        )
        return {"ok": True, "simulated": direct_action.get("type")}

    raise HTTPException(status_code=400, detail="Weder 'trigger_id' noch 'action' angegeben.")


@app.post("/api/test/chat")
async def api_test_chat(request: Request) -> dict[str, Any]:
    """Simuliert eine eingehende öffentliche Twitch-Chat-Nachricht inkl. Wort-Matching."""
    auth_manager.require_roles(request, ["admin", "streamer", "mod"])
    body = await request.json()
    user = body.get("user", "TestViewer")
    text = body.get("text", "!bonk")
    roles = body.get("roles", ["everyone", "subscriber", "mod", "broadcaster"])
    return await twitch_manager.process_chat_message(user=user, text=text, user_roles=roles)


@app.get("/api/events/recent")
async def api_recent_events(request: Request) -> dict[str, Any]:
    auth_manager.require_roles(request, ["admin", "streamer", "mod"])
    return {"events": list(ws_hub.recent_events)}


@app.post("/api/webhook/trigger")
async def n8n_inbound_webhook(request: Request) -> dict[str, Any]:
    """
    Inbound-Webhook für n8n / Home Assistant / StreamDeck.
    Authentifizierung per Header `X-Webhook-Token` oder `Authorization: Bearer <WEBHOOK_TOKEN>`.
    """
    env = config_manager.read_env()
    expected = env.get("WEBHOOK_TOKEN", "").strip()
    token = request.headers.get("X-Webhook-Token", "").strip()
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="Ungültiges WEBHOOK_TOKEN.")

    body = await request.json()
    trigger_id = body.get("trigger_id")
    action = body.get("action")
    user = body.get("user", "n8n-Automation")
    user_input = body.get("user_input", "")

    if trigger_id:
        cfg = config_manager.get_config()
        for trig in cfg.get("triggers", []):
            if trig.get("id") == trigger_id:
                return await ws_hub.execute_trigger(
                    trigger=trig,
                    user=user,
                    user_input=user_input,
                    bypass_cooldown=True,
                )
    if action:
        await ws_hub.log_event("n8n_webhook", f"n8n: {action.get('type')}", user=user)
        await ws_hub.execute_single_action(action=action, user=user, user_input=user_input)
        return {"ok": True}

    raise HTTPException(status_code=400, detail="Bitte 'trigger_id' oder 'action' übergeben.")


# ==============================================================================
# Twitch OAuth & Channel Points Endpoints
# ==============================================================================
@app.get("/api/twitch/auth-url")
async def twitch_auth_url(request: Request) -> dict[str, str]:
    auth_manager.require_roles(request, ["admin", "streamer"])
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/twitch/callback"
    return {"auth_url": twitch_manager.get_oauth_url(redirect_uri), "redirect_uri": redirect_uri}


@app.get("/api/twitch/callback")
async def twitch_oauth_callback(request: Request, code: str = "") -> RedirectResponse:
    if not code:
        return RedirectResponse(url="/?twitch=error")
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/twitch/callback"
    try:
        await twitch_manager.exchange_code(code=code, redirect_uri=redirect_uri)
        return RedirectResponse(url="/?twitch=connected")
    except Exception as exc:
        logger.error("Twitch OAuth callback error: %s", exc)
        return RedirectResponse(url="/?twitch=error")


@app.post("/api/twitch/sync-rewards")
async def twitch_sync_rewards(request: Request) -> dict[str, Any]:
    auth_manager.require_roles(request, ["admin", "streamer"])
    return await twitch_manager.sync_channel_point_rewards()


# ==============================================================================
# Windows Companion Agent Update & Config Download
# ==============================================================================
@app.get("/api/agent/version")
async def agent_version_check(request: Request) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    return {
        "latest_version": AGENT_VERSION,
        "download_url": f"{base}/api/agent/download",
    }


@app.post("/api/agent/trigger-update")
async def api_trigger_agent_update(request: Request) -> dict[str, Any]:
    """Sendet ein update_agent Signal an alle verbundenen Gaming-PC Companion Agents."""
    auth_manager.require_roles(request, ["admin", "streamer"])
    await ws_hub.broadcast_agent({"action": "update_agent"})
    return {"ok": True, "message": "Update-Signal an verbundene Agents übermittelt."}


@app.get("/api/agent/config-download")
async def agent_config_download(request: Request) -> Response:
    auth_manager.require_roles(request, ["admin", "streamer"])
    env = config_manager.read_env()
    host = request.headers.get("host", "localhost:4736")
    scheme = "wss" if request.url.scheme == "https" or "orfel.de" in host else "ws"
    agent_cfg = {
        "hub_url": f"{'https' if scheme == 'wss' else 'http'}://{host}",
        "hub_http_url": f"{'https' if scheme == 'wss' else 'http'}://{host}",
        "hub_ws_url": f"{scheme}://{host}/ws/agent",
        "token": env.get("AGENT_TOKEN", ""),
        "agent_token": env.get("AGENT_TOKEN", ""),
        "auto_update": True,
        "panic_hotkey": "esc_3x",
    }
    return Response(
        content=json.dumps(agent_cfg, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="agent_config.json"'},
    )


@app.get("/api/agent/download")
async def agent_script_download() -> Response:
    exe_path = REPO_ROOT / "dist" / "StreamGremlin.exe"
    if exe_path.exists():
        return FileResponse(
            str(exe_path),
            media_type="application/octet-stream",
            filename="StreamGremlin.exe",
        )
    py_path = REPO_ROOT / "src" / "agent" / "agent.py"
    if py_path.exists():
        return FileResponse(
            str(py_path),
            media_type="text/x-python",
            filename="StreamGremlin-Agent.py",
        )
    raise HTTPException(status_code=404, detail="Agent binary not found")


# ==============================================================================
# Real-Time WebSockets (/ws/overlay, /ws/dashboard, /ws/agent)
# ==============================================================================
@app.websocket("/ws/overlay")
async def ws_overlay_endpoint(ws: WebSocket) -> None:
    await ws_hub.connect_overlay(ws)
    try:
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_json({"event": "pong", "payload": {}})
    except WebSocketDisconnect:
        pass
    finally:
        ws_hub.disconnect_overlay(ws)
        await ws_hub.broadcast_status()


@app.websocket("/ws/dashboard")
async def ws_dashboard_endpoint(ws: WebSocket) -> None:
    await ws_hub.connect_dashboard(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_hub.disconnect_dashboard(ws)


@app.websocket("/ws/agent")
async def ws_agent_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    env = config_manager.read_env()
    expected_token = env.get("AGENT_TOKEN", "").strip()
    authenticated = False

    try:
        # First message must be auth handshake
        raw = await ws.receive_text()
        handshake = json.loads(raw)
        if handshake.get("action") != "auth" or handshake.get("token") != expected_token:
            await ws.send_json({"status": "error", "message": "Invalid AGENT_TOKEN"})
            await ws.close(code=4001)
            return

        authenticated = True
        client_ver = handshake.get("version", "1.0.0")
        await ws.send_json({
            "status": "authenticated",
            "latest_version": AGENT_VERSION,
            "update_available": client_ver != AGENT_VERSION,
            "download_url": "/api/agent/download" if client_ver != AGENT_VERSION else None,
        })
        await ws_hub.connect_agent(
            ws,
            {
                "version": client_ver,
                "hostname": handshake.get("hostname", "Gaming-PC"),
            },
        )

        while True:
            raw_msg = await ws.receive_text()
            data = json.loads(raw_msg)
            act = data.get("action")
            if act == "panic_pressed":
                # Streamer pressed ESC 3x on physical keyboard -> abort everything & sync overlay!
                await ws_hub.trigger_panic(source="Gaming-PC Tastatur (ESC 3x)")
            elif act == "ping":
                await ws.send_json({"action": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("Agent WS closed: %s", exc)
    finally:
        if authenticated:
            ws_hub.disconnect_agent(ws)
            await ws_hub.broadcast_status()
