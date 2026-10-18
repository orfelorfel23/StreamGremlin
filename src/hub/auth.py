"""
StreamGremlin 3-Stufen-Rollen & Passwort-Sicherheitssystem
Unterstützt:
1. Admin (Vollzugriff, verwaltet alle Passwörter)
2. Streamer (Stream-Steuerung, verwaltet eigenes & Mod-Passwort)
3. Mod (Live-Assistenz, keine Passwort-Änderung)
Speichert Passwörter ausschließlich als gesalzene `bcrypt`-Hashes.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any
import bcrypt
from fastapi import HTTPException, Request

from src.hub.config_manager import ConfigManager

COOKIE_NAME = "sg_session"
SESSION_TTL_SEC = 86400 * 7  # 7 Tage


def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


class AuthManager:
    def __init__(self, config_manager: ConfigManager) -> None:
        self.cm = config_manager
    def is_setup_required(self) -> bool:
        """Setup wird über .env Passwörter gesteuert, daher immer False."""
        return False

    def initial_setup(
        self,
        admin_password: str,
        streamer_password: str | None = None,
        mod_password: str | None = None,
    ) -> dict[str, str]:
        if len(admin_password) < 4:
            raise ValueError("Passwort muss mindestens 4 Zeichen lang sein.")

        streamer_pw = streamer_password or f"streamer-{secrets.token_hex(4)}"
        mod_pw = mod_password or f"mod-{secrets.token_hex(4)}"

        cfg = self.cm.get_config(force_reload=True)
        auth_cfg = cfg.setdefault("auth", {})
        auth_cfg["admin_password_hash"] = hash_password(admin_password)
        auth_cfg["streamer_password_hash"] = hash_password(streamer_pw)
        auth_cfg["mod_password_hash"] = hash_password(mod_pw)
        self.cm.save_config(cfg)

        return {
            "admin_password": admin_password,
            "streamer_password": streamer_pw,
            "mod_password": mod_pw,
        }

    def authenticate_by_password(self, password: str) -> str | None:
        """
        Prüft das Passwort zuerst gegen .env, dann als Fallback gegen config.yaml.
        Gibt 'admin', 'streamer', 'mod' oder None zurück.
        """
        if not password:
            return None

        # 1. Check passwords from .env
        env = self.cm.read_env()
        admin_env = env.get("ADMIN_PASSWORD")
        streamer_env = env.get("STREAMER_PASSWORD")
        mod_env = env.get("MOD_PASSWORD")

        if admin_env and password == admin_env:
            return "admin"
        if streamer_env and password == streamer_env:
            return "streamer"
        if mod_env and password == mod_env:
            return "mod"

        # 2. Check config hashes (fallback)
        cfg = self.cm.get_config()
        auth_cfg = cfg.get("auth", {})
        if verify_password(password, auth_cfg.get("admin_password_hash", "")):
            return "admin"
        if verify_password(password, auth_cfg.get("streamer_password_hash", "")):
            return "streamer"
        if verify_password(password, auth_cfg.get("mod_password_hash", "")):
            return "mod"

        return None

    def change_password(
        self,
        caller_role: str,
        target_role: str,
        new_password: str,
        current_password: str | None = None,
    ) -> None:
        """
        Hierarchische Passwortänderung:
        - Admin: Kann admin (mit altem PW), streamer und mod ändern.
        - Streamer: Kann streamer (mit altem PW) und mod ändern.
        - Mod: Darf keine Passwörter ändern.
        """
        if len(new_password) < 4:
            raise ValueError("Das neue Passwort muss mindestens 4 Zeichen lang sein.")

        env = self.cm.read_env()
        admin_env = env.get("ADMIN_PASSWORD")
        streamer_env = env.get("STREAMER_PASSWORD")

        cfg = self.cm.get_config(force_reload=True)
        auth_cfg = cfg.setdefault("auth", {})

        if caller_role == "admin":
            if target_role == "admin":
                valid_current = False
                if admin_env and current_password == admin_env:
                    valid_current = True
                elif current_password and verify_password(
                    current_password, auth_cfg.get("admin_password_hash", "")
                ):
                    valid_current = True
                if not valid_current:
                    raise PermissionError("Aktuelles Admin-Passwort ist falsch.")
                self.cm.write_env({"ADMIN_PASSWORD": new_password}, encrypt_age=True)
            elif target_role == "streamer":
                self.cm.write_env({"STREAMER_PASSWORD": new_password}, encrypt_age=True)
            elif target_role == "mod":
                self.cm.write_env({"MOD_PASSWORD": new_password}, encrypt_age=True)
            else:
                raise ValueError(f"Unbekannte Ziel-Rolle: {target_role}")

        elif caller_role == "streamer":
            if target_role == "streamer":
                valid_current = False
                if streamer_env and current_password == streamer_env:
                    valid_current = True
                elif current_password and verify_password(
                    current_password, auth_cfg.get("streamer_password_hash", "")
                ):
                    valid_current = True
                if not valid_current:
                    raise PermissionError("Aktuelles Streamer-Passwort ist falsch.")
                self.cm.write_env({"STREAMER_PASSWORD": new_password}, encrypt_age=True)
            elif target_role == "mod":
                self.cm.write_env({"MOD_PASSWORD": new_password}, encrypt_age=True)
            else:
                raise PermissionError("Keine Berechtigung zur Passwortänderung.")

        else:
            raise PermissionError("Keine Berechtigung zur Passwortänderung.")

    def reset_admin_password_cli(self, new_password: str | None = None) -> str:
        """Notfall-Reset über die Server-Konsole (CLI)."""
        pw = new_password or f"admin-{secrets.token_hex(6)}"
        cfg = self.cm.get_config(force_reload=True)
        auth_cfg = cfg.setdefault("auth", {})
        auth_cfg["admin_password_hash"] = hash_password(pw)
        if not auth_cfg.get("streamer_password_hash"):
            auth_cfg["streamer_password_hash"] = hash_password(f"streamer-{secrets.token_hex(4)}")
        if not auth_cfg.get("mod_password_hash"):
            auth_cfg["mod_password_hash"] = hash_password(f"mod-{secrets.token_hex(4)}")
        self.cm.save_config(cfg)
        return pw

    def _get_secret(self) -> bytes:
        secret = os.environ.get("SESSION_SECRET", "streamgremlin-fallback-secret")
        return secret.encode("utf-8")

    def create_session_token(self, role: str) -> str:
        payload = {
            "role": role,
            "exp": int(time.time()) + SESSION_TTL_SEC,
            "nonce": secrets.token_hex(8),
        }
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
        sig = hmac.new(self._get_secret(), raw.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{raw}.{sig}"

    def verify_session_token(self, token: str | None) -> dict[str, Any] | None:
        if not token or "." not in token:
            return None
        try:
            raw, sig = token.rsplit(".", 1)
            expected_sig = hmac.new(
                self._get_secret(), raw.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                return None
            payload = json.loads(base64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8"))
            if payload.get("exp", 0) < time.time():
                return None
            if payload.get("role") not in ("admin", "streamer", "mod"):
                return None
            return payload
        except Exception:
            return None

    def get_current_role(self, request: Request) -> str:
        token = request.cookies.get(COOKIE_NAME)
        session = self.verify_session_token(token)
        if not session:
            raise HTTPException(status_code=401, detail="Nicht angemeldet.")
        return session["role"]

    def require_roles(self, request: Request, allowed: list[str]) -> str:
        role = self.get_current_role(request)
        if role not in allowed:
            raise HTTPException(
                status_code=403,
                detail="Zugriff verweigert.",
            )
        return role
