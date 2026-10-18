"""`pianotake` command: opens the window, or records in the terminal with --cli."""
import argparse
import datetime
import sys
import threading
from pathlib import Path

from . import __version__
from .settings import DEFAULT_OUT, load_settings, output_folder, save_settings


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="pianotake",
        description="Record a WAV and a sample-aligned MIDI file from a digital piano or "
                    "MIDI keyboard. Opens a window unless --cli is given.")
    ap.add_argument("--audio", metavar="NAME", help="audio input to use (part of its name)")
    ap.add_argument("--midi", metavar="NAME", help="MIDI input to use (part of its name), or 'none'")
    ap.add_argument("--out", metavar="DIR", help=f"output folder (default: last used, else {DEFAULT_OUT})")
    ap.add_argument("--channels", type=int, choices=(1, 2), default=2,
                    help="record the device's first 1 or 2 inputs (default 2)")
    ap.add_argument("--list", action="store_true", help="list audio inputs and MIDI ports, then exit")
    ap.add_argument("--cli", action="store_true", help="record in the terminal, without a window")
    ap.add_argument("--name", help="take name for --cli (default take_YYYYMMDD_HHMMSS)")
    ap.add_argument("--version", action="version", version=f"pianotake {__version__}")
    return ap.parse_args(argv)


def list_devices(args):
    from . import recorder as r
    audio = r.list_audio_inputs(args.channels)
    midi = r.list_midi_inputs()
    print("Audio inputs:")
    for d in audio:
        tag = "  (virtual)" if r.is_virtual_audio(d["name"]) else ""
        print(f"  {d['name']}  [{d['channels']} ch used, {d['samplerate']} Hz]{tag}")
    print("MIDI inputs:")
    for n in midi or ["(none)"]:
        print(f"  {n}{'  (virtual)' if r.is_virtual_midi(n) else ''}")
    a, m = r.pick_devices([d["name"] for d in audio], midi, args.audio, args.midi)
    print(f"\nAuto-detected: audio = {a or '-'}, MIDI = {m or '-'}")
    return 0


def choose(args, audio_names, midi_names):
    """Explicit --audio/--midi > the choice remembered from last time > automatic pairing."""
    from .recorder import NO_MIDI, pick_devices
    saved = load_settings()
    no_midi = bool(args.midi) and args.midi.lower() == "none"
    a, m = pick_devices(audio_names, midi_names, args.audio, None if no_midi else args.midi)
    if not args.audio and saved.get("audio") in audio_names:
        a = saved["audio"]
    if not args.midi and saved.get("midi") in midi_names:
        m = saved["midi"]
    if no_midi or (not args.midi and saved.get("midi") == NO_MIDI):
        m = None
    return a, m


def run_cli(args):
    from . import recorder as r
    audio = r.list_audio_inputs(args.channels)
    a, m = choose(args, [d["name"] for d in audio], r.list_midi_inputs())
    if a is None:
        print("No audio input chosen. Pick one with --audio NAME (see pianotake --list).")
        return 2
    dev = next(d for d in audio if d["name"] == a)
    print(f"Audio: {a} ({dev['channels']} ch, {dev['samplerate']} Hz)")
    print(f"MIDI:  {m or 'none (audio only)'}")
    level, text = r.diagnose(r.probe_input(dev["index"], dev["channels"], dev["samplerate"], 1.0), a)
    print(text)
    if level == "err":
        if not sys.stdin.isatty() or input("Record anyway? [y/N] ").strip().lower() != "y":
            return 1

    folder = Path(args.out or output_folder()).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    base = r.unique_base(folder, args.name or datetime.datetime.now().strftime("take_%Y%m%d_%H%M%S"))
    live = {"level": 0.0, "notes": 0}

    def post(msg):
        if msg[0] == "level":
            live["level"] = max(live["level"], msg[1])
        elif msg[0] == "midi":
            live["notes"] = msg[1]
        elif msg[0] == "log":
            print(f"\n{msg[2]}")

    take = r.Take(a, dev["channels"], dev["samplerate"], m, base, post)
    input("Press Enter to start recording...")
    take.start()
    save_settings({**load_settings(), "audio": a, "midi": m or r.NO_MIDI, "folder": str(folder)})
    print("● Recording. Press Enter (or Ctrl+C) to stop.")
    threading.Thread(target=lambda: (sys.stdin.readline(), take.stop()), daemon=True).start()
    try:
        while not take.finished.wait(0.2):
            secs = take.frames_captured / take.sr
            bar = "#" * int(max(0.0, r.db(live["level"]) + 60) / 2)
            warn = "  NO AUDIO SIGNAL" if secs >= r.NO_SIGNAL_SECONDS and take.max_peak == 0 else ""
            print(f"\r  {int(secs // 60):02d}:{secs % 60:04.1f}  [{bar:<30}] {r.fmt_db(live['level']):>11}"
                  f"  notes {live['notes']}{warn}   ", end="", flush=True)
            live["level"] = 0.0
    except KeyboardInterrupt:
        take.stop()
    res = take.wait()
    print(f"\nSaved {res.wav_path}\n      {res.mid_path}")
    print(f"{res.duration:.1f} s, {res.note_ons} notes, {res.midi_events} MIDI events, "
          f"peak {r.fmt_db(res.max_peak)}, input overflows: {res.overflows}")
    if res.stop_reason:
        print(f"Take stopped early: {res.stop_reason}. Everything captured so far was saved.")
    if res.max_peak == 0.0 and res.frames:
        print(r.silence_help("the recording"))
    for p in res.problems:
        print(f"warning: {p}")
    return 0


def main(argv=None):
    args = parse_args(argv)
    if args.list:
        return list_devices(args)
    if args.cli:
        return run_cli(args)
    try:
        from .gui import run
    except ImportError as e:  # e.g. Python built without Tk
        print(f"Can't open the window ({e}). Install Tk for your Python "
              "(macOS Homebrew: brew install python-tk; Debian/Ubuntu: apt install python3-tk), "
              "or use: pianotake --cli", file=sys.stderr)
        return 1
    run(args.audio, args.midi, args.out, args.channels)
    return 0
