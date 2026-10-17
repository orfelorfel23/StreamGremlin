"""PyInstaller build script for compiling StreamGremlin.exe on Windows."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
AGENT_SCRIPT = ROOT_DIR / "src" / "agent" / "agent.py"
ICON_PNG = ROOT_DIR / "icon.png"
ICON_ICO = ROOT_DIR / "src" / "agent" / "icon.ico"


def ensure_ico() -> Path | None:
    """Convert root icon.png to icon.ico using Pillow if available."""
    if not ICON_PNG.exists():
        return None
    try:
        from PIL import Image
        img = Image.open(ICON_PNG).convert("RGBA")
        img.save(ICON_ICO, format="ICO", sizes=[(64, 64), (32, 32), (16, 16)])
        return ICON_ICO
    except Exception as exc:
        print(f"[WARN] Could not convert icon.png to .ico: {exc}")
        return None


def build() -> None:
    ico_path = ensure_ico()
    sep = ";" if sys.platform == "win32" else ":"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name",
        "StreamGremlin",
    ]
    if ICON_PNG.exists():
        cmd.extend(["--add-data", f"{ICON_PNG}{sep}."])
    if ico_path and ico_path.exists():
        cmd.extend(["--icon", str(ico_path)])

    cmd.append(str(AGENT_SCRIPT))
    print("Running PyInstaller:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT_DIR), check=True)
    print("\nBuild complete! Executable is located at: dist/StreamGremlin.exe")


if __name__ == "__main__":
    build()
