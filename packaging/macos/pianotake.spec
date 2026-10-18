# PyInstaller spec for pianotake.app. Built by packaging/macos/build.sh.
import os
import re
from pathlib import Path

ROOT = Path(SPECPATH).parents[1]
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "pianotake" / "__init__.py").read_text()).group(1)
BUNDLE_ID = os.environ.get("PIANOTAKE_BUNDLE_ID", "io.github.rodkinivan.pianotake")

a = Analysis(
    [str(Path(SPECPATH) / "launcher.py")],
    pathex=[str(ROOT)],
    hiddenimports=["mido.backends.rtmidi", "rtmidi"],   # mido picks its backend at runtime
    excludes=["PyInstaller", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="pianotake", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="pianotake")
app = BUNDLE(
    coll,
    name="pianotake.app",
    icon=os.environ.get("PIANOTAKE_ICON"),
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
