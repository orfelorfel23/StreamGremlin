"""PyInstaller build script for compiling StreamGremlin.exe on Windows."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

from src.agent.agent import AGENT_VERSION

AGENT_SCRIPT = ROOT_DIR / "src" / "agent" / "agent.py"
ICON_PNG = ROOT_DIR / "icon.png"
ICON_ICO = ROOT_DIR / "src" / "agent" / "icon.ico"


def generate_version_file(version_str: str) -> Path:
    """Generate PyInstaller version resource to embed into Windows PE executable."""
    parts = []
    for p in version_str.lstrip("v").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 4:
        parts.append(0)
    v_tuple = tuple(parts[:4])

    content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={v_tuple},
    prodvers={v_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904B0',
          [
            StringStruct('CompanyName', 'StreamGremlin'),
            StringStruct('FileDescription', 'StreamGremlin Windows Companion Agent'),
            StringStruct('FileVersion', '{version_str}'),
            StringStruct('InternalName', 'StreamGremlin'),
            StringStruct('LegalCopyright', 'CC0-1.0'),
            StringStruct('OriginalFilename', 'StreamGremlin.exe'),
            StringStruct('ProductName', 'StreamGremlin'),
            StringStruct('ProductVersion', '{version_str}')
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    ver_file = ROOT_DIR / "src" / "agent" / "file_version_info.txt"
    ver_file.write_text(content, encoding="utf-8")
    return ver_file


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
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "StreamGremlin",
        "--hidden-import",
        "pynput.keyboard._win32",
        "--hidden-import",
        "pynput.mouse._win32",
        "--hidden-import",
        "pystray._win32",
        "--collect-all",
        "pynput",
        "--collect-all",
        "pystray",
    ]
    if ICON_PNG.exists():
        cmd.extend(["--add-data", f"{ICON_PNG}{sep}."])
    if ico_path and ico_path.exists():
        cmd.extend(["--icon", str(ico_path)])

    ver_file = generate_version_file(AGENT_VERSION)
    if ver_file.exists():
        cmd.extend(["--version-file", str(ver_file)])

    cmd.append(str(AGENT_SCRIPT))
    print("Running PyInstaller:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT_DIR), check=True)
    print("\nBuild complete! Executable is located at: dist/StreamGremlin.exe")


if __name__ == "__main__":
    build()
