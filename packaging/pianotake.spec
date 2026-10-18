# PyInstaller spec for the pianotake desktop app on macOS (pianotake.app), Windows and Linux.
# Built by packaging/macos/build.sh (macOS) or packaging/build.py (Windows, Linux).
import os
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "pianotake" / "__init__.py").read_text()).group(1)
BUNDLE_ID = os.environ.get("PIANOTAKE_BUNDLE_ID", "io.github.rodkinivan.pianotake")
ICON_DIR = os.environ.get("PIANOTAKE_ICON_DIR")
MAC, WIN = sys.platform == "darwin", sys.platform.startswith("win")

a = Analysis(
    [str(Path(SPECPATH) / "launcher.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "pianotake" / "icon.png"), "pianotake")],   # window icon
    hiddenimports=["mido.backends.rtmidi", "rtmidi"],   # mido picks its backend at runtime
    excludes=["PyInstaller", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="pianotake", console=False,
          icon=os.path.join(ICON_DIR, "pianotake.ico") if (WIN and ICON_DIR) else None)
coll = COLLECT(exe, a.binaries, a.datas, name="pianotake")

if MAC:
    app = BUNDLE(
        coll,
        name="pianotake.app",
        icon=os.path.join(ICON_DIR, "pianotake.icns") if ICON_DIR else None,
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "pianotake",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.music",
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "MIT License",
            # Shown when macOS asks for permission to use the instrument's USB audio input.
            "NSMicrophoneUsageDescription":
                "pianotake records the sound of your instrument from its USB audio input.",
        },
    )
