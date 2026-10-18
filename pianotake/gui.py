"""Tk window for pianotake: a big record button, level meter, MIDI activity and
device pickers. All recording work is in pianotake.recorder."""
import datetime
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog

from .recorder import (CHANNELS, NO_AUDIO, NO_MIDI, NO_SIGNAL_SECONDS, Take, db, diagnose, fmt_db,
                       list_audio_inputs, list_midi_inputs, mic_authorization, midi_output_for,
                       pick_devices,
                       probe_input, refresh_portaudio, resolve_audio_index, reveal, silence_help,
                       unique_base, with_retries)
from .settings import DEFAULT_OUT, load_settings, save_settings, tilde

REVEAL_LABEL = ("Reveal in Finder" if sys.platform == "darwin" else
                "Show in Explorer" if sys.platform.startswith("win") else "Open folder")


class App:
    """Tk GUI. Rule: the Tk main thread never calls PortAudio or rtmidi. On macOS a
    CoreMIDI call on the main thread can spin the Cocoa run loop while the GIL is
    released, Tk then runs a Python callback and the interpreter aborts. All device
    work runs on worker threads and reports back through self.q."""
    POLL_MS = 50
    BTN = 120

    def __init__(self, root, audio_hint=None, midi_hint=None, out_dir=None, channels=CHANNELS):
        self.root = root
        self.audio_hint, self.midi_hint, self.channels = audio_hint, midi_hint, channels
        self.settings = load_settings()
        self.out_dir = out_dir or self.settings.get("folder") or str(DEFAULT_OUT)
        root.title("pianotake")
        try:   # window / taskbar icon (the macOS app bundle has its own)
            self._icon = tk.PhotoImage(file=str(Path(__file__).with_name("icon.png")))
            root.iconphoto(True, self._icon)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
        root.minsize(560, 0)
        self.q = queue.Queue()
        self.post = self.q.put
        self.pa_lock = threading.Lock()   # one of {scan, probe, take} uses PortAudio at a time
        self.audio_devs, self.midi_names = [], []
        self.take = None
        self.state = "idle"               # idle | starting | recording | stopping
        self.scan_attempts = 0
        self.scan_after = None
        self.probed_for = None
        self.probing = False
        self.waiting_permission = False
        self.quit_after_stop = False
        self.last_result = None
        self.meter_db = self.hold_db = -120.0
        self.hold_until = 0.0
        self.midi_flash_until = 0.0
        self.no_signal_logged = False
        self.hover = False
        self._build()
        self._set_state("idle")
        self._scan()
        root.after(self.POLL_MS, self._tick)

    # ---------------------------------------------------------------- layout
    def _build(self):
        r = self.root
        bg = ttk.Style().lookup("TFrame", "background") or r.cget("bg")
        main = ttk.Frame(r, padding=14)
        main.grid(sticky="nsew")
        r.columnconfigure(0, weight=1)
        r.rowconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)

        self.btn = tk.Canvas(main, width=self.BTN, height=self.BTN, highlightthickness=0, bg=bg)
        self.btn.grid(row=0, column=0, rowspan=4, padx=(0, 16), sticky="n")
        self.btn.bind("<Button-1>", lambda e: self.toggle())
        self.btn.bind("<Enter>", lambda e: self._hover(True))
        self.btn.bind("<Leave>", lambda e: self._hover(False))
        self.btn.configure(cursor="hand2")

        self.elapsed = ttk.Label(main, text="00:00.0", font=("Helvetica", 34, "bold"))
        self.elapsed.grid(row=0, column=1, sticky="w")
        self.btn_hint = ttk.Label(main, text="Press Space or the red button to record")
        self.btn_hint.grid(row=1, column=1, sticky="w")

        self.meter = tk.Canvas(main, height=24, highlightthickness=0, bg="#1e1e1e")
        self.meter.grid(row=2, column=1, sticky="ew", pady=(8, 4))
        self.meter.bind("<Configure>", lambda e: self._draw_meter())

        mrow = ttk.Frame(main)
        mrow.grid(row=3, column=1, sticky="w")
        self.midi_dot = tk.Canvas(mrow, width=16, height=16, highlightthickness=0, bg=bg)
        self.midi_dot.grid(row=0, column=0)
        self.dot = self.midi_dot.create_oval(2, 2, 14, 14, fill="#555", outline="")
        self.notes_lbl = ttk.Label(mrow, text="MIDI  ·  notes: 0")
        self.notes_lbl.grid(row=0, column=1, padx=(6, 0))

        dev = ttk.LabelFrame(main, text="Devices", padding=8)
        dev.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        dev.columnconfigure(1, weight=1)
        ttk.Label(dev, text="Audio in").grid(row=0, column=0, sticky="w")
        self.audio_var = tk.StringVar()
        self.audio_cb = ttk.Combobox(dev, textvariable=self.audio_var, state="readonly")
        self.audio_cb.grid(row=0, column=1, sticky="ew", padx=6)
        self.audio_cb.bind("<<ComboboxSelected>>", lambda e: self._on_device_pick())
        ttk.Label(dev, text="MIDI in").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.midi_var = tk.StringVar()
        self.midi_cb = ttk.Combobox(dev, textvariable=self.midi_var, state="readonly")
        self.midi_cb.grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        self.midi_cb.bind("<<ComboboxSelected>>", lambda e: self._on_device_pick())
        self.refresh_btn = ttk.Button(dev, text="Refresh", command=self._manual_scan)
        self.refresh_btn.grid(row=0, column=2)
        self.test_btn = ttk.Button(dev, text="Test audio", command=lambda: self._probe(True))
        self.test_btn.grid(row=1, column=2, pady=(4, 0))

        out = ttk.LabelFrame(main, text="Output", padding=8)
        out.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        out.columnconfigure(1, weight=1)
        ttk.Label(out, text="Folder").grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar(value=tilde(self.out_dir))
        self.folder_entry = ttk.Entry(out, textvariable=self.folder_var)
        self.folder_entry.grid(row=0, column=1, sticky="ew", padx=6)
        self.folder_btn = ttk.Button(out, text="Choose…", command=self._choose_folder)
        self.folder_btn.grid(row=0, column=2)
        ttk.Label(out, text="Take name").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(out, textvariable=self.name_var)
        self.name_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        ttk.Label(out, text="blank = take_YYYYMMDD_HHMMSS", foreground="gray").grid(
            row=1, column=2, sticky="w", pady=(4, 0))
        for e in (self.folder_entry, self.name_entry):
            e.bind("<Escape>", lambda ev: self.root.focus_set())
            e.bind("<Return>", lambda ev: self.root.focus_set())

        self.status = ttk.Label(main, text="", wraplength=540, justify="left")
        self.status.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.warn = ttk.Label(main, text="", foreground="#c0392b", wraplength=540, justify="left")
        self.warn.grid(row=7, column=0, columnspan=2, sticky="w")

        res = ttk.Frame(main)
        res.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        res.columnconfigure(0, weight=1)
        self.result_lbl = ttk.Label(res, text="", justify="left", wraplength=400)
        self.result_lbl.grid(row=0, column=0, rowspan=2, sticky="w")
        self.reveal_btn = ttk.Button(res, text=REVEAL_LABEL, command=self._reveal)
        self.check_btn = ttk.Button(res, text="Check sync", command=self._check)

        logf = ttk.Frame(main)
        logf.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        main.rowconfigure(9, weight=1)
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log = tk.Text(logf, height=10, width=80, wrap="word", font="TkFixedFont",
                           relief="flat", borderwidth=1, takefocus=0)
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        for tag, color in (("err", "#c0392b"), ("warn", "#b9770e"), ("ok", "#1e8449"),
                           ("info", "")):
            self.log.tag_configure(tag, foreground=color or None)

        r.bind("<space>", self._on_space)
        main.bind("<Button-1>", lambda e: r.focus_set())
        r.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            r.createcommand("tk::mac::Quit", self._on_close)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- drawing
    def _hover(self, on):
        self.hover = on
        self._draw_button()

    def _draw_button(self):
        c, s = self.btn, self.BTN
        c.delete("all")
        enabled = self._can_toggle()
        if not enabled:
            fill, ring = "#a5a5a5", "#7d7d7d"
        elif self.state == "recording":
            fill, ring = "#e03131", "#8b1414"
        else:
            fill, ring = ("#f03e3e" if self.hover else "#d62828"), "#8b1414"
        if self.state == "recording" and int(time.monotonic() * 2) % 2 == 0:
            c.create_oval(1, 1, s - 1, s - 1, outline="#e03131", width=3)
        c.create_oval(8, 8, s - 8, s - 8, fill=fill, outline=ring, width=3)
        m = s / 2
        if self.state == "recording":
            c.create_rectangle(m - 19, m - 19, m + 19, m + 19, fill="white", outline="")
        else:
            label = {"starting": "…", "stopping": "SAVE"}.get(self.state, "REC")
            c.create_text(m, m, text=label, fill="white", font=("Helvetica", 22, "bold"))

    def _draw_meter(self, no_signal=False):
        c = self.meter
        c.delete("all")
        w = max(c.winfo_width(), 10)
        h = max(c.winfo_height(), 10)
        c.configure(bg="#7a1010" if no_signal else "#1e1e1e")

        def x_of(dbv):
            return w * min(1.0, max(0.0, (dbv + 60.0) / 60.0))
        x = x_of(self.meter_db)
        for lo, hi, col in ((-60, -12, "#2fb344"), (-12, -3, "#f2c94c"), (-3, 0, "#eb5757")):
            x0, x1 = x_of(lo), min(x, x_of(hi))
            if x1 > x0:
                c.create_rectangle(x0, 2, x1, h - 2, fill=col, outline="")
        for tick in (-48, -36, -24, -12, -6, -3):
            c.create_line(x_of(tick), h - 5, x_of(tick), h - 1, fill="#777")
        if self.hold_db > -60:
            hx = x_of(self.hold_db)
            c.create_line(hx, 2, hx, h - 2, fill="white", width=2)
        if no_signal:
            txt = "NO AUDIO SIGNAL"
        elif self.hold_db > -119:
            txt = f"{self.hold_db:.1f} dBFS"
        else:
            txt = "-∞ dBFS"
        c.create_text(w - 6, h / 2, text=txt, anchor="e", fill="white",
                      font=("Helvetica", 11, "bold"))

    # ---------------------------------------------------------------- messages
    def say(self, text, level="info"):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{ts}] {text}\n", level)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _tick(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self._live()
        self.root.after(self.POLL_MS, self._tick)

    def _handle(self, m):
        kind = m[0]
        if kind == "level":
            d = db(m[1])
            self.meter_db = max(self.meter_db, d)
            if d >= self.hold_db or time.monotonic() > self.hold_until:
                self.hold_db, self.hold_until = d, time.monotonic() + 1.5
        elif kind == "midi":
            self.notes_lbl.configure(text=f"MIDI  ·  notes: {m[1]}")
            self.midi_flash_until = time.monotonic() + 0.12
        elif kind == "log":
            self.say(m[2], m[1])
        elif kind == "devices":
            self._on_devices(*m[1:])
        elif kind == "probe":
            self._on_probe(*m[1:])
        elif kind == "permission":
            self._on_permission(*m[1:])
        elif kind == "started":
            self._set_state("recording")
            t = self.take
            self.say("Recording → " + " / ".join(p.name for p in (t.wav_path, t.mid_path) if p)
                     + ("" if t.audio else " (MIDI only)"), "ok")
            if self.quit_after_stop:
                self._set_state("stopping")
                t.stop()
        elif kind == "start_failed":
            self.take = None
            self._set_state("idle")
            if self.quit_after_stop:
                self.root.destroy()
                return
            self.say(f"Could not start recording: {m[1]}", "err")
            self.warn.configure(text="Could not start recording. See messages below; "
                                     "check the devices and press Refresh.")
        elif kind == "stopped":
            self._on_stopped(m[1])
        elif kind == "check":
            self.say(m[1], "ok" if m[2] else "warn")

    def _live(self):
        now = time.monotonic()
        # meter falls ~30 dB/s; hold marker decays after 1.5 s
        self.meter_db = max(-120.0, self.meter_db - 1.5)
        if now > self.hold_until:
            self.hold_db = max(-120.0, self.hold_db - 1.0)
        no_signal = False
        t = self.take
        if self.state == "recording" and t:
            secs = t.elapsed()
            mm, ss = divmod(secs, 60)
            self.elapsed.configure(text=f"{int(mm):02d}:{ss:04.1f}")
            if t.audio and secs >= NO_SIGNAL_SECONDS and t.max_peak == 0.0:
                no_signal = True
                if not self.no_signal_logged:
                    self.no_signal_logged = True
                    self.warn.configure(text=f"NO AUDIO SIGNAL: {NO_SIGNAL_SECONDS:.0f} s of "
                                             "exact digital silence. See messages below.")
                    self.say(silence_help("the recording"), "err")
            elif self.no_signal_logged and t.max_peak > 0:
                self.no_signal_logged = False
                self.warn.configure(text="")
            if t.overflows:
                self.btn_hint.configure(
                    text=f"Recording · Space or button to stop · input overflows: {t.overflows}")
        self._draw_meter(no_signal)
        self.midi_dot.itemconfigure(self.dot, fill="#2fb344" if now < self.midi_flash_until else "#555")
        if self.state == "recording":
            self._draw_button()  # blink ring

    # ---------------------------------------------------------------- devices
    def _manual_scan(self):
        self.scan_attempts = 0
        self.probed_for = None
        self._scan()

    def _scan(self):
        if self.scan_after:
            self.root.after_cancel(self.scan_after)
            self.scan_after = None
        if self.state != "idle":
            return

        def work():
            if not self.pa_lock.acquire(blocking=False):
                self.post(("devices", None, None, "busy"))
                return
            try:
                refresh_portaudio()
                audio = list_audio_inputs(self.channels)
                err = None
            except Exception as e:  # noqa: BLE001
                audio, err = [], str(e)
            finally:
                self.pa_lock.release()
            try:
                midi = list_midi_inputs()
            except Exception as e:  # noqa: BLE001
                midi, err = [], (err or "") + f" MIDI: {e}"
            self.post(("devices", audio, midi, err))
        threading.Thread(target=work, daemon=True).start()

    def _audio_label(self, d):
        return f"{d['name']}  ({d['channels']} ch, {d['samplerate']} Hz)"

    def _on_devices(self, audio, midi, err):
        if err == "busy":
            self.scan_after = self.root.after(1000, self._scan)
            return
        if err:
            self.say(f"Device scan problem: {err}", "warn")
        self.audio_devs, self.midi_names = audio, midi
        labels = [self._audio_label(d) for d in audio]
        self.audio_cb.configure(values=labels + [NO_AUDIO])
        self.midi_cb.configure(values=midi + [NO_MIDI])

        names = [d["name"] for d in audio]
        cur_a = self._selected_audio()
        want_a = NO_AUDIO if self._midi_only() else (cur_a["name"] if cur_a and cur_a["name"] in names else None)
        want_m = self.midi_var.get() if self.midi_var.get() in midi + [NO_MIDI] else None
        if want_a is None or want_m is None:
            # explicit --audio/--midi > the choice remembered from last time > automatic pairing
            hint_a = None if (self.audio_hint or "").lower() == "none" else self.audio_hint
            hint_m = None if (self.midi_hint or "").lower() == "none" else self.midi_hint
            auto_a, auto_m = pick_devices(names, midi, hint_a, hint_m)
            saved_a, saved_m = self.settings.get("audio"), self.settings.get("midi")
            if want_a is None:
                want_a = auto_a if self.audio_hint or saved_a not in names + [NO_AUDIO] else saved_a
                if (self.audio_hint or "").lower() == "none":
                    want_a = NO_AUDIO
            if want_m is None:
                want_m = auto_m if self.midi_hint or saved_m not in midi + [NO_MIDI] else saved_m
            if self.midi_hint and self.midi_hint.lower() == "none":
                want_m = NO_MIDI
        self.audio_var.set(NO_AUDIO if want_a == NO_AUDIO else
                           next((self._audio_label(d) for d in audio if d["name"] == want_a), ""))
        self.midi_var.set(want_m or "")

        have_audio = self._selected_audio() is not None or self._midi_only()
        have_midi = self.midi_var.get() != ""
        self._update_status()
        if not (have_audio and have_midi) and self.scan_attempts < 60:
            self.scan_attempts += 1
            missing = " and ".join(x for x, ok in (("audio", have_audio), ("MIDI", have_midi)) if not ok)
            self.warn.configure(text=f"Waiting for your instrument… no {missing} input recognised "
                                     f"yet (retry {self.scan_attempts}/60 every 2 s). Connect it, or "
                                     "pick the devices below (choose MIDI \"none\" to record audio only).")
            self.scan_after = self.root.after(2000, self._scan)
        elif not (have_audio and have_midi):
            self.warn.configure(text="Instrument not found. Connect it and press Refresh, "
                                     "or choose the devices manually.")
        else:
            if self.warn.cget("text").startswith(("Waiting for your instrument", "Instrument not found")):
                self.warn.configure(text="")
            if self.scan_attempts or self.probed_for is None:
                self.say(f"Found audio '{self._selected_audio()['name']}', MIDI '{self.midi_var.get()}'.", "ok")
            self.scan_attempts = 0
        a = self._selected_audio()
        if a and self.probed_for != a["name"]:
            self._probe(False)
        self._set_state(self.state)

    def _selected_audio(self):
        lbl = self.audio_var.get()
        return next((d for d in self.audio_devs if self._audio_label(d) == lbl), None)

    def _midi_only(self):
        return self.audio_var.get() == NO_AUDIO

    def _selected_midi(self):
        v = self.midi_var.get()
        return None if v in ("", NO_MIDI) else v

    def _on_device_pick(self):
        self.root.focus_set()
        self._remember()
        self._update_status()
        a = self._selected_audio()
        if a and self.probed_for != a["name"]:
            self._probe(False)
        elif self._midi_only():
            self.probed_for = None
            if self.warn.cget("text").startswith(("⚠ Pre-flight", "Click Allow")):
                self.warn.configure(text="")
            if not self._selected_midi():
                self.warn.configure(text="Choose a MIDI input to record MIDI only.")
        self._set_state(self.state)

    def _update_status(self):
        a = self._selected_audio()
        parts = [f"Audio: {a['name']} · {a['channels']} ch · {a['samplerate']} Hz" if a else
                 "Audio: none (MIDI only)" if self._midi_only() else "Audio: (not chosen)",
                 f"MIDI: {self._selected_midi() or ('none (audio only)' if self.midi_var.get() else '(not chosen)')}"]
        self.status.configure(text="    ".join(parts))

    # ---------------------------------------------------------------- pre-flight
    def _probe(self, play, retry=0):
        a = self._selected_audio()
        if not a or self.state != "idle" or self.probing:
            return
        self.probing = True
        self.probed_for = a["name"]
        self._set_state(self.state)
        midi_in = self._selected_midi()
        self.say(f"Testing audio input '{a['name']}'"
                 + (" (playing a short chord through MIDI; play a few notes if it stays silent)…"
                    if play else " (listening 1 s)…"))

        def work():
            midi_out = None
            if play:
                try:
                    midi_out = midi_output_for(midi_in)
                except Exception:  # noqa: BLE001
                    midi_out = None
            if not self.pa_lock.acquire(timeout=3):
                self.post(("probe", a, None, "audio device busy", play, retry))
                return
            try:
                res = with_retries(lambda: probe_input(
                    resolve_audio_index(a["name"], a["channels"]), a["channels"], a["samplerate"],
                    1.5 if play else 1.0, midi_out), "Opening audio for test", attempts=3,
                    log=lambda s: self.post(("log", "warn", s)))
                self.post(("probe", a, res, None, play, retry))
            except Exception as e:  # noqa: BLE001
                self.post(("probe", a, None, str(e), play, retry))
            finally:
                self.pa_lock.release()
        threading.Thread(target=work, daemon=True).start()

    def _on_probe(self, dev, res, err, play=False, retry=0):
        self.probing = False
        self._set_state(self.state)
        if err:
            self.say(f"Audio test failed: {err}", "err")
            self.warn.configure(text="Audio test failed: could not open the input. See messages.")
            return
        if res["all_zero"] and res.get("mic") == "not_determined":
            # Opening the input just made macOS ask; until the user answers it delivers silence.
            self.say("macOS is asking whether pianotake may use audio input. Click Allow; "
                     "the test runs again by itself.", "warn")
            self.warn.configure(text="Click Allow in the macOS dialog to let pianotake hear your instrument.")
            self._wait_for_permission(dev)
            return
        if res["all_zero"] and not play and retry == 0:
            # An instrument that was just switched on can send pure zeros for a moment.
            self.say("No signal yet; testing again in 2 s…")
            self.root.after(2000, lambda: self._probe(False, retry=1))
            return
        level, text = diagnose(res, dev["name"])
        self.say(text, level)
        self._handle(("level", res["peak"]))
        if level == "err":
            self.warn.configure(text="⚠ Pre-flight: no audio from the input (digital silence). "
                                     "See messages below for causes and fixes.")
        elif level == "warn":
            self.warn.configure(text="⚠ Pre-flight: test chord not heard. See messages below.")
        elif self.warn.cget("text").startswith("⚠ Pre-flight"):
            self.warn.configure(text="")

    def _wait_for_permission(self, dev):
        """Poll the microphone permission off the Tk thread until the user answers."""
        if self.waiting_permission:
            return
        self.waiting_permission = True

        def work():
            for _ in range(600):                      # up to 5 minutes
                status = mic_authorization()
                if status != "not_determined":
                    self.post(("permission", dev, status))
                    return
                time.sleep(0.5)
            self.post(("permission", dev, None))
        threading.Thread(target=work, daemon=True).start()

    def _on_permission(self, dev, status):
        self.waiting_permission = False
        if status == "authorized":
            self.say("Audio input allowed. Testing again…", "ok")
            self.warn.configure(text="")
            self.probed_for = None
            self._probe(False)
        elif status in ("denied", "restricted"):
            self.say(silence_help("the input", mic=status), "err")
            self.warn.configure(text="⚠ Audio input was not allowed. See messages below to turn it on.")

    # ---------------------------------------------------------------- record
    def _can_toggle(self):
        if self.state == "recording":
            return True
        if self.state != "idle" or self.probing:
            return False
        if self._midi_only():
            return self._selected_midi() is not None
        return self._selected_audio() is not None

    def _set_state(self, s):
        self.state = s
        idle = s == "idle"
        for w in (self.audio_cb, self.midi_cb):
            w.configure(state="readonly" if idle else "disabled")
        for w in (self.refresh_btn, self.test_btn, self.folder_btn):
            w.configure(state="normal" if idle and not self.probing else "disabled")
        for w in (self.folder_entry, self.name_entry):
            w.configure(state="normal" if idle else "disabled")
        self.btn_hint.configure(text={
            "idle": "Press Space or the red button to record",
            "starting": "Starting…",
            "recording": "Recording · Space or button to stop",
            "stopping": "Saving…"}[s])
        self._draw_button()

    def _on_space(self, event):
        w = self.root.focus_get()
        if isinstance(w, (ttk.Entry, tk.Entry)) and str(w.cget("state")) == "normal":
            return None  # typing a take name / folder
        self.toggle()
        return "break"

    def toggle(self):
        self.root.focus_set()
        if self.state == "recording":
            self._set_state("stopping")
            self.take.stop()
        elif self._can_toggle():
            self._start()

    def _start(self):
        a = self._selected_audio()
        folder = Path(os.path.expanduser(self.folder_var.get().strip() or str(DEFAULT_OUT)))
        name = self.name_var.get().strip() or datetime.datetime.now().strftime("take_%Y%m%d_%H%M%S")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            self.say(f"Cannot create output folder {folder}: {e}", "err")
            return
        base = unique_base(folder, name)
        self._remember()
        if self._midi_only():
            self.take = Take(None, 0, 0, self._selected_midi(), base, self.post)
        else:
            self.take = Take(a["name"], a["channels"], a["samplerate"], self._selected_midi(), base, self.post)
        self.no_signal_logged = False
        self.warn.configure(text="")
        self.notes_lbl.configure(text="MIDI  ·  notes: 0")
        self.elapsed.configure(text="00:00.0")
        self.result_lbl.configure(text="")
        self.reveal_btn.grid_remove()
        self.check_btn.grid_remove()
        self._set_state("starting")
        take = self.take

        def work():
            if not self.pa_lock.acquire(timeout=5):
                self.post(("start_failed", "audio device busy (a test is still running)"))
                return
            try:
                take.start()
            except Exception as e:  # noqa: BLE001
                self.pa_lock.release()
                self.post(("start_failed", str(e)))
                return
            self.post(("started",))
            take.finished.wait()
            self.pa_lock.release()
        threading.Thread(target=work, daemon=True).start()

    def _on_stopped(self, r):
        self.last_result = r
        self.take = None
        self._set_state("idle")
        mm, ss = divmod(r.duration, 60)
        stats = f"{int(mm)}:{ss:04.1f} · {r.note_ons} notes · {r.midi_events} MIDI events"
        if r.wav_path:
            stats += f" · peak {fmt_db(r.max_peak)} · input overflows: {r.overflows}"
        files = [p for p in (r.wav_path, r.mid_path) if p]
        self.result_lbl.configure(text="Saved:\n" + "\n".join(tilde(p) for p in files) + f"\n{stats}")
        self.reveal_btn.grid(row=0, column=1, padx=(8, 0), sticky="e")
        if r.wav_path:   # the sync check compares the WAV with the MIDI
            self.check_btn.grid(row=1, column=1, padx=(8, 0), sticky="e")
        self.say(f"Saved {' and '.join(p.name for p in files)}: {stats}", "ok")
        if r.stop_reason:
            self.warn.configure(text=f"Take stopped early: {r.stop_reason}. "
                                     "Everything captured so far was saved.")
        elif r.wav_path and r.max_peak == 0.0 and r.frames:
            self.warn.configure(text="⚠ The saved WAV is digital silence. See messages for fixes.")
        elif r.overflows:
            self.warn.configure(text=f"⚠ {r.overflows} input overflow(s): some audio blocks were "
                                     "dropped, which shifts later audio against the MIDI.")
        for p in r.problems:
            self.say(p, "warn")
        if r.stop_reason:
            self.probed_for = None
            self._manual_scan()
        if self.quit_after_stop:
            self.root.destroy()

    def _remember(self):
        self.settings.update(audio=NO_AUDIO if self._midi_only() else (self._selected_audio() or {}).get("name"),
                             midi=self.midi_var.get() or None,
                             folder=self.folder_var.get().strip() or None)
        save_settings(self.settings)

    def _reveal(self):
        r = self.last_result
        if r:
            reveal(r.wav_path or r.mid_path)

    def _check(self):
        r = self.last_result
        if not r:
            return

        def work():
            try:
                from . import check
                text, ok = check.report(r.wav_path, r.mid_path)
            except Exception as e:  # noqa: BLE001
                text, ok = f"Check failed: {e}", False
            self.post(("check", text, ok))
        threading.Thread(target=work, daemon=True).start()

    def _choose_folder(self):
        d = filedialog.askdirectory(initialdir=os.path.expanduser(self.folder_var.get() or "~"),
                                    title="Choose output folder")
        if d:
            self.folder_var.set(tilde(d))
            self._remember()

    def _on_close(self):
        if self.state in ("recording", "stopping", "starting"):
            self.quit_after_stop = True
            if self.state == "recording":
                self._set_state("stopping")
                self.take.stop()
            self.say("Saving the take before quitting…", "warn")
            return
        self.root.destroy()


def run(audio_hint=None, midi_hint=None, out_dir=None, channels=CHANNELS):
    root = tk.Tk()
    App(root, audio_hint, midi_hint, out_dir, channels)
    root.mainloop()
