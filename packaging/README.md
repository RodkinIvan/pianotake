# Building the desktop apps

The apps are made with [PyInstaller](https://pyinstaller.org) from `pianotake.spec`, so
users need no Python. Build each one on the system it is for:

```bash
pip install pyinstaller .
python packaging/build.py          # Windows -> dist/pianotake-<version>-windows-x64.zip
                                   # Linux   -> dist/pianotake-<version>-linux-x86_64.tar.gz
packaging/macos/build.sh           # macOS   -> dist/pianotake.app + .dmg (signing: macos/README.md)
```

`make_icon.py` draws the icon at build time (`--package` refreshes `pianotake/icon.png`,
the window icon). Every build answers `pianotake --self-test`, which loads Tk, the audio
and MIDI libraries and the window code without opening a window.

GitHub Actions (`.github/workflows/desktop-apps.yml`) builds all four (macOS Apple
Silicon and Intel, Windows, Linux) on every `v*` tag and publishes them as a release.
You can also run it by hand from the Actions tab.

The Linux build is made on Ubuntu 22.04 so that it runs on distributions with glibc 2.35 or
newer. It uses the system's PortAudio (`libportaudio2`).
