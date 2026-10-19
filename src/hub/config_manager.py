"""
StreamGremlin Configuration & Secret Manager
Verwaltet `config.yaml` (mit Hot-Reloading), validiert Sicherheitsregeln
(z. B. Verbot von `/` als Command-Präfix) und verschlüsselt `.env` mit `age`
in `.env.age` für Rebuild-Sicherheit im Git.
"""
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any
import yaml


class ConfigManager:
    def __init__(
        self,
        config_path: str | Path | None = None,
        repo_root: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[2])
        self.config_path = Path(
            config_path
            or os.environ.get("CONFIG_PATH")
            or (self.repo_root / "config" / "config.yaml")
        )
        self.example_path = self.repo_root / "config" / "config.example.yaml"
        self.env_path = Path(
            os.environ.get("ENV_PATH")
            or (self.repo_root / ".env")
        )
        self.env_age_path = Path(
            os.environ.get("ENV_AGE_PATH")
            or (self.repo_root / ".env.age")
        )
        self.age_pub_key_path = Path(
            os.environ.get("AGE_PUBLIC_KEY_FILE")
            or (self.repo_root / "keys" / "age-public-key.txt")
        )
        self.instance_id = str(os.environ.get("INSTANCE_ID") or "main").strip()
        self.overrides_path = Path(
            os.environ.get("OVERRIDES_PATH")
            or (self.config_path.parent / f"overrides.{self.instance_id}.yaml")
        )
        self._last_mtime: float = 0.0
        self._last_overrides_mtime: float = 0.0
        self._cache: dict[str, Any] = {}
        self.ensure_config_exists()
        self.ensure_env_exists()

    def ensure_config_exists(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists() and self.example_path.exists():
            shutil.copyfile(self.example_path, self.config_path)

    def ensure_env_exists(self) -> None:
        """Lädt oder initialisiert `.env` und stellt sichere Tokens bereit."""
        env_vars = self.read_env()
        changed = False
        if not env_vars.get("SESSION_SECRET"):
            env_vars["SESSION_SECRET"] = secrets.token_hex(24)
            changed = True
        if not env_vars.get("AGENT_TOKEN"):
            env_vars["AGENT_TOKEN"] = f"sg_agent_{secrets.token_hex(16)}"
            changed = True
        if not env_vars.get("WEBHOOK_TOKEN"):
            env_vars["WEBHOOK_TOKEN"] = f"sg_hook_{secrets.token_hex(16)}"
            changed = True
        if "PORT" not in env_vars:
            env_vars["PORT"] = "4736"
            changed = True
        if not env_vars.get("ADMIN_PASSWORD"):
            env_vars["ADMIN_PASSWORD"] = "admin"
            changed = True
        if not env_vars.get("STREAMER_PASSWORD"):
            env_vars["STREAMER_PASSWORD"] = "streamer"
            changed = True
        if not env_vars.get("MOD_PASSWORD"):
            env_vars["MOD_PASSWORD"] = "mod"
            changed = True
        if changed or not self.env_path.exists():
            self.write_env(env_vars, encrypt_age=True)
        # Apply to current process environment
        for k, v in env_vars.items():
            if k not in os.environ or not os.environ[k]:
                os.environ[k] = v

    def read_env(self) -> dict[str, str]:
        data: dict[str, str] = {}
        if self.env_path.exists():
            for line in self.env_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                k, v = stripped.split("=", 1)
                data[k.strip()] = v.strip()
        # Fallback to environment variables
        for key in [
            "PORT",
            "SESSION_SECRET",
            "AGENT_TOKEN",
            "WEBHOOK_TOKEN",
            "ADMIN_PASSWORD",
            "STREAMER_PASSWORD",
            "MOD_PASSWORD",
            "TWITCH_CLIENT_ID",
            "TWITCH_CLIENT_SECRET",
            "TWITCH_BROADCASTER_CHANNEL",
            "TWITCH_BOT_CHANNEL",
            "TWITCH_ACCESS_TOKEN",
            "TWITCH_REFRESH_TOKEN",
            "HA_BASE_URL",
            "HA_ACCESS_TOKEN",
            "N8N_WEBHOOK_URL",
        ]:
            if key not in data and os.environ.get(key):
                data[key] = os.environ[key]
        return data

    def write_env(self, updates: dict[str, str], encrypt_age: bool = True) -> dict[str, Any]:
        current = self.read_env()
        current.update({k: str(v) for k, v in updates.items() if v is not None})
        lines = [
            "# StreamGremlin Environment Secrets (Auto-Managed)",
            "# Plaintext .env is git-ignored; .env.age is encrypted for Git.",
        ]
        for k, v in current.items():
            lines.append(f"{k}={v}")
            os.environ[k] = str(v)
        self.env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        age_status = {"encrypted": False, "file": str(self.env_age_path), "error": None}
        if encrypt_age:
            age_status = self.encrypt_env_with_age()
        return age_status

    def encrypt_env_with_age(self) -> dict[str, Any]:
        """Verschlüsselt `.env` mit dem öffentlichen Jannik-Cloud `age`-Schlüssel nach `.env.age`."""
        if not self.age_pub_key_path.exists():
            return {"encrypted": False, "error": "age-public-key.txt not found"}
        pub_key = self.age_pub_key_path.read_text(encoding="utf-8").strip()
        if not pub_key:
            return {"encrypted": False, "error": "age-public-key.txt is empty"}

        age_bin = shutil.which("age")
        if age_bin:
            try:
                subprocess.run(
                    [age_bin, "-r", pub_key, "-o", str(self.env_age_path), str(self.env_path)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return {"encrypted": True, "file": str(self.env_age_path), "recipient": pub_key}
            except Exception as exc:
                return {"encrypted": False, "error": str(exc)}
        return {
            "encrypted": False,
            "error": "age binary not installed on host (will encrypt inside Docker container or via generate-env.sh)",
            "recipient": pub_key,
        }

    def get_config(self, force_reload: bool = False) -> dict[str, Any]:
        """Lädt `config.yaml` mit automatischem Hot-Reloading und merged Instanz-Overrides."""
        try:
            mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            self.ensure_config_exists()
            mtime = self.config_path.stat().st_mtime if self.config_path.exists() else 0.0

        try:
            ovr_mtime = self.overrides_path.stat().st_mtime if self.overrides_path.exists() else 0.0
        except FileNotFoundError:
            ovr_mtime = 0.0

        if (
            force_reload
            or not self._cache
            or mtime > self._last_mtime
            or ovr_mtime > self._last_overrides_mtime
        ):
            if self.config_path.exists():
                raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            else:
                raw = {}
            normalized = self._validate_and_normalize(raw)
            self._cache = self._apply_overrides(normalized)
            self._last_mtime = mtime
            self._last_overrides_mtime = ovr_mtime
        return self._cache

    def _apply_overrides(self, base_config: dict[str, Any]) -> dict[str, Any]:
        """Wendet instanzspezifische Cooldowns und Einstellungen auf die Basiskonfiguration an."""
        if not self.overrides_path.exists():
            return base_config

        try:
            overrides = yaml.safe_load(self.overrides_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return base_config

        if not isinstance(overrides, dict):
            return base_config

        cfg = yaml.safe_load(yaml.safe_dump(base_config))

        # 1. Allgemeine Instanz-Cooldowns
        gen_ovr = overrides.get("general", {})
        if isinstance(gen_ovr, dict):
            gen = cfg.setdefault("general", {})
            if "global_cooldown_sec" in gen_ovr and gen_ovr["global_cooldown_sec"] is not None:
                gen["global_cooldown_sec"] = int(gen_ovr["global_cooldown_sec"])
            if "user_cooldown_sec" in gen_ovr and gen_ovr["user_cooldown_sec"] is not None:
                gen["user_cooldown_sec"] = int(gen_ovr["user_cooldown_sec"])

        # 2. Trigger-Overrides (Instanz-spezifisch: Start-Art, Preis, Cooldown, Rollen, Aktivierung)
        trig_ovr = overrides.get("triggers", {})
        if isinstance(trig_ovr, dict):
            for t in cfg.get("triggers", []):
                t_id = t.get("id")
                if t_id and t_id in trig_ovr:
                    ovr_entry = trig_ovr[t_id]
                    if isinstance(ovr_entry, dict):
                        has_custom = False
                        if "enabled" in ovr_entry and ovr_entry["enabled"] is not None:
                            t["enabled"] = bool(ovr_entry["enabled"])
                            has_custom = True
                        if "name" in ovr_entry and ovr_entry["name"]:
                            t["name"] = str(ovr_entry["name"]).strip()
                            has_custom = True
                        if "type" in ovr_entry and ovr_entry["type"]:
                            t["type"] = str(ovr_entry["type"]).strip()
                            has_custom = True
                        if "pattern" in ovr_entry:
                            t["pattern"] = str(ovr_entry["pattern"]).strip() if ovr_entry["pattern"] is not None else ""
                            has_custom = True
                        if "title" in ovr_entry:
                            t["title"] = str(ovr_entry["title"]).strip() if ovr_entry["title"] is not None else ""
                            has_custom = True
                        if "cost" in ovr_entry and ovr_entry["cost"] is not None:
                            try:
                                t["cost"] = int(ovr_entry["cost"])
                                has_custom = True
                            except (ValueError, TypeError):
                                pass
                        if "match_mode" in ovr_entry and ovr_entry["match_mode"]:
                            t["match_mode"] = str(ovr_entry["match_mode"]).strip()
                            has_custom = True
                        if "allowed_roles" in ovr_entry and isinstance(ovr_entry["allowed_roles"], list):
                            t["allowed_roles"] = ovr_entry["allowed_roles"]
                            has_custom = True
                        if "cooldown_sec" in ovr_entry and ovr_entry["cooldown_sec"] is not None:
                            try:
                                t["cooldown_sec"] = int(ovr_entry["cooldown_sec"])
                                t["is_custom_cooldown"] = True
                                has_custom = True
                            except (ValueError, TypeError):
                                pass
                        if has_custom:
                            t["is_custom"] = True
        return cfg

    def _validate_and_normalize(self, data: dict[str, Any]) -> dict[str, Any]:
        general = data.setdefault("general", {})
        prefix = str(general.get("prefix", "!")).strip()
        # Sicherheits-Regel: '/' darf NIEMALS verwendet werden!
        if not prefix or "/" in prefix:
            prefix = "!"
        general["prefix"] = prefix
        general["port"] = int(general.get("port", 4736))
        general["overlay_resolution"] = "1920x1080"
        general.setdefault("global_cooldown_sec", 3)
        general.setdefault("user_cooldown_sec", 15)
        general.setdefault("bot_chat_reply", True)
        general.setdefault("troll_hud_enabled", True)
        general.setdefault("chat_velocity_threshold", 15)
        general.setdefault("chat_emotes_auto_bounce", True)

        data.setdefault("tts", {
            "enabled": True,
            "engine": "edge-tts",
            "default_voice": "de-DE-KatjaNeural",
            "max_characters": 200,
            "volume": 0.9,
        })
        data.setdefault("auth", {
            "admin_password_hash": "",
            "streamer_password_hash": "",
            "mod_password_hash": "",
            "permissions": {
                "streamer": {
                    "can_manage_triggers": True,
                    "can_upload_sounds": True,
                    "can_edit_cooldowns": True,
                    "can_configure_twitch": True,
                    "can_manage_mod_permissions": True,
                },
                "mod": {
                    "can_trigger_panic": True,
                    "can_use_live_deck": True,
                    "can_trigger_tests": True,
                    "can_mute_audio": True,
                },
            },
        })
        data.setdefault("sounds", {})
        data.setdefault("triggers", [])
        return data

    def save_config(self, new_config: dict[str, Any]) -> dict[str, Any]:
        """Validiert und schreibt die Konfiguration atomar nach `config.yaml`."""
        general = new_config.get("general", {})
        prefix = str(general.get("prefix", "!")).strip()
        if "/" in prefix:
            raise ValueError("Sicherheits-Regel: '/' darf niemals als Command-Präfix verwendet werden!")

        validated = self._validate_and_normalize(new_config)
        yaml_text = yaml.safe_dump(
            validated,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        self.config_path.write_text(yaml_text, encoding="utf-8")
        self._last_mtime = self.config_path.stat().st_mtime
        self._cache = self._apply_overrides(validated)
        return self._cache

    def save_streamer_overrides(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Speichert Instanz-spezifische Cooldowns und Aktivierungs-Status
        in `overrides.<instance>.yaml`, ohne die globale `config.yaml` zu berühren.
        """
        existing: dict[str, Any] = {}
        if self.overrides_path.exists():
            try:
                existing = yaml.safe_load(self.overrides_path.read_text(encoding="utf-8")) or {}
            except Exception:
                existing = {}

        if not isinstance(existing, dict):
            existing = {}

        # 1. Allgemeine Cooldowns
        gen = payload.get("general", {})
        if isinstance(gen, dict):
            existing_gen = existing.setdefault("general", {})
            if "global_cooldown_sec" in gen and gen["global_cooldown_sec"] is not None:
                existing_gen["global_cooldown_sec"] = int(gen["global_cooldown_sec"])
            if "user_cooldown_sec" in gen and gen["user_cooldown_sec"] is not None:
                existing_gen["user_cooldown_sec"] = int(gen["user_cooldown_sec"])

        # 2. Trigger-spezifische Einstellungen (Typ, Preis/Kosten, Muster, Cooldown, Rollen, Aktivierung)
        trig_list = payload.get("triggers", [])
        if isinstance(trig_list, list):
            existing_trigs = existing.setdefault("triggers", {})
            for t in trig_list:
                if not isinstance(t, dict) or not t.get("id"):
                    continue
                t_id = t["id"]
                trig_ovr = existing_trigs.setdefault(t_id, {})
                if "enabled" in t and t["enabled"] is not None:
                    trig_ovr["enabled"] = bool(t["enabled"])
                if "name" in t and t["name"]:
                    trig_ovr["name"] = str(t["name"]).strip()
                if "type" in t and t["type"]:
                    trig_ovr["type"] = str(t["type"]).strip()
                if "pattern" in t:
                    trig_ovr["pattern"] = str(t["pattern"]).strip() if t["pattern"] is not None else ""
                if "title" in t:
                    trig_ovr["title"] = str(t["title"]).strip() if t["title"] is not None else ""
                if "cost" in t and t["cost"] is not None:
                    try:
                        trig_ovr["cost"] = int(t["cost"])
                    except (ValueError, TypeError):
                        pass
                if "match_mode" in t and t["match_mode"]:
                    trig_ovr["match_mode"] = str(t["match_mode"]).strip()
                if "allowed_roles" in t and isinstance(t["allowed_roles"], list):
                    trig_ovr["allowed_roles"] = t["allowed_roles"]
                if "cooldown_sec" in t and t["cooldown_sec"] is not None:
                    try:
                        trig_ovr["cooldown_sec"] = int(t["cooldown_sec"])
                    except (ValueError, TypeError):
                        pass

        yaml_text = yaml.safe_dump(
            existing,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
        self.overrides_path.write_text(yaml_text, encoding="utf-8")
        self._last_overrides_mtime = self.overrides_path.stat().st_mtime
        return self.get_config(force_reload=True)

    def filter_for_role(self, role: str) -> dict[str, Any]:
        """Gibt die Konfiguration entsprechend der Rolle (admin, streamer, mod) gefiltert zurück."""
        cfg = self.get_config()
        # Never expose raw password hashes to frontend
        safe_cfg = yaml.safe_load(yaml.safe_dump(cfg))
        safe_cfg["instance_id"] = self.instance_id
        if "auth" in safe_cfg:
            safe_cfg["auth"].pop("admin_password_hash", None)
            safe_cfg["auth"].pop("streamer_password_hash", None)
            safe_cfg["auth"].pop("mod_password_hash", None)

        env = self.read_env()
        if role == "admin":
            safe_cfg["system_secrets"] = {
                "twitch_client_id": env.get("TWITCH_CLIENT_ID", ""),
                "twitch_client_secret_set": bool(env.get("TWITCH_CLIENT_SECRET")),
                "twitch_broadcaster_channel": env.get("TWITCH_BROADCASTER_CHANNEL", ""),
                "twitch_bot_channel": env.get("TWITCH_BOT_CHANNEL", ""),
                "agent_token": env.get("AGENT_TOKEN", ""),
                "webhook_token": env.get("WEBHOOK_TOKEN", ""),
                "ha_base_url": env.get("HA_BASE_URL", "https://home.orfel.de"),
                "ha_token_set": bool(env.get("HA_ACCESS_TOKEN")),
                "n8n_webhook_url": env.get("N8N_WEBHOOK_URL", ""),
                "env_age_exists": self.env_age_path.exists(),
            }
        elif role == "streamer":
            safe_cfg["system_secrets"] = {
                "twitch_client_id": env.get("TWITCH_CLIENT_ID", ""),
                "twitch_client_secret_set": bool(env.get("TWITCH_CLIENT_SECRET")),
                "twitch_broadcaster_channel": env.get("TWITCH_BROADCASTER_CHANNEL", ""),
                "twitch_bot_channel": env.get("TWITCH_BOT_CHANNEL", ""),
                "agent_token": env.get("AGENT_TOKEN", ""),
                "env_age_exists": self.env_age_path.exists(),
            }
        else:
            # Mod sees only public operational settings
            safe_cfg.pop("system_secrets", None)
        return safe_cfg
