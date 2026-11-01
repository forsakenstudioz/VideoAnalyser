"""
Lecture Digest - simple window for people who do not want to use a terminal.

Runs lecture_digest.py (next to this file) in the background and shows its
progress. Started by LectureDigest.exe, or directly with:  python gui.py
"""
from __future__ import annotations

import json
import os
import queue
import time
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
TOOL = HERE / "lecture_digest.py"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

LANGUAGES = {
    "Auto-detect": "auto", "English": "en", "Hindi": "hi", "Tamil": "ta", "Telugu": "te",
    "Malayalam": "ml", "Kannada": "kn", "Bengali": "bn", "Marathi": "mr", "Gujarati": "gu",
    "Punjabi": "pa", "Urdu": "ur", "Arabic": "ar", "Spanish": "es", "French": "fr",
    "German": "de", "Chinese": "zh", "Japanese": "ja",
}
QUALITY = {
    "Fast and accurate (recommended)": "large-v3-turbo",
    "Most accurate (about 2x slower)": "large-v3",
    "Light (small GPU or no GPU)": "small",
}
VIDEO_TYPES = [("Videos", "*.mp4 *.mkv *.avi *.mov *.webm *.flv *.wmv *.m4v *.mpg *.mpeg *.ts *.3gp"),
               ("All files", "*.*")]


def detect_gpu() -> str:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
        return out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


SETTINGS_FILE = Path.home() / ".lecture_digest_gui.json"
WHEN_DONE = ["Do nothing", "Put the PC to sleep", "Shut down the PC"]

# (light, dark) colours for the app's own label styles
THEMES = {
    "light": {"bg": None, "fg": None, "field": None, "hint": "#666666", "muted": "#555555",
              "good": "#1a7f37", "warn": "#b35900"},
    "dark": {"bg": "#1f1f1f", "fg": "#e6e6e6", "field": "#2b2b2b", "hint": "#a0a0a0", "muted": "#b0b0b0",
             "good": "#4ac26b", "warn": "#f0a050", "button": "#333333", "active": "#3d3d3d",
             "border": "#454545", "accent": "#3b82f6"},
}


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(settings), encoding="utf-8")
    except OSError:
        pass


def system_prefers_dark() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, m = sec // 3600, sec % 3600 // 60
    if h >= 24:
        return f"{h // 24} d {h % 24} h"
    return f"{h} h {m:02d} min" if h else f"{m} min"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Lecture Digest")
        self.geometry("820x840")
        self.minsize(700, 560)
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str | None] = queue.Queue()

        self.settings = st = load_settings()
        pick = lambda key, choices, default: st.get(key) if st.get(key) in choices else default  # noqa: E731
        self.input_var = tk.StringVar(value=st.get("input", ""))
        self.output_var = tk.StringVar(value=st.get("output") or str(Path.home() / "Documents" / "LectureDigests"))
        self.lang_var = tk.StringVar(value=pick("language", LANGUAGES, "Auto-detect"))
        self.quality_var = tk.StringVar(value=pick("quality", QUALITY, next(iter(QUALITY))))
        self.ocr_var = tk.BooleanVar(value=st.get("ocr", True))
        self.translate_var = tk.BooleanVar(value=st.get("translate", False))
        self.combined_var = tk.BooleanVar(value=st.get("combined", True))
        self.prompt_var = tk.StringVar(value=st.get("prompt", ""))
        self.awake_var = tk.BooleanVar(value=st.get("keep_awake", True))
        self.after_var = tk.StringVar(value=pick("when_done", WHEN_DONE, WHEN_DONE[0]))
        self.sound_var = tk.BooleanVar(value=st.get("sound", True))
        self.dark_var = tk.BooleanVar(value=st.get("dark", system_prefers_dark()))
        self.status_var = tk.StringVar(value="Choose a video or a folder of videos, then press Start.")

        self._build()
        self._apply_theme()
        self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI --
    def _build(self) -> None:
        pad = {"padx": 10, "pady": 4}
        self.style = ttk.Style(self)
        self.light_theme = "vista" if "vista" in self.style.theme_names() else self.style.theme_use()

        gpu = detect_gpu()
        gpu_text = (f"GPU found: {gpu}" if gpu else
                    "No NVIDIA GPU found - it will still work, but slowly. Install/update the NVIDIA driver to use your GPU.")
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Checkbutton(top, text="Dark mode", variable=self.dark_var,
                        command=self._toggle_dark).pack(side="right")
        ttk.Label(top, text=gpu_text, style="Good.TLabel" if gpu else "Warn.TLabel",
                  wraplength=640).pack(side="left", anchor="w")
        if not gpu:
            self.quality_var.set("Light (small GPU or no GPU)")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=6)
        main = ttk.Frame(nb, padding=8)
        extra = ttk.Frame(nb, padding=8)
        nb.add(main, text="  Make digests  ")
        nb.add(extra, text="  Extra frames  ")
        self._build_main(main)
        self._build_extra(extra)

    def _toggle_dark(self) -> None:
        self.settings["dark"] = self.dark_var.get()
        save_settings(self.settings)
        self._apply_theme()

    def _apply_theme(self) -> None:
        dark = self.dark_var.get()
        c = THEMES["dark" if dark else "light"]
        st = self.style
        st.theme_use("clam" if dark else self.light_theme)
        if dark:
            st.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                         bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"],
                         troughcolor=c["field"], selectbackground=c["accent"], selectforeground="#ffffff",
                         insertcolor=c["fg"])
            st.map(".", foreground=[("disabled", "#777777")])
            st.configure("TButton", background=c["button"])
            st.map("TButton", background=[("active", c["active"]), ("disabled", c["bg"])])
            st.configure("TNotebook", background=c["bg"])
            st.configure("TNotebook.Tab", background=c["button"], padding=(10, 4))
            st.map("TNotebook.Tab", background=[("selected", c["field"])])
            st.map("TCombobox", fieldbackground=[("readonly", c["field"])],
                   foreground=[("readonly", c["fg"])], background=[("readonly", c["button"])])
            st.map("TCheckbutton", background=[("active", c["bg"])],
                   indicatorcolor=[("selected", c["accent"]), ("!selected", c["field"])])
            st.configure("Horizontal.TProgressbar", background=c["accent"])
            st.configure("Vertical.TScrollbar", background=c["button"], troughcolor=c["field"],
                         arrowcolor=c["fg"])
            st.map("Vertical.TScrollbar", background=[("active", c["active"])])
        bg = c["bg"] or st.lookup("TFrame", "background") or "SystemButtonFace"
        for name, key in (("Hint", "hint"), ("Muted", "muted"), ("Good", "good"), ("Warn", "warn")):
            st.configure(f"{name}.TLabel", foreground=c[key])
        self.configure(background=bg)
        self.option_add("*TCombobox*Listbox.background", c["field"] or "white")
        self.option_add("*TCombobox*Listbox.foreground", c["fg"] or "black")
        self.log.configure(background=c["field"] or "white", foreground=c["fg"] or "black",
                           insertbackground=c["fg"] or "black")
        self._dark_title_bar(dark)

    def _dark_title_bar(self, dark: bool) -> None:
        if os.name != "nt":
            return
        try:  # Windows 10 1809+ / 11: DWMWA_USE_IMMERSIVE_DARK_MODE
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            value = ctypes.c_int(1 if dark else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
            self.withdraw()  # redraw so the title bar picks up the change
            self.deiconify()
        except (OSError, AttributeError):
            pass

    def _row(self, parent, label, var, buttons):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, width=16).pack(side="left")
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True, padx=(0, 6))
        for text, cmd in buttons:
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=2)

    def _build_main(self, f: ttk.Frame) -> None:
        self._row(f, "Videos:", self.input_var,
                  [("Choose video...", self._pick_video), ("Choose folder...", self._pick_folder)])
        self._row(f, "Save digests to:", self.output_var, [("Browse...", self._pick_output)])

        opts = ttk.LabelFrame(f, text="Options", padding=8)
        opts.pack(fill="x", pady=8)
        r = ttk.Frame(opts)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text="Language spoken:", width=16).pack(side="left")
        ttk.Combobox(r, textvariable=self.lang_var, values=list(LANGUAGES), state="readonly",
                     width=18).pack(side="left")
        ttk.Label(r, text="   Quality:").pack(side="left")
        ttk.Combobox(r, textvariable=self.quality_var, values=list(QUALITY), state="readonly",
                     width=32).pack(side="left", padx=4)
        r = ttk.Frame(opts)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text="Subject words:", width=16).pack(side="left")
        ttk.Entry(r, textvariable=self.prompt_var).pack(side="left", fill="x", expand=True)
        ttk.Label(opts, text="(optional) technical words used in the lectures, so they are spelled right, "
                             "e.g. enthalpy, Kirchhoff, eigenvalue", style="Hint.TLabel").pack(anchor="w")
        ttk.Checkbutton(opts, text="Read the text on slides and the board (OCR)",
                        variable=self.ocr_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(opts, text="Translate speech to English",
                        variable=self.translate_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(opts, text="Also make one combined file of all lectures (ALL_LECTURES.md) for uploading to Claude",
                        variable=self.combined_var).pack(anchor="w", pady=1)

        run = ttk.LabelFrame(f, text="While running and when finished", padding=8)
        run.pack(fill="x", pady=(0, 6))
        r = ttk.Frame(run)
        r.pack(fill="x")
        ttk.Checkbutton(r, text="Keep the PC awake", variable=self.awake_var).pack(side="left")
        ttk.Checkbutton(r, text="Play a sound when done", variable=self.sound_var).pack(side="left", padx=16)
        ttk.Label(r, text="When done:").pack(side="left", padx=(8, 4))
        ttk.Combobox(r, textvariable=self.after_var, values=WHEN_DONE, state="readonly",
                     width=20).pack(side="left")

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=4)
        self.start_btn = ttk.Button(btns, text="Start", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="Open digests folder",
                   command=lambda: open_folder(Path(self.output_var.get()))).pack(side="right")

        self.progress = ttk.Progressbar(f, mode="determinate")
        self.progress.pack(fill="x", pady=4)
        ttk.Label(f, textvariable=self.status_var, wraplength=760).pack(anchor="w")

        stats = ttk.LabelFrame(f, text="Progress", padding=(8, 4))
        stats.pack(fill="x", pady=4)
        self.stat_vars: dict[str, tk.StringVar] = {}
        cells = [("total", "Total videos"), ("completed", "Completed"), ("failed", "Failed"),
                 ("remaining", "Remaining"), ("video", "Video done"), ("elapsed", "Time spent"),
                 ("eta", "Time left"), ("finish", "Finishes around")]
        for i, (key, label) in enumerate(cells):
            col = (i % 4) * 2
            ttk.Label(stats, text=label + ":", style="Muted.TLabel").grid(row=i // 4, column=col, sticky="w", padx=(0, 4))
            var = self.stat_vars[key] = tk.StringVar(value="-")
            ttk.Label(stats, textvariable=var, font=("Segoe UI", 10, "bold")).grid(
                row=i // 4, column=col + 1, sticky="w", padx=(0, 18))

        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, pady=4)
        self.log = tk.Text(box, height=10, wrap="word", font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(box, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        ttk.Label(f, style="Hint.TLabel", wraplength=760,
                  text="You can stop at any time. Press Start again later and it continues where it stopped. "
                       "When it is done, give Claude the digests folder (or upload ALL_LECTURES.md) and ask your questions.").pack(anchor="w")

    def _build_extra(self, f: ttk.Frame) -> None:
        ttk.Label(f, wraplength=740, text=(
            "Save more pictures from part of a lecture, for example when the teacher writes a long "
            "derivation. Pick the lecture's folder inside your digests folder (or the video itself), "
            "then the start and end time.")).pack(anchor="w", pady=(0, 8))
        self.extra_lecture = tk.StringVar()
        self.extra_start = tk.StringVar(value="00:10:00")
        self.extra_end = tk.StringVar(value="00:11:00")
        self.extra_every = tk.StringVar(value="5")
        self._row(f, "Lecture:", self.extra_lecture,
                  [("Lecture folder...", self._pick_extra_folder), ("Video...", self._pick_extra_video)])
        r = ttk.Frame(f)
        r.pack(fill="x", pady=6)
        for label, var, w in (("From (h:mm:ss):", self.extra_start, 10), ("   To:", self.extra_end, 10),
                              ("   One picture every (seconds):", self.extra_every, 5)):
            ttk.Label(r, text=label).pack(side="left")
            ttk.Entry(r, textvariable=var, width=w).pack(side="left", padx=4)
        ttk.Button(f, text="Save pictures", command=self._extra_frames).pack(anchor="w", pady=6)
        ttk.Label(f, style="Hint.TLabel", text="Pictures go into the lecture's 'extra_frames' folder, "
                                             "which opens when they are ready.").pack(anchor="w")

    # ------------------------------------------------------------- pickers --
    def _pick_video(self):
        p = filedialog.askopenfilename(title="Choose a lecture video", filetypes=VIDEO_TYPES)
        if p:
            self.input_var.set(p)

    def _pick_folder(self):
        p = filedialog.askdirectory(title="Choose the folder with your lecture videos")
        if p:
            self.input_var.set(p)

    def _pick_output(self):
        p = filedialog.askdirectory(title="Where should the digests be saved?")
        if p:
            self.output_var.set(p)

    def _pick_extra_folder(self):
        p = filedialog.askdirectory(title="Choose the lecture's folder", initialdir=self.output_var.get())
        if p:
            self.extra_lecture.set(p)

    def _pick_extra_video(self):
        p = filedialog.askopenfilename(title="Choose the lecture video", filetypes=VIDEO_TYPES)
        if p:
            self.extra_lecture.set(p)

    # ------------------------------------------------------------- running --
    def _write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _run(self, args: list[str], on_done=None) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(TOOL), *args], cwd=str(HERE), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)

        def reader(proc=self.proc):
            for line in proc.stdout:
                self.lines.put(line.rstrip())
            proc.wait()
            self.lines.put(None)

        self._on_done = on_done
        threading.Thread(target=reader, daemon=True).start()

    def _start(self) -> None:
        src = self.input_var.get().strip()
        if not src or not Path(src).exists():
            messagebox.showwarning("Lecture Digest", "Please choose a video or a folder of videos first.")
            return
        args = ["process", str(Path(src).resolve()), "-o", str(Path(self.output_var.get().strip()).resolve()),
                "--model", QUALITY[self.quality_var.get()],
                "--language", LANGUAGES[self.lang_var.get()]]
        if self.ocr_var.get():
            args.append("--ocr")
        if self.translate_var.get():
            args += ["--task", "translate"]
        if self.combined_var.get():
            args.append("--combined")
        if self.prompt_var.get().strip():
            args += ["--prompt", self.prompt_var.get().strip()]
        if not detect_gpu():
            args += ["--device", "cpu"]
        self._save()
        self._keep_awake(self.awake_var.get())
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(value=0, maximum=1)
        for var in self.stat_vars.values():
            var.set("-")
        self.status_var.set("Starting... (the first time it downloads the speech model, about 1.6 GB)")
        self._run(args, on_done=self._finished)

    def _finished(self, code: int) -> None:
        self._keep_awake(False)
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if code == 0:
            self.progress.configure(value=self.progress["maximum"])
            if self.sound_var.get():
                self._ding()
            msg = self._last_line
            if re.search(r"[1-9]\d* failure", msg):
                msg += "\n\nSome videos failed. The reasons are in errors.log in the digests folder."
            if self.after_var.get() != WHEN_DONE[0]:
                self._power_action(self.after_var.get())
            elif messagebox.askyesno("Lecture Digest", f"{msg}\n\nOpen the digests folder now?"):
                open_folder(Path(self.output_var.get()))
        elif self._last_line.startswith("ERROR"):
            self.status_var.set(self._last_line)
            messagebox.showerror("Lecture Digest", self._last_line)
        else:
            self.status_var.set("Stopped. Press Start to continue where it left off.")

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":  # also stop ffmpeg started by the tool
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                               capture_output=True, creationflags=NO_WINDOW)
            else:
                self.proc.terminate()
            self._write("Stopping...")

    def _extra_frames(self) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("Lecture Digest", "Please wait until the current job has finished.")
            return
        lecture = self.extra_lecture.get().strip()
        if not lecture:
            messagebox.showwarning("Lecture Digest", "Choose the lecture first.")
            return
        args = ["frames", str(Path(lecture).resolve()), "--start", self.extra_start.get(), "--end", self.extra_end.get(),
                "--every", self.extra_every.get() or "5", "-o", str(Path(self.output_var.get().strip()).resolve())]
        self.start_btn.configure(state="disabled")

        def done(code):
            self.start_btn.configure(state="normal")
            m = re.search(r"Saved \d+ frame\(s\) to (.+)$", self._last_line)
            if code == 0 and m:
                open_folder(Path(m.group(1)))
            else:
                messagebox.showwarning("Lecture Digest", self._last_line or "Could not save pictures.")

        self._run(args, on_done=done)

    _last_line = ""

    def _pump(self) -> None:
        try:
            while True:
                line = self.lines.get_nowait()
                if line is None:
                    code = self.proc.returncode if self.proc else 0
                    if self._on_done:
                        self._on_done(code)
                    continue
                if line.startswith("PROGRESS ") and " | " in line:
                    self._update_stats(line.split(" | ", 1)[1])
                    continue
                self._last_line = re.sub(r"^\[\d\d:\d\d:\d\d\] ", "", line)
                self._write(line)
                self._update_status(self._last_line)
        except queue.Empty:
            pass
        self.after(150, self._pump)

    def _update_status(self, text: str) -> None:
        if m := re.match(r"Found (\d+) video", text):
            self.progress.configure(maximum=max(1, int(m.group(1))), value=0)
            self.status_var.set(text)
        elif text.startswith("=== "):
            self.status_var.set("Working on: " + Path(text[4:]).name)
        elif text.strip().startswith(("transcribed", "Loading Whisper", "language:")) or "keyframes" in text:
            self.status_var.set(self.status_var.get().split("  -  ")[0] + "  -  " + text.strip())
        elif text.startswith(("Done.", "FAILED", "ERROR")) or "FAILED" in text:
            self.status_var.set(text.strip())

    def _update_stats(self, payload: str) -> None:
        try:
            p = json.loads(payload)
        except ValueError:
            return
        done_sec = p["total_sec"] - p["left_sec"]
        v = self.stat_vars
        v["total"].set(str(p["total"]))
        v["completed"].set(str(p["completed"]) + (f" ({p['skipped']} done earlier)" if p["skipped"] else ""))
        v["failed"].set(str(p["failed"]))
        v["remaining"].set(str(p["remaining"]))
        v["video"].set(f"{fmt_duration(done_sec)} of {fmt_duration(p['total_sec'])}")
        v["elapsed"].set(fmt_duration(p["elapsed_sec"]))
        if p["eta_sec"] is None:
            v["eta"].set("estimating..." if p["remaining"] else "0 min")
            v["finish"].set("-")
        else:
            v["eta"].set(fmt_duration(p["eta_sec"]))
            v["finish"].set(time.strftime("%a %d %b, %I:%M %p", time.localtime(time.time() + p["eta_sec"])))
        self.progress.configure(maximum=max(1, p["total_sec"]), value=done_sec)

    def _on_close(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Lecture Digest", "A job is running. Stop it and close? "
                                                         "(You can continue later.)"):
                return
            self._stop()
        self._keep_awake(False)
        self._save()
        self.destroy()

    # ------------------------------------------------- settings and power --
    def _save(self) -> None:
        self.settings.update({
            "input": self.input_var.get(), "output": self.output_var.get(),
            "language": self.lang_var.get(), "quality": self.quality_var.get(),
            "ocr": self.ocr_var.get(), "translate": self.translate_var.get(),
            "combined": self.combined_var.get(), "prompt": self.prompt_var.get(),
            "keep_awake": self.awake_var.get(), "when_done": self.after_var.get(),
            "sound": self.sound_var.get(), "dark": self.dark_var.get()})
        save_settings(self.settings)

    @staticmethod
    def _keep_awake(on: bool) -> None:
        """Stop Windows going to sleep mid-run (the screen may still turn off)."""
        if os.name != "nt":
            return
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))

    def _ding(self) -> None:
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except (ImportError, RuntimeError):
            self.bell()

    def _power_action(self, action: str) -> None:
        """Sleep or shut down after a 60 s countdown the user can cancel."""
        if os.name != "nt":
            return
        if action == WHEN_DONE[1]:
            cmd = ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
        else:
            cmd = ["shutdown", "/s", "/t", "0"]
        job = self.after(60_000, lambda: subprocess.Popen(cmd, creationflags=NO_WINDOW))
        verb = "go to sleep" if action == WHEN_DONE[1] else "shut down"
        if messagebox.askyesno("Lecture Digest",
                               f"All done. The PC will {verb} in 1 minute.\n\nCancel that?"):
            self.after_cancel(job)


if __name__ == "__main__":
    App().mainloop()
