# pianotake

**One button records your digital piano. Every take saves a WAV of the sound and a
MIDI file of what you played, perfectly lined up.**

Plug a digital piano or MIDI keyboard into your computer with USB, press Space, play,
press Space again. You get:

- `take_20261018_201500.wav`: the instrument's own sound (24-bit WAV)
- `take_20261018_201500.mid`: every note, the sustain pedal and other controllers

No audio cable or USB audio? Choose *(none, MIDI only)* as the audio input to record just
the MIDI.

Both files start at the same instant and have the same length. Drop them into a
MIDI visualizer, a piano-roll video tool or a DAW, and the notes sit exactly on the sound.
No DAW, no routing, no setup.

## Features

- **One window, one button.** A big record button (Space toggles it), elapsed time, a
  level meter, MIDI activity and a note counter.
- **Finds your instrument by itself.** USB instruments show up with the same name for
  audio and MIDI ("Roland Digital Piano", "FP-30X", "P-125", ...), and pianotake pairs
  them. If your audio arrives through an audio interface, pick it once; it is remembered.
- **Sample-accurate sync.** MIDI is timestamped on the audio stream's own clock (see
  [How it works](#how-it-works)). Measured offset: a few milliseconds.
- **Catches silent recordings.** A pre-flight check and a live warning detect digital
  silence, the usual symptom of a missing microphone permission, and tell you how to fix it.
- **Never loses a take.** Audio is written to disk as it arrives. If the cable comes out
  mid-take, the take stops and everything so far is saved. Even a crash leaves usable files.
- **Self-check tool.** `pianotake-check` verifies that a take's WAV and MIDI line up.
- **Terminal mode** (`pianotake --cli`) for machines without a display.

## Install

Download the app for your system from
[Releases](https://github.com/RodkinIvan/pianotake/releases). It needs no Python.

| System | File | First launch |
|---|---|---|
| macOS, Apple Silicon | `…-macos-arm64.dmg` | Open the DMG and drag pianotake to Applications. |
| macOS, Intel | `…-macos-x86_64.dmg` | Same. |
| Windows 10/11 | `…-windows-x64.zip` | Unzip, then run `pianotake.exe` from the folder. |
| Linux (x86-64) | `…-linux-x86_64.tar.gz` | Unpack, install PortAudio (`sudo apt install libportaudio2`), run `./pianotake`. |

The apps aren't signed yet, so the first launch takes one extra step:

- **macOS** says it can't verify pianotake. Go to **System Settings → Privacy & Security**,
  scroll down, and click **Open Anyway** next to pianotake. After that it opens normally.
  (In a terminal, `xattr -dr com.apple.quarantine /Applications/pianotake.app` does the same.)
- **Windows** may show *Windows protected your PC*: click **More info → Run anyway**.

When pianotake first opens your instrument, macOS asks whether it may use audio input:
click **Allow**. To build the apps yourself, see [packaging](packaging/README.md).

**With Python** (any OS), Python 3.9 or newer:

```bash
pipx install git+https://github.com/RodkinIvan/pianotake
```

or, from a clone, `pip install .`. That installs two commands, `pianotake` and
`pianotake-check`; `python -m pianotake` works too.

The window uses Tkinter, which ships with the python.org and conda builds. With Homebrew
Python run `brew install python-tk`; on Debian/Ubuntu, `sudo apt install python3-tk`.
On Linux, also install PortAudio and the ALSA headers (python-rtmidi may compile itself):
`sudo apt install libportaudio2 libasound2-dev`.

## Allow audio input (important)

Your instrument's USB audio counts as a microphone, so the app that runs pianotake
needs microphone permission. Without it you get a WAV full of silence and no error.

- **macOS:** System Settings → Privacy & Security → Microphone → turn on the app you
  start pianotake from (Terminal, iTerm, Warp, VS Code, ...). Then **quit and reopen that
  app**. If the app isn't listed, run `tccutil reset Microphone <its bundle id>` and
  relaunch it; macOS will ask the next time pianotake opens the input.
- **Windows:** Settings → Privacy & security → Microphone → turn on *Microphone access*
  and *Let desktop apps access your microphone*.
- **Linux:** make sure the capture device isn't muted (`alsamixer` / `pavucontrol`).

pianotake checks this when it starts and tells you which app to enable.

## Use

```bash
pianotake
```

- **Space** or the red button starts and stops a take.
- Pick *(none, MIDI only)* under *Audio in* to record only MIDI, or *(none, audio only)*
  under *MIDI in* to record only sound.
- **Test audio** plays a short, soft chord on your instrument through MIDI and measures
  what comes back. It is the quickest way to prove the whole chain works.
- Takes go to `~/Music/PianoTakes` unless you choose another folder. The name defaults to
  `take_YYYYMMDD_HHMMSS`.
- After a take: the file paths, **Reveal in Finder** (or Explorer / your file manager),
  and **Check sync**.

Options:

```text
pianotake --list                 show audio inputs and MIDI ports, and what would be picked
pianotake --audio scarlett       use the audio input whose name contains "scarlett"
pianotake --midi none            record audio only
pianotake --audio none           record MIDI only
pianotake --channels 1           record a mono input
pianotake --out ~/Recordings     save takes somewhere else
pianotake --cli                  record in the terminal (Enter starts and stops)
```

## Check a take

```bash
pianotake-check                  # newest take in the output folder
pianotake-check take.wav
```

It prints both durations, the note and pedal counts, the audio level, and the offset
between the first note in the MIDI and the first sound in the WAV. It passes when the
lengths match within 10 ms and the offset is within ±50 ms. On a Roland FP-30X the
measured offset is +2 to +6 ms (the instrument's own sound-engine latency).

## How it works

- **Sync.** Every MIDI message is timestamped when it arrives using the PortAudio stream
  clock (`stream.time`). The first audio callback reports the same clock's time for
  sample 0 (`inputBufferAdcTime`). A MIDI event's position is its time minus the time of
  sample 0, so tick 0 of the MIDI file is sample 0 of the WAV. With one clock for both
  streams there is no drift to correct.
- **MIDI file.** Type 0, 960 ticks per beat at 120 BPM, so 1 tick ≈ 0.5 ms. The tempo is
  only a time grid; it isn't detected from your playing. `end_of_track` sits exactly at
  the audio length. Notes or the pedal still held when you stop are released at the end.
  Only `clock` and `active_sensing` messages are dropped.
- **Crash safety.** A writer thread streams audio into the WAV and rewrites its header
  every 0.5 s. A MIDI checkpoint is saved every 2 s. After a crash you lose under 0.5 s
  of audio and under 2 s of MIDI.
- **Unplugging.** If audio callbacks stop, the stream dies or the MIDI port disappears,
  the take stops and saves by itself.
- **Robustness.** Devices are looked up by name, so changing device numbers don't matter.
  Opening a device is retried with backoff. Input overflows (dropped audio blocks) are
  counted and reported after the take.

## Troubleshooting

**The WAV is silent.** pianotake shows *digital silence* when every sample is exactly 0.
A live USB stream normally has at least a tiny noise floor, so this means something is
blocking the input:

1. **Microphone permission** (most likely): see [Allow audio input](#allow-audio-input-important).
2. **An audio routing app** (Background Music, SoundSource, Loopback, ...) is intercepting
   the device. Quit it and press Test audio.
3. **The instrument isn't sending audio over USB.** Make sure *Local Control* is On, and
   check the manual for USB audio settings. Many instruments never send demo songs over USB.
4. **Wrong device.** Pick your instrument, or the interface it's plugged into, under
   *Audio in*.

**Notes are recorded but there is no sound.** Same list. MIDI doesn't need microphone
permission, so that combination almost always means step 1.

**My piano is plugged into an audio interface.** Choose the interface under *Audio in*.
pianotake records its first two inputs (or the first one with `--channels 1`).

**The recording sounds different from the piano's headphones.** First check the file
itself. Play it back into the instrument over USB and listen on the instrument's
headphones, so both go through the same speakers or headphones:

```bash
python -c "import sounddevice as sd, soundfile as sf; x, sr = sf.read('take.wav'); sd.play(x, sr, device='YOUR INSTRUMENT'); sd.wait()"
```

If it sounds right there, the difference comes from your computer's playback chain
(Bluetooth headphones, the player app, system effects), not the recording.

**Linux: no MIDI ports at all.** Load the ALSA MIDI sequencer with `sudo modprobe snd-seq`
(most desktop distributions load it automatically).

**Something else.** Open an issue and include the output of `pianotake --list`.

## Tested with

- Roland FP-30X on macOS (Apple Silicon): audio and MIDI over a single USB cable.

The Intel Mac, Windows and Linux apps are built and smoke-tested automatically, but
nobody has tried them with a real instrument yet. Reports with other instruments and
systems are very welcome.

## Development

```bash
pip install -e ".[test]"
pytest
```

The tests need no hardware. The Tk thread never calls PortAudio or rtmidi: on macOS,
a CoreMIDI call on the main thread can crash Tk, so all device work runs on worker threads.

## License

MIT. See [LICENSE](LICENSE).
