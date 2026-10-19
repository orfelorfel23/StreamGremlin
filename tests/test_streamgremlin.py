"""Automated verification tests for StreamGremlin Hub, Auth Hierarchy, Prefix Guard, and Matching."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["WEBHOOK_TOKEN"] = "test_webhook_secret"

from fastapi.testclient import TestClient
from src.hub.auth import hash_password
from src.hub.main import app, config_manager, auth_manager, twitch_manager


def run_tests() -> None:
    # Snapshot original config to restore after tests
    orig_cfg_text = config_manager.config_path.read_text(encoding="utf-8")
    try:
        cfg = config_manager.get_config(force_reload=True)
        cfg.setdefault("auth", {})["admin_password_hash"] = hash_password("test_admin_pw_hash")
        cfg["auth"]["streamer_password_hash"] = hash_password("test_streamer_pw_hash")
        cfg["auth"]["mod_password_hash"] = hash_password("test_mod_pw_hash")
        config_manager.save_config(cfg)
        config_manager.write_env({
            "WEBHOOK_TOKEN": "test_webhook_secret",
            "ADMIN_PASSWORD": "test_admin_pw",
            "STREAMER_PASSWORD": "test_streamer_pw",
            "MOD_PASSWORD": "test_mod_pw",
        }, encrypt_age=False)

        # 1. Test .env Password Authentication
        assert auth_manager.authenticate_by_password("test_admin_pw") == "admin"
        assert auth_manager.authenticate_by_password("test_streamer_pw") == "streamer"
        assert auth_manager.authenticate_by_password("test_mod_pw") == "mod"
        assert auth_manager.authenticate_by_password("wrong_pw") is None

        # Test Hash Fallback
        assert auth_manager.authenticate_by_password("test_admin_pw_hash") == "admin"
        assert auth_manager.authenticate_by_password("test_streamer_pw_hash") == "streamer"
        assert auth_manager.authenticate_by_password("test_mod_pw_hash") == "mod"

        # Test Hierarchical Password Changes
        # Admin can change streamer and mod without current_password, and admin with current_password
        auth_manager.change_password("admin", "streamer", "new_streamer_pw")
        assert auth_manager.authenticate_by_password("new_streamer_pw") == "streamer"
        # Streamer can change mod password, but CANNOT change admin password
        auth_manager.change_password("streamer", "mod", "new_mod_pw")
        assert auth_manager.authenticate_by_password("new_mod_pw") == "mod"
        try:
            auth_manager.change_password("streamer", "admin", "hacked_pw")
            raise AssertionError("Streamer should not be able to change admin password")
        except PermissionError:
            pass
        try:
            auth_manager.change_password("mod", "mod", "hacked_mod_pw")
            raise AssertionError("Mod should not be able to change any password")
        except PermissionError:
            pass
        print("[PASS] 3-Tier Role-Based Access Control & Password Hierarchy verified.")

        # 2. Test '/' Command Prefix Prohibition
        try:
            bad_cfg = config_manager.get_config()
            bad_cfg["general"]["prefix"] = "/"
            config_manager.save_config(bad_cfg)
            raise AssertionError("'/' prefix should have raised ValueError")
        except ValueError:
            print("[PASS] '/' Twitch command prefix prohibition verified.")

        # 3. Test Word-Boundary vs Substring Matching (Case-Insensitive)
        assert twitch_manager.matches_word_rule("Ampel ist Rot heute", "rot", "word") is True
        assert twitch_manager.matches_word_rule("Ich esse Brot", "rot", "word") is False
        assert twitch_manager.matches_word_rule("Ich esse Brot", "rot", "substring") is True
        print("[PASS] Word-boundary (\\bwort\\b) vs Substring case-insensitive matching verified.")

        # 4. Test FastAPI HTTP Endpoints
        with TestClient(app) as client:
            r_health = client.get("/api/health")
            assert r_health.status_code == 200 and r_health.json()["status"] == "ok"

            r_ver = client.get("/api/agent/version")
            assert r_ver.status_code == 200 and "latest_version" in r_ver.json()

            r_ovl = client.get("/overlay")
            assert r_ovl.status_code == 200 and "1920" in r_ovl.text

            # Login as Admin
            r_login = client.post("/api/auth/login", json={"password": "test_admin_pw"})
            assert r_login.status_code == 200 and r_login.json()["role"] == "admin"

            # Fetch config
            r_cfg = client.get("/api/config")
            assert r_cfg.status_code == 200 and len(r_cfg.json()["config"]["triggers"]) > 0

            # Simulate chat & panic
            r_sim = client.post("/api/test/chat", json={"user": "Tester", "text": "!bonk"})
            assert r_sim.status_code == 200 and "matched" in r_sim.json()

            r_panic = client.post("/api/panic")
            assert r_panic.status_code == 200 and r_panic.json()["ok"] is True

            # Inbound n8n webhook
            r_wh = client.post(
                "/api/webhook/trigger",
                headers={"X-Webhook-Token": "test_webhook_secret"},
                json={"action": {"type": "confetti_burst", "duration_ms": 2000}, "user": "n8n_bot"},
            )
            assert r_wh.status_code == 200 and r_wh.json()["ok"] is True

            # 5. Test Streamer Instance Trigger Start, Cost & Cooldown Overrides Isolation
            # Streamer login (password was updated to new_streamer_pw in step 1)
            r_s_login = client.post("/api/auth/login", json={"password": "new_streamer_pw"})
            assert r_s_login.status_code == 200 and r_s_login.json()["role"] == "streamer"

            # Streamer adjusts trigger start, price/cost, and cooldown
            orig_triggers = r_cfg.json()["config"]["triggers"]
            target_trigger = orig_triggers[0]
            target_id = target_trigger["id"]
            orig_cd = target_trigger.get("cooldown_sec", 5)
            orig_type = target_trigger.get("type", "command")

            update_payload = {
                "triggers": [
                    {
                        "id": target_id,
                        "type": "channel_point",
                        "title": "Custom Reward Title",
                        "cost": 3500,
                        "cooldown_sec": orig_cd + 37,
                        "enabled": True,
                    }
                ]
            }
            r_put = client.put("/api/config", json=update_payload)
            assert r_put.status_code == 200 and r_put.json()["ok"] is True

            # Verify overrides file was created and global config.yaml was NOT modified
            assert config_manager.overrides_path.exists()
            import yaml
            overrides_data = yaml.safe_load(config_manager.overrides_path.read_text(encoding="utf-8"))
            assert overrides_data["triggers"][target_id]["cooldown_sec"] == orig_cd + 37
            assert overrides_data["triggers"][target_id]["cost"] == 3500
            assert overrides_data["triggers"][target_id]["type"] == "channel_point"
            assert overrides_data["triggers"][target_id]["title"] == "Custom Reward Title"

            # Base config on disk must still have original cooldown and type
            raw_base_yaml = yaml.safe_load(config_manager.config_path.read_text(encoding="utf-8"))
            base_trigger = next(t for t in raw_base_yaml["triggers"] if t["id"] == target_id)
            assert base_trigger.get("cooldown_sec", 5) == orig_cd
            assert base_trigger.get("type") == orig_type

            # Merged config returned to streamer must show overridden cooldown, cost, and type
            r_s_cfg = client.get("/api/config")
            s_trig = next(t for t in r_s_cfg.json()["config"]["triggers"] if t["id"] == target_id)
            assert s_trig["cooldown_sec"] == orig_cd + 37
            assert s_trig["cost"] == 3500
            assert s_trig["type"] == "channel_point"
            assert s_trig["title"] == "Custom Reward Title"
            assert s_trig.get("is_custom") is True
            print("[PASS] Streamer instance trigger start, price/cost, and cooldown overrides verified.")

        print("[PASS] All FastAPI endpoints, Simulator, Panic, and n8n Webhook verified.")
    finally:
        config_manager.config_path.write_text(orig_cfg_text, encoding="utf-8")
        if config_manager.overrides_path.exists():
            config_manager.overrides_path.unlink()


if __name__ == "__main__":
    run_tests()
