"""
StreamGremlin CLI-Hilfstool (Notfall-Passwort-Reset)
Verwendung im Container:
  docker compose exec streamgremlin python -m src.hub.cli reset-password
"""
import sys
from src.hub.config_manager import ConfigManager
from src.hub.auth import AuthManager


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ("reset-password", "reset-admin"):
        print("Verwendung: python -m src.hub.cli reset-password [neues_passwort]")
        sys.exit(1)

    custom_pw = args[1] if len(args) > 1 else None
    cm = ConfigManager()
    am = AuthManager(cm)
    new_pw = am.reset_admin_password_cli(custom_pw)

    print("=" * 60)
    print("👹 STREAMGREMLIN NOTFALL-PASSWORT-RESET ERFOLGREICH")
    print("=" * 60)
    print(f"Neues Admin-Passwort: {new_pw}")
    print("Bitte logge dich im Dashboard ein und ändere es nach Bedarf.")
    print("=" * 60)


if __name__ == "__main__":
    main()
