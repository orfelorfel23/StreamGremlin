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
        cfg.setdefault("auth", {})["admin_password_hash"] = hash_password("test_admin_pw")
        cfg["auth"]["streamer_password_hash"] = hash_password("test_streamer_pw")
        cfg["auth"]["mod_password_hash"] = hash_password("test_mod_pw")
        config_manager.save_config(cfg)
        config_manager.write_env({"WEBHOOK_TOKEN": "test_webhook_secret"}, encrypt_age=False)

        # 1. Test 3-Tier Role Authentication
        assert auth_manager.authenticate_by_password("test_admin_pw") == "admin"
        assert auth_manager.authenticate_by_password("test_streamer_pw") == "streamer"
        assert auth_manager.authenticate_by_password("test_mod_pw") == "mod"
        assert auth_manager.authenticate_by_password("wrong_pw") is None

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

        print("[PASS] All FastAPI endpoints, Simulator, Panic, and n8n Webhook verified.")
    finally:
        config_manager.config_path.write_text(orig_cfg_text, encoding="utf-8")


if __name__ == "__main__":
    run_tests()
