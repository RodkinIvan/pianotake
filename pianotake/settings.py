"""Where takes go and what was chosen last time (audio device, MIDI port, folder)."""
import json
import os
import sys
from pathlib import Path

DEFAULT_OUT = Path.home() / "Music" / "PianoTakes"


def settings_path():
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "pianotake" / "settings.json"


def load_settings():
    try:
        return json.loads(settings_path().read_text())
    except Exception:  # noqa: BLE001 - missing or corrupt: start fresh
        return {}


def save_settings(data):
    try:
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))
    except Exception:  # noqa: BLE001 - settings are a convenience only
        pass


def output_folder():
    """The folder takes are saved to: the remembered one, else the default."""
    return Path(load_settings().get("folder") or DEFAULT_OUT).expanduser()
