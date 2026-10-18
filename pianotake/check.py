"""Self-check a recorded take: durations, note count, audio peak, and the offset
between the first MIDI note_on and the first audio onset.

Usage:
    pianotake-check                      # newest take in the output folder
    pianotake-check take.wav             # or take.mid, or the base name
    pianotake-check ~/Music/PianoTakes   # newest take in that folder
    pianotake-check take.wav --tol 0.03  # offset tolerance in seconds

Exit status 0 = all checks passed, 1 = a check failed or was inconclusive, 2 = files missing.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import mido

from .settings import output_folder


def resolve(arg):
    p = Path(arg).expanduser() if arg else output_folder()
    if p.is_dir():
        wavs = sorted(p.glob("*.wav"), key=lambda f: f.stat().st_mtime)
        if not wavs:
            raise FileNotFoundError(f"no .wav files in {p}")
        p = wavs[-1]
    base = p.with_suffix("") if p.suffix.lower() in (".wav", ".mid") else p
    wav, mid = Path(f"{base}.wav"), Path(f"{base}.mid")
    for f in (wav, mid):
        if not f.exists():
            raise FileNotFoundError(f"missing {f}")
    return wav, mid


def midi_info(path):
    """Absolute-time view of a MIDI file (any type, honouring tempo changes)."""
    mf = mido.MidiFile(str(path))
    events = []
    for ti, track in enumerate(mf.tracks):
        tick = 0
        for msg in track:
            tick += msg.time
            events.append((tick, ti, msg))
    events.sort(key=lambda e: (e[0], e[1]))
    tempo, last_tick, secs = 500000, 0, 0.0
    notes, cc64, other_cc, end = [], 0, 0, 0.0
    for tick, _, msg in events:
        secs += mido.tick2second(tick - last_tick, mf.ticks_per_beat, tempo)
        last_tick = tick
        if msg.type == "set_tempo":
            tempo = msg.tempo
        elif msg.type == "note_on" and msg.velocity > 0:
            notes.append(secs)
        elif msg.type == "control_change":
            if msg.control == 64:
                cc64 += 1
            else:
                other_cc += 1
        end = max(end, secs)  # includes end_of_track
    return {"duration": end, "note_times": notes, "cc64": cc64, "other_cc": other_cc,
            "type": mf.type, "tpb": mf.ticks_per_beat, "messages": len(events)}


def audio_onset(x, sr, hop_s=0.001, quiet_s=0.02):
    """First onset by a simple energy threshold on a 1 ms peak envelope.
    Threshold = max(noise floor + 30 dB, loudest peak - 45 dB, -80 dBFS), but never above
    loudest peak - 20 dB (a take full of playing has a high "floor").
    If sound is already ringing at sample 0 (a note held over from before the take), the
    search starts after the first `quiet_s` of silence instead.
    Returns (onset_s or None, threshold, quiet_from_s); quiet_from_s is None if the take
    started in silence, and the onset is None if the ringing never stopped."""
    mono = np.abs(x).max(axis=1) if x.ndim > 1 else np.abs(x)
    hop = max(1, int(sr * hop_s))
    n = len(mono) // hop
    if n == 0:
        return None, 0.0, None
    env = mono[: n * hop].reshape(n, hop).max(axis=1)
    floor = float(np.percentile(env, 10))
    top = float(env.max())
    thr = max(min(floor * 10 ** (30 / 20), top * 10 ** (-20 / 20)), top * 10 ** (-45 / 20), 10 ** (-80 / 20))
    above = env > thr
    start, quiet_from = 0, None
    if above[:5].any():
        q = max(1, int(quiet_s / hop_s))
        run = np.convolve(~above, np.ones(q, dtype=int), "valid")  # below-threshold hops per window
        quiet = np.flatnonzero(run == q)
        if len(quiet) == 0:
            return None, thr, float("inf")
        start = int(quiet[0])
        quiet_from = start * hop / sr
    idx = np.flatnonzero(above[start:]) + start
    if len(idx) == 0:
        return None, thr, quiet_from
    i = idx[0]
    # refine to the first sample above threshold inside that hop
    seg = mono[i * hop:(i + 1) * hop]
    j = int(np.flatnonzero(seg > thr)[0]) if np.any(seg > thr) else 0
    return (i * hop + j) / sr, thr, quiet_from


def analyze(wav, mid):
    x, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    info = sf.info(str(wav))
    m = midi_info(mid)
    peak = float(np.abs(x).max()) if len(x) else 0.0
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if len(x) else 0.0
    onset, thr, quiet_from = audio_onset(x, sr) if peak > 0 else (None, 0.0, None)
    # Compare against the first note that could have produced the onset: with sound ringing
    # at the start, notes pressed before it died away are hidden inside the ringing.
    after = [t for t in m["note_times"] if quiet_from is None or t >= quiet_from - 0.02]
    first_note = after[0] if after else None
    return {"quiet_from": quiet_from, "sr": sr, "channels": x.shape[1], "subtype": info.subtype, "frames": len(x),
            "wav_dur": len(x) / sr, "peak": peak, "rms": rms, "onset": onset, "threshold": thr,
            "first_note": first_note,
            "offset": (onset - first_note) if (onset is not None and first_note is not None) else None,
            **{k: m[k] for k in ("duration", "cc64", "other_cc", "type", "tpb", "messages")},
            "notes": len(m["note_times"])}


def report(wav, mid, tol=0.05, dur_tol=0.01):
    """Return (text, ok); ok is None when the sync check is inconclusive."""
    a = analyze(wav, mid)

    def dbfs(v):
        return f"{20 * np.log10(v):.1f} dBFS" if v > 0 else "-inf dBFS (all zero)"

    lines = [f"WAV  {wav}", f"MID  {mid}",
             f"audio : {a['wav_dur']:.3f} s, {a['frames']} frames, {a['sr']} Hz, "
             f"{a['channels']} ch, {a['subtype']}, peak {dbfs(a['peak'])}, RMS {dbfs(a['rms'])}",
             f"midi  : {a['duration']:.3f} s, type {a['type']}, {a['tpb']} TPB, {a['notes']} notes, "
             f"{a['cc64']} sustain (CC64), {a['other_cc']} other CC, {a['messages']} messages"]
    ok = True
    checks = []
    if a["peak"] == 0.0:
        ok = False
        checks.append("FAIL audio is digital silence (all samples 0): see README > Troubleshooting")
    dd = a["wav_dur"] - a["duration"]
    good = bool(abs(dd) <= dur_tol)
    ok = ok and good
    checks.append(f"{'ok  ' if good else 'FAIL'} length difference wav - mid = {dd * 1000:+.1f} ms "
                  f"(tolerance ±{dur_tol * 1000:.0f} ms)")
    ringing = a["quiet_from"] is not None
    if a["notes"] == 0:
        checks.append("skip offset check: no note_on in the MIDI file")
    elif ringing and (a["onset"] is None or a["first_note"] is None):
        ok = None
        checks.append("INCONCLUSIVE audio is already sounding at 0 s (notes ringing from before the "
                      "take) and never goes quiet before a note, so no onset can be measured. "
                      "Start a take in silence to check.")
    elif a["onset"] is None:
        ok = False
        checks.append("FAIL no audio onset found above the threshold")
    else:
        off = a["offset"]
        good = bool(abs(off) <= tol)
        ok = ok and good
        if ringing:
            checks.append(f"note: sound was ringing at 0 s (from before the take) until "
                          f"{a['quiet_from']:.3f} s; measuring the first note after that")
        checks.append(f"first note_on {a['first_note']:.4f} s, first audio onset {a['onset']:.4f} s "
                      f"(threshold {dbfs(a['threshold'])})")
        checks.append(f"{'ok  ' if good else 'FAIL'} offset audio - MIDI = {off * 1000:+.1f} ms "
                      f"(tolerance ±{tol * 1000:.0f} ms; positive = audio after MIDI)")
    lines += checks
    lines.append("RESULT: " + {True: "PASS", False: "FAIL", None: "INCONCLUSIVE"}[ok])
    return "\n".join(lines), ok


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pianotake-check", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("take", nargs="?", help=".wav, .mid, base name, or folder (default: newest take)")
    ap.add_argument("--tol", type=float, default=0.05, help="max |offset| in seconds (default 0.05)")
    args = ap.parse_args(argv)
    try:
        wav, mid = resolve(args.take)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    text, ok = report(wav, mid, tol=args.tol)
    print(text)
    return 0 if ok is True else 1


if __name__ == "__main__":
    sys.exit(main())
