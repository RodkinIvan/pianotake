"""Core of pianotake: device discovery, the pre-flight check and the Take recorder.
No GUI code lives here; the GUI and the terminal mode both build on it.

Sync: every MIDI message is timestamped with the PortAudio stream clock
(`stream.time`) when it arrives, and converted to seconds relative to the
stream-clock ADC time of the first recorded audio sample
(`inputBufferAdcTime` of the first callback). Both files therefore start at
the same instant: sample 0 of the WAV == tick 0 of the MIDI.
"""
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
import mido

CHANNELS = 2              # record the first 1-2 inputs of the chosen device
TPB = 960                 # MIDI ticks per beat
TEMPO = 500000            # 120 bpm (1 beat = 0.5 s) -> 1 tick ~ 0.52 ms
NO_SIGNAL_SECONDS = 5.0   # warn live if the take is still exact digital zero after this
DROP_TYPES = {"clock", "active_sensing"}
NO_MIDI = "(none, audio only)"
SFC_UPDATE_HEADER_NOW = 0x1060  # libsndfile command; not exported by soundfile
_STOP = object()

# Words that say nothing about which instrument a port belongs to.
GENERIC_WORDS = {"usb", "audio", "midi", "in", "out", "input", "output", "port", "device",
                 "interface", "line", "mic", "microphone", "analog", "digital", "stereo",
                 "client", "driver", "core", "ch", "bus", "the", "and", "hw"}
# Software / system ports that should never be picked automatically.
VIRTUAL_AUDIO = ("background music", "blackhole", "loopback", "soundflower", "zoomaudiodevice",
                 "microsoft teams", "aggregate", "multi-output", "default", "sysdefault",
                 "pulse", "pipewire", "sound mapper", "primary sound", "stereo mix", "virtual")
VIRTUAL_MIDI = ("iac driver", "midi through", "through port", "network", "session",
                "loopmidi", "microsoft gs", "virtual")
ROUTING_APPS = ("Background Music", "SoundSource", "Loopback", "Audio Hijack")


# --------------------------------------------------------------------------- helpers

def db(x):
    return 20.0 * np.log10(max(float(x), 1e-12))


def fmt_db(x):
    return f"{db(x):.1f} dBFS" if x > 0 else "-inf dBFS"


def with_retries(fn, what, attempts=4, delay=0.25, log=None):
    """Call fn() up to `attempts` times with exponential backoff; raise the last error."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - device errors come in many types
            last = e
            if i < attempts - 1:
                if log:
                    log(f"{what} failed ({e}); retrying ({i + 1}/{attempts - 1})...")
                time.sleep(delay * (2 ** i))
    raise RuntimeError(f"{what} failed after {attempts} attempts: {last}")


def refresh_portaudio():
    """PortAudio caches the device list at init; re-init to see hot-plugged devices.
    Only call when no stream is open."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:  # noqa: BLE001
        pass


def list_audio_inputs(max_channels=CHANNELS):
    devs = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            devs.append({"index": i, "name": d["name"],
                         "channels": min(max_channels, d["max_input_channels"]),
                         "samplerate": int(d["default_samplerate"])})
    return devs


def list_midi_inputs():
    return list(mido.get_input_names())


# --------------------------------------------------------------------------- discovery

def name_words(name):
    """Distinctive lowercase words of a port name: 'Roland Digital Piano' -> {roland, piano},
    'P-125: USB Audio (hw:2,0)' -> {p-125}. Hyphenated model names stay one word."""
    return {w for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", name.lower())
            if len(w) > 1 and not w.isdigit() and w not in GENERIC_WORDS}


def is_virtual_audio(name):
    return any(v in name.lower() for v in VIRTUAL_AUDIO)


def is_virtual_midi(name):
    return any(v in name.lower() for v in VIRTUAL_MIDI)


def find_by_substring(names, hint):
    """First name containing `hint` (case-insensitive), or None."""
    if not hint:
        return None
    return next((n for n in names if hint.lower() in n.lower()), None)


def pick_devices(audio_names, midi_names, audio_hint=None, midi_hint=None):
    """Choose (audio_name, midi_name) for an instrument; either may be None.

    1. Explicit substrings (`--audio` / `--midi`) win.
    2. Otherwise pair an audio input with a MIDI input that shares a distinctive name
       word: a USB instrument usually shows up under one name on both sides
       ("Roland Digital Piano", "FP-30X", "P-125", "Clavinova").
    3. Otherwise, if exactly one hardware MIDI input exists, take it; audio stays unset
       (the user picks it once, e.g. an audio interface, and the choice is remembered).
    """
    a = find_by_substring(audio_names, audio_hint)
    m = find_by_substring(midi_names, midi_hint)
    if a and m:
        return a, m
    hw_audio = [n for n in audio_names if not is_virtual_audio(n)] if a is None else [a]
    hw_midi = [n for n in midi_names if not is_virtual_midi(n)] if m is None else [m]
    best = None
    for an in hw_audio:
        for mn in hw_midi:
            score = len(name_words(an) & name_words(mn))
            if score and (best is None or score > best[0]):
                best = (score, an, mn)
    if best:
        return best[1], best[2]
    if m is None and len(hw_midi) == 1:
        m = hw_midi[0]
    return a, m


def resolve_audio_index(name, channels):
    """Device indices change on hot-plug; look the device up by name, refreshing once."""
    for attempt in range(2):
        for d in list_audio_inputs():
            if d["name"] == name and d["channels"] >= channels:
                return d["index"]
        if attempt == 0:
            refresh_portaudio()
    raise RuntimeError(f"audio input '{name}' not found")


def midi_output_for(midi_in_name):
    """The instrument's MIDI output (for the test chord): same name, or most shared words."""
    if not midi_in_name:
        return None
    outs = mido.get_output_names()
    if midi_in_name in outs:
        return midi_in_name
    scored = [(len(name_words(o) & name_words(midi_in_name)), o) for o in outs if not is_virtual_midi(o)]
    scored = [s for s in scored if s[0]]
    return max(scored)[1] if scored else None


# --------------------------------------------------------------------------- files

def unique_base(folder, name):
    name = re.sub(r'[/\\:*?"<>|\x00]', "_", name).strip() or "take"
    base = Path(folder) / name
    i = 2
    while Path(f"{base}.wav").exists() or Path(f"{base}.mid").exists():
        base = Path(folder) / f"{name}_{i}"
        i += 1
    return base


def sync_wav_header(f):
    """Rewrite the WAV header with the current length and fsync, so a crash or power
    loss still leaves a valid file containing everything written so far."""
    try:
        sf._snd.sf_command(f._file, SFC_UPDATE_HEADER_NOW, sf._ffi.NULL, 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        f.flush()
    except Exception:  # noqa: BLE001
        pass


def reveal(path):
    """Show a file in Finder / Explorer / the file manager."""
    path = str(path)
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path])
    elif sys.platform.startswith("win"):
        subprocess.run(["explorer", f"/select,{path}"])
    else:
        subprocess.run(["xdg-open", str(Path(path).parent)])


# --------------------------------------------------------------------------- pre-flight

def probe_input(dev_index, channels, samplerate, seconds=1.0, midi_out_name=None):
    """Record `seconds` from the input and measure it. If midi_out_name is given, play a
    short soft chord on the instrument through MIDI so the test doesn't depend on the player."""
    blocks = []
    flags = [0]

    def cb(indata, frames, t, status):
        if status:
            flags[0] += 1
        blocks.append(indata.copy())

    out = None
    if midi_out_name:
        try:
            out = mido.open_output(midi_out_name)
        except Exception:  # noqa: BLE001
            out = None
    try:
        with sd.InputStream(device=dev_index, channels=channels, samplerate=samplerate,
                            dtype="float32", callback=cb):
            if out:
                chord = (60, 64, 67)
                time.sleep(0.15)
                for n in chord:
                    out.send(mido.Message("note_on", note=n, velocity=55))
                time.sleep(0.6)
                for n in chord:
                    out.send(mido.Message("note_off", note=n, velocity=0))
                time.sleep(max(0.0, seconds - 0.75))
            else:
                time.sleep(seconds)
    finally:
        if out:
            out.close()
    x = np.concatenate(blocks) if blocks else np.zeros((0, channels), np.float32)
    return {"frames": len(x), "peak": float(np.abs(x).max()) if len(x) else 0.0,
            "rms": float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if len(x) else 0.0,
            "all_zero": bool(len(x)) and not np.any(x), "played": out is not None,
            "flags": flags[0], "seconds": seconds}


def host_app():
    """Best guess of the macOS app whose Microphone permission applies to this process."""
    bid = os.environ.get("__CFBundleIdentifier", "")
    known = {"com.apple.Terminal": "Terminal", "com.googlecode.iterm2": "iTerm",
             "com.microsoft.VSCode": "Visual Studio Code", "dev.warp.Warp-Stable": "Warp",
             "com.mitchellh.ghostty": "Ghostty", "net.kovidgoyal.kitty": "kitty",
             "org.alacritty": "Alacritty", "com.jetbrains.pycharm": "PyCharm",
             "com.jetbrains.pycharm.ce": "PyCharm"}
    by_term = {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "vscode": "Visual Studio Code",
               "WarpTerminal": "Warp", "ghostty": "Ghostty"}
    name = known.get(bid) or by_term.get(os.environ.get("TERM_PROGRAM", ""))
    if name:
        return name, bid
    if bid:
        return bid, bid
    return f"the app that launched {sys.executable}", ""


def running_routing_apps():
    """Audio routing apps that are running right now (macOS only)."""
    if sys.platform != "darwin":
        return []
    found = []
    for app in ROUTING_APPS:
        try:
            if subprocess.run(["pgrep", "-x", app], capture_output=True).returncode == 0:
                found.append(app)
        except Exception:  # noqa: BLE001
            pass
    return found


def permission_help():
    """Step 1 of the silence checklist, for the current OS."""
    if sys.platform == "darwin":
        app, bid = host_app()
        reset = (f"\n   Not listed? Run:  tccutil reset Microphone {bid}  then relaunch {app} to get "
                 "the prompt." if bid else "")
        return ("1. Microphone permission. macOS hands out silence (no error) when the app that runs\n"
                "   Python may not use audio input. Fix: System Settings > Privacy & Security >\n"
                f"   Microphone > turn on \"{app}\", then quit and reopen {app} and run this again.{reset}\n")
    if sys.platform.startswith("win"):
        return ("1. Microphone privacy. Windows can block audio input for desktop apps. Fix: Settings >\n"
                "   Privacy & security > Microphone > turn on \"Microphone access\" and \"Let desktop\n"
                "   apps access your microphone\", then restart this app.\n")
    return ("1. Input muted or blocked. Check the device's capture level isn't muted (alsamixer,\n"
            "   pavucontrol) and that your user may use audio devices (e.g. the 'audio' group).\n")


def silence_help(where="the input"):
    running = running_routing_apps()
    now = f" ({', '.join(running)} is running right now)" if running else ""
    return (
        f"NO AUDIO: {where} delivered digital silence (every sample is exactly 0).\n"
        "A live USB audio stream normally carries at least a tiny noise floor, so exact\n"
        "zeros mean something is blocking the input. Likely causes, most likely first:\n"
        + permission_help() +
        f"2. An audio routing app{now} (Background Music, SoundSource, Loopback, ...)\n"
        "   intercepting the device. Quit it and test again.\n"
        "3. The instrument isn't sending its sound over USB. Check that Local Control is On\n"
        "   (Local Off disconnects the keys from the sound engine) and look in its manual for\n"
        "   USB audio settings. Many instruments never send demo songs over USB.\n"
        "   Re-plug the USB cable / power-cycle the instrument.\n"
        "4. Wrong device. Choose your instrument (or the audio interface it is plugged into)\n"
        "   under Audio in. pianotake records the device's first two inputs.\n"
        "Press \"Test audio\" after each fix: it plays a short chord through MIDI and measures it.")


def diagnose(res, name):
    """Turn a probe result into (level, message). level: ok | warn | err."""
    head = f"Pre-flight on {name}: "
    if res["frames"] == 0:
        return "err", head + "the stream opened but delivered no audio buffers.\n" + silence_help()
    stats = f"peak {fmt_db(res['peak'])}, RMS {fmt_db(res['rms'])} over {res['seconds']:.1f} s"
    if res["all_zero"]:
        return "err", head + stats + "\n" + silence_help()
    if res["peak"] < 10 ** (-80 / 20):
        if res["played"]:
            return "warn", (head + stats + "\nThe input is live (noise floor only) but the test chord "
                            "did not come through. Check that Local Control is On and that this is the "
                            "input your instrument's sound arrives on, then play a few keys and press "
                            "Test audio again.")
        return "ok", (head + stats + "\nInput is live (noise floor only, nothing playing). "
                      "Play a few notes or press Test audio to hear it register.")
    return "ok", head + stats + ("  (test chord played via MIDI)" if res["played"] else "") + "\nAudio OK."


# --------------------------------------------------------------------------- recording

@dataclass
class TakeResult:
    wav_path: Path
    mid_path: Path
    duration: float
    frames: int
    samplerate: int
    note_ons: int
    midi_events: int
    overflows: int
    other_flags: int
    max_peak: float
    stop_reason: str = None          # None = stopped by the user
    problems: list = field(default_factory=list)


class Take:
    """One recording. GUI-agnostic: progress goes out through post((kind, ...)).

    Threads: PortAudio callback -> audio queue -> writer thread -> WAV on disk.
    rtmidi callback -> list of (stream_time, msg). A monitor thread watches for an
    unplugged device. stop() finalizes on its own thread and always saves."""

    def __init__(self, audio_name, channels, samplerate, midi_name, base_path, post=None):
        self.audio_name, self.channels, self.sr = audio_name, channels, samplerate
        self.midi_name = midi_name
        self.wav_path = Path(f"{base_path}.wav")
        self.mid_path = Path(f"{base_path}.mid")
        self.post = post or (lambda m: None)
        self.stream = self.port = self.sf = None
        self.audio_q = queue.Queue()
        self.first_adc = None
        self.frames_captured = 0
        self.frames_written = 0
        self.max_peak = 0.0
        self.overflows = 0
        self.other_flags = 0
        self.midi_events = []
        self.note_ons = 0
        self.accept_midi = False
        self.last_cb = None
        self.started_at = None
        self.problems = []
        self.stop_reason = None
        self.result = None
        self.finished = threading.Event()
        self._stopping = False
        self._stop_lock = threading.Lock()
        self._audio_closed = False
        self._writer_th = None
        self._lvl_peak, self._lvl_frames = 0.0, 0
        self._lvl_every = max(1, int(samplerate * 0.05))  # ~20 level updates/s

    def _log(self, text, level="warn"):
        self.post(("log", level, text))

    # -- start ---------------------------------------------------------------
    def start(self):
        """Open WAV, MIDI and audio (with retries). On failure nothing is left open."""
        self.wav_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.sf = sf.SoundFile(str(self.wav_path), "w", samplerate=self.sr,
                                   channels=self.channels, subtype="PCM_24", format="WAV")
            self._writer_th = threading.Thread(target=self._writer, name="wav-writer", daemon=True)
            self._writer_th.start()
            if self.midi_name:
                self.port = with_retries(
                    lambda: mido.open_input(self.midi_name, callback=self._midi_cb),
                    f"Opening MIDI '{self.midi_name}'", log=self._log)
            self.started_at = time.monotonic()
            self.stream = with_retries(self._open_stream, f"Opening audio '{self.audio_name}'",
                                       log=self._log)
            self.accept_midi = True
        except Exception:
            self._abort_start()
            raise
        threading.Thread(target=self._monitor, name="take-monitor", daemon=True).start()

    def _open_stream(self):
        idx = resolve_audio_index(self.audio_name, self.channels)
        st = sd.InputStream(device=idx, channels=self.channels, samplerate=self.sr,
                            dtype="float32", callback=self._audio_cb)
        try:
            st.start()
        except Exception:
            st.close()
            raise
        return st

    def _abort_start(self):
        self._stopping = True
        for closer in (lambda: self.port and self.port.close(),
                       lambda: self.stream and self.stream.close()):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        if self._writer_th:
            self.audio_q.put(_STOP)
            self._writer_th.join(5)
        try:
            if self.frames_written == 0:
                self.wav_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    # -- callbacks (no Tk here!) ----------------------------------------------
    def _audio_cb(self, indata, frames, t, status):
        if self._audio_closed:
            return
        self.last_cb = time.monotonic()
        if status:
            if status.input_overflow:
                self.overflows += 1
            else:
                self.other_flags += 1
        if self.first_adc is None:
            self.first_adc = t.inputBufferAdcTime or t.currentTime
        self.audio_q.put(indata.copy())
        self.frames_captured += frames
        pk = float(np.abs(indata).max()) if frames else 0.0
        if pk > self.max_peak:
            self.max_peak = pk
        self._lvl_peak = max(self._lvl_peak, pk)
        self._lvl_frames += frames
        if self._lvl_frames >= self._lvl_every:
            self.post(("level", self._lvl_peak))
            self._lvl_peak, self._lvl_frames = 0.0, 0

    def _midi_cb(self, msg):
        if not self.accept_midi or msg.type in DROP_TYPES or self.stream is None:
            return
        try:
            t = self.stream.time       # same clock as inputBufferAdcTime
        except Exception:  # noqa: BLE001
            return
        self.midi_events.append((t, msg))
        if msg.type == "note_on" and msg.velocity > 0:
            self.note_ons += 1
        self.post(("midi", self.note_ons))

    # -- background threads ----------------------------------------------------
    def _writer(self):
        """Drain the audio queue to disk. Every 0.5 s the WAV header is rewritten and
        every 2 s a MIDI checkpoint is saved, so even a hard crash leaves both files."""
        last_sync = last_midi = time.monotonic()
        try:
            while True:
                try:
                    blk = self.audio_q.get(timeout=0.25)
                except queue.Empty:
                    blk = None
                if blk is _STOP:
                    break
                if blk is not None:
                    self.sf.write(blk)
                    self.frames_written += len(blk)
                now = time.monotonic()
                if now - last_sync >= 0.5:
                    sync_wav_header(self.sf)
                    last_sync = now
                if now - last_midi >= 2.0:
                    last_midi = now
                    try:
                        self._write_midi(self.frames_written / self.sr, checkpoint=True)
                    except Exception:  # noqa: BLE001 - the final write reports errors
                        pass
        except Exception as e:  # noqa: BLE001 - e.g. disk full
            self.problems.append(f"writing audio failed: {e}")
            self._log(f"Writing audio failed: {e}", "err")
            self.stop(f"disk write failed: {e}")
        finally:
            try:
                self.sf.close()
            except Exception:  # noqa: BLE001
                pass

    def _monitor(self):
        """Detect an unplugged device: callbacks stop, the stream dies, or MIDI vanishes."""
        midi_miss, next_midi = 0, time.monotonic() + 2.0
        while not self._stopping:
            time.sleep(0.25)
            if self._stopping:
                return
            now = time.monotonic()
            last = self.last_cb
            if last is None and now - self.started_at > 3.0:
                return self.stop("no audio buffers arrived from the audio device")
            if last is not None and now - last > 1.5:
                return self.stop("audio device stopped delivering audio (unplugged?)")
            try:
                active = self.stream.active
            except Exception:  # noqa: BLE001
                active = False
            if not active and not self._stopping:
                return self.stop("audio stream stopped unexpectedly")
            if self.midi_name and now >= next_midi:
                next_midi = now + 2.0
                try:
                    present = self.midi_name in mido.get_input_names()
                except Exception:  # noqa: BLE001
                    present = True
                midi_miss = 0 if present else midi_miss + 1
                if midi_miss >= 2:
                    return self.stop("MIDI port disappeared (unplugged?)")

    # -- stop ------------------------------------------------------------------
    def stop(self, reason=None):
        """Request stop (any thread). Finalizing runs on its own non-daemon thread."""
        with self._stop_lock:
            if self._stopping:
                return
            self._stopping = True
            self.stop_reason = reason
        if reason:
            self._log(f"Stopping take: {reason}. Saving what was captured...", "warn")
        threading.Thread(target=self._finalize, name="take-finalize").start()

    def wait(self, timeout=None):
        self.finished.wait(timeout)
        return self.result

    @staticmethod
    def _run_with_timeout(fn, seconds):
        th = threading.Thread(target=fn, daemon=True)
        th.start()
        th.join(seconds)
        return not th.is_alive()

    def _finalize(self):
        self.accept_midi = False
        if self.port:
            def close_port():
                try:
                    self.port.close()
                except Exception:  # noqa: BLE001
                    pass
            if not self._run_with_timeout(close_port, 3):
                self.problems.append("MIDI port did not close cleanly")

        def close_stream():
            try:
                if self.stop_reason:
                    self.stream.abort()   # device may be gone; don't wait for buffers
                else:
                    self.stream.stop()    # drain the last buffers
            except Exception:  # noqa: BLE001
                pass
            try:
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
        if not self._run_with_timeout(close_stream, 4):
            self.problems.append("audio stream did not close cleanly (device gone?)")
        self._audio_closed = True
        self.audio_q.put(_STOP)
        self._writer_th.join(30)

        duration = self.frames_written / self.sr
        try:
            self._write_midi(duration)
        except Exception as e:  # noqa: BLE001
            self.problems.append(f"writing MIDI failed: {e}")
        self.result = TakeResult(self.wav_path, self.mid_path, duration, self.frames_written,
                                 self.sr, self.note_ons, len(self.midi_events), self.overflows,
                                 self.other_flags, self.max_peak, self.stop_reason,
                                 list(self.problems))
        self.finished.set()
        self.post(("stopped", self.result))

    def _write_midi(self, duration, checkpoint=False):
        """Type-0 SMF. Tick 0 = first audio sample; end_of_track = audio duration."""
        events = [(t, m) for t, m in list(self.midi_events)
                  if not m.is_realtime and (m.type == "sysex" or hasattr(m, "channel"))]
        if self.first_adc is not None:
            base = self.first_adc
        else:  # no audio at all: fall back to the first MIDI event
            base = events[0][0] if events else 0.0
            duration = max(duration, (events[-1][0] - base) if events else 0.0)
        end_tick = round(mido.second2tick(duration, TPB, TEMPO))

        mid = mido.MidiFile(type=0, ticks_per_beat=TPB)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        track.append(mido.MetaMessage("set_tempo", tempo=TEMPO, time=0))
        track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
        prev = 0
        held, sustained = set(), set()
        for t, msg in events:
            tick = round(mido.second2tick(max(0.0, t - base), TPB, TEMPO))
            tick = min(end_tick, max(prev, tick))
            track.append(msg.copy(time=tick - prev))
            prev = tick
            if msg.type == "note_on" and msg.velocity > 0:
                held.add((msg.channel, msg.note))
            elif msg.type in ("note_on", "note_off"):
                held.discard((msg.channel, msg.note))
            elif msg.type == "control_change" and msg.control == 64:
                (sustained.add if msg.value >= 64 else sustained.discard)(msg.channel)
        # Close notes / pedal still down when the take ended so viewers don't hang them.
        for ch, note in sorted(held):
            track.append(mido.Message("note_off", channel=ch, note=note, velocity=0,
                                      time=end_tick - prev))
            prev = end_tick
        for ch in sorted(sustained):
            track.append(mido.Message("control_change", channel=ch, control=64, value=0,
                                      time=end_tick - prev))
            prev = end_tick
        track.append(mido.MetaMessage("end_of_track", time=end_tick - prev))
        tmp = self.mid_path.with_name(self.mid_path.name + ".tmp")
        mid.save(str(tmp))
        os.replace(tmp, self.mid_path)   # atomic: a crash never leaves a half-written .mid
