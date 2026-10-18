"""`pianotake` command: opens the window, or records in the terminal with --cli."""
import argparse
import datetime
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .settings import DEFAULT_OUT, load_settings, output_folder, save_settings, tilde


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="pianotake",
        description="Record a WAV and a sample-aligned MIDI file from a digital piano or "
                    "MIDI keyboard. Opens a window unless --cli is given.")
    ap.add_argument("--audio", metavar="NAME", help="audio input to use (part of its name), or 'none'")
    ap.add_argument("--midi", metavar="NAME", help="MIDI input to use (part of its name), or 'none'")
    ap.add_argument("--out", metavar="DIR", help=f"output folder (default: last used, else {DEFAULT_OUT})")
    ap.add_argument("--channels", type=int, choices=(1, 2), default=2,
                    help="record the device's first 1 or 2 inputs (default 2)")
    ap.add_argument("--list", action="store_true", help="list audio inputs and MIDI ports, then exit")
    ap.add_argument("--cli", action="store_true", help="record in the terminal, without a window")
    ap.add_argument("--name", help="take name for --cli (default take_YYYYMMDD_HHMMSS)")
    ap.add_argument("--version", action="version", version=f"pianotake {__version__}")
    ap.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def self_test():
    """Check that a packaged build can load everything it needs (used by the release CI)."""
    import importlib
    for module in ("tkinter", "pianotake.check", "pianotake.gui"):
        importlib.import_module(module)
    from . import recorder
    recorder.list_audio_inputs()
    recorder.list_midi_inputs()
    print(f"pianotake {__version__} self-test ok")
    return 0


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
    """Explicit --audio/--midi > the choice remembered from last time > automatic pairing.
    Returns (audio, midi); audio is NO_AUDIO for a MIDI-only take, midi None for audio only."""
    from .recorder import NO_AUDIO, NO_MIDI, pick_devices
    saved = load_settings()
    no_audio = bool(args.audio) and args.audio.lower() == "none"
    no_midi = bool(args.midi) and args.midi.lower() == "none"
    a, m = pick_devices(audio_names, midi_names, None if no_audio else args.audio,
                        None if no_midi else args.midi)
    if not args.audio and saved.get("audio") in audio_names + [NO_AUDIO]:
        a = saved["audio"]
    if not args.midi and saved.get("midi") in midi_names:
        m = saved["midi"]
    if no_midi or (not args.midi and saved.get("midi") == NO_MIDI):
        m = None
    if no_audio:
        a = NO_AUDIO
    return a, m


def preflight(r, dev, name):
    """The 1 s input check, waiting out the macOS permission prompt. False = don't record."""
    def probe():
        return r.probe_input(dev["index"], dev["channels"], dev["samplerate"], 1.0)
    res = probe()
    if res["all_zero"] and res.get("mic") == "not_determined":
        # Opening the input just made macOS ask; until the user answers it delivers silence.
        print("macOS is asking whether this app may use audio input. Click Allow...")
        for _ in range(600):
            if r.mic_authorization() != "not_determined":
                break
            time.sleep(0.5)
        res = probe()
    elif res["all_zero"]:
        print("No signal yet; testing again in 2 s...")  # a just-switched-on instrument
        time.sleep(2)
        res = probe()
    level, text = r.diagnose(res, name)
    print(text)
    if level == "err":
        return sys.stdin.isatty() and input("Record anyway? [y/N] ").strip().lower() == "y"
    return True


def run_cli(args):
    from . import recorder as r
    audio = r.list_audio_inputs(args.channels)
    a, m = choose(args, [d["name"] for d in audio], r.list_midi_inputs())
    midi_only = a == r.NO_AUDIO
    if a is None:
        print("No audio input chosen. Pick one with --audio NAME, or --audio none for MIDI only "
              "(see pianotake --list).")
        return 2
    if midi_only and m is None:
        print("MIDI-only recording needs a MIDI input. Pick one with --midi NAME (see pianotake --list).")
        return 2
    dev = None if midi_only else next(d for d in audio if d["name"] == a)
    print("Audio: none (MIDI only)" if midi_only else
          f"Audio: {a} ({dev['channels']} ch, {dev['samplerate']} Hz)")
    print(f"MIDI:  {m or 'none (audio only)'}")
    if not midi_only and not preflight(r, dev, a):
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

    if midi_only:
        take = r.Take(None, 0, 0, m, base, post)
    else:
        take = r.Take(a, dev["channels"], dev["samplerate"], m, base, post)
    input("Press Enter to start recording...")
    take.start()
    save_settings({**load_settings(), "audio": a, "midi": m or r.NO_MIDI, "folder": tilde(folder)})
    print("● Recording. Press Enter (or Ctrl+C) to stop.")
    threading.Thread(target=lambda: (sys.stdin.readline(), take.stop()), daemon=True).start()
    try:
        while not take.finished.wait(0.2):
            secs = take.elapsed()
            line = f"\r  {int(secs // 60):02d}:{secs % 60:04.1f}"
            if take.audio:
                bar = "#" * int(max(0.0, r.db(live["level"]) + 60) / 2)
                warn = "  NO AUDIO SIGNAL" if secs >= r.NO_SIGNAL_SECONDS and take.max_peak == 0 else ""
                line += f"  [{bar:<30}] {r.fmt_db(live['level']):>11}"
            else:
                warn = ""
            print(f"{line}  notes {live['notes']}{warn}   ", end="", flush=True)
            live["level"] = 0.0
    except KeyboardInterrupt:
        take.stop()
    res = take.wait()
    files = [p for p in (res.wav_path, res.mid_path) if p]
    print("\nSaved " + "\n      ".join(tilde(p) for p in files))
    stats = f"{res.duration:.1f} s, {res.note_ons} notes, {res.midi_events} MIDI events"
    if res.wav_path:
        stats += f", peak {r.fmt_db(res.max_peak)}, input overflows: {res.overflows}"
    print(stats)
    if res.stop_reason:
        print(f"Take stopped early: {res.stop_reason}. Everything captured so far was saved.")
    if res.wav_path and res.max_peak == 0.0 and res.frames:
        print(r.silence_help("the recording"))
    for p in res.problems:
        print(f"warning: {p}")
    return 0


def main(argv=None):
    args = parse_args(argv)
    if args.self_test:
        return self_test()
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
