"""Build the pianotake desktop app for Windows or Linux with PyInstaller.

    pip install pyinstaller .
    python packaging/build.py

Output: dist/pianotake/ (the app folder) and an archive of it:
  dist/pianotake-<version>-windows-x64.zip   or   dist/pianotake-<version>-linux-x86_64.tar.gz
macOS uses packaging/macos/build.sh instead (app bundle, DMG, signing).
"""
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIST, WORK = ROOT / "dist", ROOT / "build" / "desktop"

README = {
    "windows": """pianotake {version} for Windows

Run pianotake.exe. Keep it inside this folder: it needs the files next to it.
Windows may show "Windows protected your PC" the first time: click More info, then Run anyway.
If recordings are silent: Settings > Privacy & security > Microphone > allow desktop apps.

https://github.com/RodkinIvan/pianotake
""",
    "linux": """pianotake {version} for Linux

Run ./pianotake inside this folder. It needs PortAudio from your distribution:
  Debian/Ubuntu: sudo apt install libportaudio2
  Fedora:        sudo dnf install portaudio
  Arch:          sudo pacman -S portaudio

https://github.com/RodkinIvan/pianotake
""",
}


def main():
    if sys.platform == "darwin":
        sys.exit("On macOS use packaging/macos/build.sh")
    plat = "windows" if sys.platform.startswith("win") else "linux"
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    if plat == "linux" and arch == "x64":
        arch = "x86_64"
    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "pianotake" / "__init__.py").read_text()).group(1)

    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(DIST / "pianotake", ignore_errors=True)
    subprocess.run([sys.executable, str(HERE / "make_icon.py"), str(WORK / "icon")], check=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(DIST), "--workpath", str(WORK / "pyinstaller"),
                    str(HERE / "pianotake.spec")],
                   check=True, env=dict(os.environ, PIANOTAKE_ICON_DIR=str(WORK / "icon")))
    (DIST / "pianotake" / "README.txt").write_text(README[plat].format(version=version))

    name = DIST / f"pianotake-{version}-{plat}-{arch}"
    archive = shutil.make_archive(str(name), "zip" if plat == "windows" else "gztar", DIST, "pianotake")
    print(f"Built {DIST / 'pianotake'}\n      {archive}")


if __name__ == "__main__":
    main()
