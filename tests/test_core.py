"""Hardware-free tests: device pairing, MIDI writing, the recording pipeline with fake
audio, and the take checker on synthetic files."""
import types

import mido
import numpy as np
import pytest
import soundfile as sf

from pianotake import check, cli
from pianotake import recorder as r

SR = 44100


# --------------------------------------------------------------------------- discovery

@pytest.mark.parametrize("audio, midi, expected", [
    # macOS: same name on both sides
    (["MacBook Pro Microphone", "Roland Digital Piano", "Background Music"],
     ["IAC Driver Bus 1", "Roland Digital Piano"], ("Roland Digital Piano", "Roland Digital Piano")),
    # Windows-style names
    (["Microphone Array (Realtek)", "Line (2- FP-30X)"], ["FP-30X", "Microsoft GS Wavetable Synth"],
     ("Line (2- FP-30X)", "FP-30X")),
    # Linux ALSA-style names
    (["HDA Intel PCH: ALC257 Analog (hw:0,0)", "P-125: USB Audio (hw:2,0)"],
     ["Midi Through:Midi Through Port-0 14:0", "P-125:P-125 MIDI 1 20:0"],
     ("P-125: USB Audio (hw:2,0)", "P-125:P-125 MIDI 1 20:0")),
    # piano into an audio interface: names don't match, the lone MIDI port is still found
    (["Scarlett 2i2 USB", "MacBook Pro Microphone"], ["Digital Piano", "IAC Driver Bus 1"],
     (None, "Digital Piano")),
    # nothing plugged in: virtual devices are never picked
    (["Background Music", "ZoomAudioDevice"], ["IAC Driver Bus 1"], (None, None)),
])
def test_pick_devices(audio, midi, expected):
    assert r.pick_devices(audio, midi) == expected


def test_pick_devices_hints_win():
    audio = ["Roland Digital Piano", "Scarlett 2i2 USB"]
    midi = ["Roland Digital Piano"]
    assert r.pick_devices(audio, midi, audio_hint="scarlett") == ("Scarlett 2i2 USB", "Roland Digital Piano")


def test_name_words_ignores_generic_words():
    assert r.name_words("Roland Digital Piano") == {"roland", "piano"}
    assert r.name_words("USB Audio Device 2") == set()
    assert r.name_words("Line (2- FP-30X)") == {"fp-30x"}
    assert r.name_words("P-125: USB Audio (hw:2,0)") == {"p-125"}


def test_unique_base(tmp_path):
    (tmp_path / "take.wav").write_bytes(b"")
    assert r.unique_base(tmp_path, "take").name == "take_2"
    assert r.unique_base(tmp_path, 'a/b:c').name == "a_b_c"


# --------------------------------------------------------------------------- MIDI

def make_take(tmp_path, midi_name="Piano"):
    return r.Take("Fake input", 2, SR, midi_name, tmp_path / "t")


def test_midi_callback_drops_only_clock_and_active_sensing(tmp_path):
    take = make_take(tmp_path)
    take.stream = types.SimpleNamespace(time=5.0)
    take.accept_midi = True
    for msg in (mido.Message("clock"), mido.Message("active_sensing"),
                mido.Message("note_on", note=60, velocity=90),
                mido.Message("control_change", control=64, value=127),
                mido.Message("control_change", control=67, value=127),
                mido.Message("note_on", note=60, velocity=0)):
        take._midi_cb(msg)
    assert [m.type for _, m in take.midi_events] == ["note_on", "control_change", "control_change", "note_on"]
    assert take.note_ons == 1


def test_write_midi_aligns_to_first_sample_and_ends_at_audio_length(tmp_path):
    take = make_take(tmp_path)
    take.first_adc = 100.0
    take.midi_events = [
        (99.99, mido.Message("note_on", note=59, velocity=40)),   # just before sample 0 -> tick 0
        (100.5, mido.Message("note_on", note=60, velocity=80)),
        (100.6, mido.Message("control_change", control=64, value=127)),
        (101.0, mido.Message("note_off", note=60)),
        (101.2, mido.Message("note_on", note=62, velocity=70)),  # still held at the end
    ]
    take._write_midi(2.0)
    info = check.midi_info(take.mid_path)
    assert info["note_times"] == pytest.approx([0.0, 0.5, 1.2], abs=1e-3)
    assert info["duration"] == pytest.approx(2.0, abs=1e-3)
    assert info["cc64"] == 2                                     # pedal down + released at the end
    msgs = [m for m in mido.MidiFile(take.mid_path).tracks[0] if not m.is_meta]
    assert {(m.type, getattr(m, "note", None)) for m in msgs[-3:]} >= {("note_off", 59), ("note_off", 62)}


# --------------------------------------------------------------------------- pipeline

class FakeStream:
    active = True

    def stop(self):
        self.active = False

    abort = stop

    def close(self):
        pass


class Overflow:
    """Stands in for sounddevice.CallbackFlags with input_overflow set."""
    input_overflow = True

    def __bool__(self):
        return True


def test_take_pipeline_with_fake_audio(tmp_path):
    """Feed the audio callback like PortAudio would and check both files."""
    take = make_take(tmp_path)
    take.sf = sf.SoundFile(str(take.wav_path), "w", samplerate=SR, channels=2, subtype="PCM_24")
    take._writer_th = r.threading.Thread(target=take._writer, daemon=True)
    take._writer_th.start()
    take.stream = FakeStream()
    take.accept_midi = True

    t = np.arange(SR) / SR
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    audio = np.stack([tone, tone], axis=1)
    for i, start in enumerate(range(0, SR, 512)):
        block = audio[start:start + 512]
        ti = types.SimpleNamespace(inputBufferAdcTime=10.0 + start / SR, currentTime=10.0 + start / SR)
        status = Overflow() if i == 3 else 0
        take._audio_cb(block, len(block), ti, status)
        if i == 40:                                            # a note ~0.46 s in
            take.stream.time = 10.0 + start / SR
            take._midi_cb(mido.Message("note_on", note=69, velocity=100))
    take.stop()
    res = take.wait(10)
    assert res is not None and res.stop_reason is None and res.problems == []
    assert res.frames == SR and res.overflows == 1 and res.note_ons == 1
    x, sr = sf.read(str(take.wav_path), dtype="float32")
    assert sr == SR and np.allclose(x, audio, atol=2 ** -22)
    info = check.midi_info(take.mid_path)
    assert info["duration"] == pytest.approx(1.0, abs=1e-3)
    assert info["note_times"][0] == pytest.approx(40 * 512 / SR, abs=1e-3)


# --------------------------------------------------------------------------- checker

def write_take(tmp_path, onsets, midi_notes, seconds=3.0, ring_at_start=False):
    n = int(seconds * SR)
    x = np.zeros(n)
    t = np.arange(int(0.8 * SR)) / SR
    burst = 0.4 * np.sin(2 * np.pi * 330 * t) * np.exp(-t * 6)
    for on in onsets:
        i = int(on * SR)
        x[i:i + len(burst)] += burst[: n - i]
    if ring_at_start:
        x[: int(0.3 * SR)] += 0.05 * np.sin(2 * np.pi * 220 * np.arange(int(0.3 * SR)) / SR) * \
            np.linspace(1, 0, int(0.3 * SR))
    base = tmp_path / "take"
    sf.write(f"{base}.wav", np.stack([x, x], 1), SR, subtype="PCM_24")
    mid = mido.MidiFile(type=0, ticks_per_beat=960)
    tr = mido.MidiTrack()
    mid.tracks.append(tr)
    tr.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    prev = 0
    for s in midi_notes:
        tick = round(mido.second2tick(s, 960, 500000))
        tr.append(mido.Message("note_on", note=64, velocity=80, time=tick - prev))
        prev = tick
    tr.append(mido.MetaMessage("end_of_track", time=round(mido.second2tick(seconds, 960, 500000)) - prev))
    mid.save(f"{base}.mid")
    return base


def test_check_passes_aligned_take(tmp_path):
    base = write_take(tmp_path, onsets=[1.003, 2.0], midi_notes=[1.0, 2.0])
    text, ok = check.report(f"{base}.wav", f"{base}.mid")
    assert ok is True, text
    assert check.analyze(f"{base}.wav", f"{base}.mid")["offset"] == pytest.approx(0.003, abs=0.002)


def test_check_measures_after_ringing_at_start(tmp_path):
    base = write_take(tmp_path, onsets=[1.0], midi_notes=[1.0], ring_at_start=True)
    text, ok = check.report(f"{base}.wav", f"{base}.mid")
    assert ok is True and "ringing" in text, text


def test_check_fails_offset_and_silence(tmp_path):
    late = write_take(tmp_path, onsets=[1.2], midi_notes=[1.0])
    assert check.report(f"{late}.wav", f"{late}.mid")[1] is False
    (tmp_path / "s").mkdir()
    silent = write_take(tmp_path / "s", onsets=[], midi_notes=[1.0])
    text, ok = check.report(f"{silent}.wav", f"{silent}.mid")
    assert ok is False and "digital silence" in text


def test_check_main_exit_codes(tmp_path, capsys):
    base = write_take(tmp_path, onsets=[1.0], midi_notes=[1.0])
    assert check.main([f"{base}.wav"]) == 0
    assert check.main([str(tmp_path / "missing.wav")]) == 2


# --------------------------------------------------------------------------- CLI / GUI

def test_cli_args():
    a = cli.parse_args(["--audio", "scarlett", "--midi", "none", "--channels", "1", "--cli"])
    assert (a.audio, a.midi, a.channels, a.cli) == ("scarlett", "none", 1, True)


def test_gui_module_imports():
    pytest.importorskip("tkinter")
    from pianotake import gui
    assert hasattr(gui, "App")


# --------------------------------------------------------------------------- MIDI only

def test_midi_only_take_writes_only_a_mid(tmp_path):
    take = r.Take(None, 0, 0, "Piano", tmp_path / "m")
    assert take.wav_path is None
    take.first_adc = r.time.perf_counter()      # what start() does for a MIDI-only take
    take.accept_midi = True
    take._midi_cb(mido.Message("note_on", note=60, velocity=90))
    r.time.sleep(0.3)
    take._midi_cb(mido.Message("note_off", note=60))
    r.time.sleep(0.2)
    take.stop()
    res = take.wait(10)
    assert res.wav_path is None and not list(tmp_path.glob("*.wav"))
    # sleep() only guarantees a minimum, and shared CI machines overshoot a lot: the take
    # lasts at least the 0.5 s slept, and the MIDI file must end exactly where the take did.
    assert res.note_ons == 1 and 0.5 <= res.duration < 5
    info = check.midi_info(take.mid_path)
    assert info["note_times"][0] == pytest.approx(0.0, abs=0.05)
    assert info["duration"] == pytest.approx(res.duration, abs=1e-3)


def test_cli_choose_midi_only():
    args = cli.parse_args(["--audio", "none"])
    a, m = cli.choose(args, ["Roland Digital Piano"], ["Roland Digital Piano"])
    assert (a, m) == (r.NO_AUDIO, "Roland Digital Piano")


def test_tilde_hides_the_home_folder():
    from pathlib import Path
    from pianotake.settings import tilde
    assert tilde(Path.home() / "Music" / "x.wav") == str(Path("~") / "Music" / "x.wav")
    assert tilde("/elsewhere/x.wav") == "/elsewhere/x.wav"


def test_cli_survives_a_system_without_midi(monkeypatch, capsys):
    def broken():
        raise RuntimeError("MidiInAlsa::initialize: error creating ALSA sequencer client object.")
    monkeypatch.setattr(r, "list_midi_inputs", broken)
    assert cli.midi_inputs(r) == []
    assert "MIDI is unavailable" in capsys.readouterr().err
