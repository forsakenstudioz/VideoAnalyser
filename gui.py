"""
Lecture Digest - simple window for people who do not want to use a terminal.

Runs lecture_digest.py (next to this file) in the background and shows its
progress. Started by LectureDigest.exe, or directly with:  python gui.py
Built with customtkinter, in a green "swordsman" theme (dark and light).
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

HERE = Path(__file__).resolve().parent
TOOL = HERE / "lecture_digest.py"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
SETTINGS_FILE = Path.home() / ".lecture_digest_gui.json"

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
WHEN_DONE = ["Do nothing", "Sleep", "Shut down"]
VIDEO_TYPES = [("Videos", "*.mp4 *.mkv *.avi *.mov *.webm *.flv *.wmv *.m4v *.mpg *.mpeg *.ts *.3gp"),
               ("All files", "*.*")]

# Colours as (light, dark). Swordsman green with a touch of katana gold.
C = {
    "bg": ("#eef5ef", "#0b1410"),
    "card": ("#ffffff", "#13221a"),
    "card2": ("#e3efe6", "#1a2e22"),
    "border": ("#cfe2d4", "#24402f"),
    "text": ("#0f2418", "#e8f5ec"),
    "muted": ("#5b7465", "#8fb39c"),
    "green": ("#1f8a45", "#3ccf6e"),
    "green_hover": ("#176b36", "#2fae5b"),
    "green_dark": ("#145c2e", "#1f8a45"),
    "gold": ("#a8801c", "#d9b44a"),
    "danger": ("#b4232a", "#e05252"),
    "on_green": ("#ffffff", "#06120b"),
    "log_bg": ("#f6faf7", "#08100c"),
}
FONT = "Segoe UI" if os.name == "nt" else "DejaVu Sans"


def font(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


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


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, m = sec // 3600, sec % 3600 // 60
    if h >= 24:
        return f"{h // 24}d {h % 24}h"
    return f"{h}h {m:02d}m" if h else f"{m} min"


class Card(ctk.CTkFrame):
    """Rounded panel with an optional small heading."""

    def __init__(self, master, title: str = "", **kw):
        super().__init__(master, fg_color=C["card"], corner_radius=14, border_width=1,
                         border_color=C["border"], **kw)
        if title:
            ctk.CTkLabel(self, text=title.upper(), font=font(11, "bold"),
                         text_color=C["green"]).pack(anchor="w", padx=16, pady=(12, 2))


class StatTile(ctk.CTkFrame):
    def __init__(self, master, label: str):
        super().__init__(master, fg_color=C["card2"], corner_radius=10)
        self.value = ctk.CTkLabel(self, text="-", font=font(20, "bold"), text_color=C["text"])
        self.value.pack(anchor="w", padx=12, pady=(8, 0))
        ctk.CTkLabel(self, text=label, font=font(11), text_color=C["muted"]).pack(anchor="w", padx=12, pady=(0, 8))

    def set(self, text: str) -> None:
        self.value.configure(text=text)


class App(ctk.CTk):
    def __init__(self) -> None:
        self.settings = st = load_settings()
        ctk.set_appearance_mode(st.get("appearance", "Dark"))
        super().__init__(fg_color=C["bg"])
        self.title("Lecture Digest")
        self.geometry("900x860")
        self.minsize(760, 640)
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str | None] = queue.Queue()
        self._on_done = None
        self._last_line = ""

        pick = lambda key, choices, default: st.get(key) if st.get(key) in choices else default  # noqa: E731
        self.input_var = ctk.StringVar(value=st.get("input", ""))
        self.output_var = ctk.StringVar(value=st.get("output") or str(Path.home() / "Documents" / "LectureDigests"))
        self.lang_var = ctk.StringVar(value=pick("language", LANGUAGES, "Auto-detect"))
        self.quality_var = ctk.StringVar(value=pick("quality", QUALITY, next(iter(QUALITY))))
        self.ocr_var = ctk.BooleanVar(value=st.get("ocr", True))
        self.translate_var = ctk.BooleanVar(value=st.get("translate", False))
        self.combined_var = ctk.BooleanVar(value=st.get("combined", True))
        self.prompt_var = ctk.StringVar(value=st.get("prompt", ""))
        self.awake_var = ctk.BooleanVar(value=st.get("keep_awake", True))
        self.after_var = ctk.StringVar(value=pick("when_done", WHEN_DONE, WHEN_DONE[0]))
        self.sound_var = ctk.BooleanVar(value=st.get("sound", True))
        self.appearance_var = ctk.StringVar(value=ctk.get_appearance_mode())
        self.status_var = ctk.StringVar(value="Choose a video or a folder of videos, then press Start.")
        self.extra_lecture = ctk.StringVar()
        self.extra_start = ctk.StringVar(value="00:10:00")
        self.extra_end = ctk.StringVar(value="00:11:00")
        self.extra_every = ctk.StringVar(value="5")

        self.gpu = detect_gpu()
        if not self.gpu:
            self.quality_var.set("Light (small GPU or no GPU)")
        self._build()
        self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI --
    def _build(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=22, pady=(18, 6))
        ctk.CTkFrame(header, width=6, height=46, corner_radius=3, fg_color=C["green"]).pack(side="left", padx=(0, 12))
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(titles, text="Lecture Digest", font=font(26, "bold"), text_color=C["text"]).pack(anchor="w")
        ctk.CTkLabel(titles, text="Turn lecture videos into notes you can ask Claude about",
                     font=font(12), text_color=C["muted"]).pack(anchor="w")
        ctk.CTkSegmentedButton(header, values=["Dark", "Light"], variable=self.appearance_var,
                               command=self._set_appearance, font=font(12),
                               selected_color=C["green"], selected_hover_color=C["green_hover"],
                               unselected_color=C["card2"], unselected_hover_color=C["border"],
                               text_color=C["text"]).pack(side="right")
        chip_text = f"GPU  {self.gpu}" if self.gpu else "No NVIDIA GPU - will run slowly on the CPU"
        ctk.CTkLabel(header, text=chip_text, font=font(11, "bold"), corner_radius=12,
                     fg_color=C["card2"], text_color=C["green"] if self.gpu else C["gold"],
                     padx=12).pack(side="right", padx=12)

        tabs = ctk.CTkTabview(self, fg_color=C["bg"], segmented_button_fg_color=C["card2"],
                              segmented_button_selected_color=C["green"],
                              segmented_button_selected_hover_color=C["green_hover"],
                              segmented_button_unselected_color=C["card2"],
                              segmented_button_unselected_hover_color=C["border"],
                              text_color=C["text"], anchor="w")
        tabs.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self._build_main(tabs.add("Make digests"))
        self._build_extra(tabs.add("Extra frames"))

    def _entry(self, parent, var, placeholder="", width=140):
        return ctk.CTkEntry(parent, textvariable=var, height=36, width=width, corner_radius=10, font=font(12),
                            fg_color=C["card2"], border_color=C["border"], text_color=C["text"],
                            placeholder_text=placeholder or None)

    def _button(self, parent, text, cmd, primary=False, danger=False, **kw):
        if primary:
            colors = dict(fg_color=C["green"], hover_color=C["green_hover"], text_color=C["on_green"])
        elif danger:
            colors = dict(fg_color="transparent", hover_color=C["card2"], text_color=C["danger"],
                          border_width=1, border_color=C["danger"])
        else:
            colors = dict(fg_color=C["card2"], hover_color=C["border"], text_color=C["text"],
                          border_width=1, border_color=C["border"])
        kw.setdefault("height", 36)
        return ctk.CTkButton(parent, text=text, command=cmd, corner_radius=10,
                             font=font(12, "bold" if primary else "normal"), **colors, **kw)

    def _switch(self, parent, text, var):
        return ctk.CTkSwitch(parent, text=text, variable=var, font=font(12), text_color=C["text"],
                             progress_color=C["green"], button_color=("#ffffff", "#e8f5ec"),
                             button_hover_color=("#f0f0f0", "#ffffff"), fg_color=C["border"])

    def _menu(self, parent, var, values, width=220):
        return ctk.CTkOptionMenu(parent, variable=var, values=values, width=width, height=34,
                                 corner_radius=10, font=font(12), fg_color=C["card2"],
                                 button_color=C["green_dark"], button_hover_color=C["green"],
                                 text_color=C["text"], dropdown_fg_color=C["card"],
                                 dropdown_hover_color=C["card2"], dropdown_text_color=C["text"])

    def _path_row(self, card, label, var, buttons, placeholder=""):
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=5)
        ctk.CTkLabel(row, text=label, width=110, anchor="w", font=font(12), text_color=C["muted"]).pack(side="left")
        self._entry(row, var, placeholder).pack(side="left", fill="x", expand=True, padx=(0, 8))
        for text, cmd in buttons:
            self._button(row, text, cmd, width=96).pack(side="left", padx=2)

    def _build_main(self, tab) -> None:
        body = ctk.CTkScrollableFrame(tab, fg_color=C["bg"], scrollbar_button_color=C["border"],
                                      scrollbar_button_hover_color=C["green_dark"])
        body.pack(fill="both", expand=True)

        src = Card(body, "Videos")
        src.pack(fill="x", pady=(0, 10))
        self._path_row(src, "Videos", self.input_var,
                       [("Video...", self._pick_video), ("Folder...", self._pick_folder)],
                       "Pick one video or a whole folder")
        self._path_row(src, "Save digests to", self.output_var, [("Browse...", self._pick_output)])
        ctk.CTkFrame(src, height=8, fg_color="transparent").pack()

        opts = Card(body, "Options")
        opts.pack(fill="x", pady=(0, 10))
        grid = ctk.CTkFrame(opts, fg_color="transparent")
        grid.pack(fill="x", padx=16, pady=4)
        for col, (label, var, values, width) in enumerate((("Language", self.lang_var, list(LANGUAGES), 180),
                                                           ("Quality", self.quality_var, list(QUALITY), 260))):
            ctk.CTkLabel(grid, text=label, width=110 if col == 0 else 0, anchor="w", font=font(12),
                         text_color=C["muted"]).grid(row=0, column=col * 2, sticky="w", padx=(0 if col == 0 else 18, 8))
            self._menu(grid, var, values, width=width).grid(row=0, column=col * 2 + 1, sticky="w")
        row = ctk.CTkFrame(opts, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(8, 2))
        ctk.CTkLabel(row, text="Subject words", width=110, anchor="w", font=font(12),
                     text_color=C["muted"]).pack(side="left")
        self._entry(row, self.prompt_var, "Optional, e.g. enthalpy, Kirchhoff, eigenvalue").pack(
            side="left", fill="x", expand=True)
        sw = ctk.CTkFrame(opts, fg_color="transparent")
        sw.pack(fill="x", padx=16, pady=(10, 14))
        for text, var in (("Read slide and board text (OCR)", self.ocr_var),
                          ("Translate to English", self.translate_var),
                          ("One combined file for Claude", self.combined_var)):
            self._switch(sw, text, var).pack(side="left", padx=(0, 18))

        run = Card(body, "While running")
        run.pack(fill="x", pady=(0, 10))
        rr = ctk.CTkFrame(run, fg_color="transparent")
        rr.pack(fill="x", padx=16, pady=(4, 14))
        self._switch(rr, "Keep PC awake", self.awake_var).pack(side="left", padx=(0, 18))
        self._switch(rr, "Sound when done", self.sound_var).pack(side="left", padx=(0, 18))
        ctk.CTkLabel(rr, text="When done", font=font(12), text_color=C["muted"]).pack(side="left", padx=(6, 8))
        ctk.CTkSegmentedButton(rr, values=WHEN_DONE, variable=self.after_var, font=font(12),
                               selected_color=C["green"], selected_hover_color=C["green_hover"],
                               unselected_color=C["card2"], unselected_hover_color=C["border"],
                               text_color=C["text"]).pack(side="left")

        actions = ctk.CTkFrame(body, fg_color=C["bg"])
        actions.pack(fill="x", pady=(2, 10))
        self.start_btn = self._button(actions, "Start", self._start, primary=True, width=150, height=44)
        self.start_btn.configure(font=font(15, "bold"))
        self.start_btn.pack(side="left")
        self.stop_btn = self._button(actions, "Stop", self._stop, danger=True, width=100, height=44)
        self.stop_btn.configure(state="disabled")
        self.stop_btn.pack(side="left", padx=10)
        self._button(actions, "Open digests folder",
                     lambda: open_folder(Path(self.output_var.get())), width=170, height=44).pack(side="right")

        prog = Card(body, "Progress")
        prog.pack(fill="x", pady=(0, 10))
        self.progress = ctk.CTkProgressBar(prog, height=12, corner_radius=6, progress_color=C["green"],
                                           fg_color=C["card2"])
        self.progress.set(0)
        self.progress.pack(fill="x", padx=16, pady=(6, 4))
        ctk.CTkLabel(prog, textvariable=self.status_var, font=font(12), text_color=C["text"],
                     wraplength=760, justify="left").pack(anchor="w", padx=16, pady=(0, 6))
        tiles = ctk.CTkFrame(prog, fg_color="transparent")
        tiles.pack(fill="x", padx=12, pady=(0, 14))
        self.tiles: dict[str, StatTile] = {}
        for i, (key, label) in enumerate([("total", "Total videos"), ("completed", "Completed"),
                                          ("failed", "Failed"), ("remaining", "Remaining"),
                                          ("video", "Video done"), ("elapsed", "Time spent"),
                                          ("eta", "Time left"), ("finish", "Finishes around")]):
            tiles.columnconfigure(i % 4, weight=1, uniform="t")
            tile = self.tiles[key] = StatTile(tiles, label)
            tile.grid(row=i // 4, column=i % 4, sticky="nsew", padx=4, pady=4)
        self.tiles["eta"].value.configure(text_color=C["green"])

        log = Card(body, "Details")
        log.pack(fill="x", pady=(0, 6))
        self.log = ctk.CTkTextbox(log, height=170, corner_radius=10, font=ctk.CTkFont(family="Consolas", size=11),
                                  fg_color=C["log_bg"], text_color=C["muted"], border_width=0, wrap="word")
        self.log.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.log.configure(state="disabled")
        ctk.CTkLabel(body, text="You can stop any time; Start again continues where it stopped. When it is done, "
                                "give Claude the digests folder (or upload ALL_LECTURES.md) and ask your questions.",
                     font=font(11), text_color=C["muted"], wraplength=780, justify="left").pack(anchor="w", pady=(2, 6))

    def _build_extra(self, tab) -> None:
        card = Card(tab, "Save more pictures from part of a lecture")
        card.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(card, wraplength=760, justify="left", font=font(12), text_color=C["muted"], text=(
            "Useful when the teacher writes a long derivation. Pick the lecture's folder inside your digests "
            "folder (or the video itself), then the start and end time.")).pack(anchor="w", padx=16, pady=(2, 6))
        self._path_row(card, "Lecture", self.extra_lecture,
                       [("Folder...", self._pick_extra_folder), ("Video...", self._pick_extra_video)])
        r = ctk.CTkFrame(card, fg_color="transparent")
        r.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(r, text="", width=110).pack(side="left")
        for label, var, w in (("From (h:mm:ss)", self.extra_start, 100), ("To", self.extra_end, 100),
                              ("Every (seconds)", self.extra_every, 60)):
            ctk.CTkLabel(r, text=label, font=font(12), text_color=C["muted"]).pack(side="left", padx=(0, 6))
            self._entry(r, var, width=w).pack(side="left", padx=(0, 16))
        self.extra_btn = self._button(card, "Save pictures", self._extra_frames, primary=True, width=160, height=40)
        self.extra_btn.pack(anchor="w", padx=16, pady=(4, 6))
        ctk.CTkLabel(card, font=font(11), text_color=C["muted"],
                     text="Pictures go into the lecture's 'extra_frames' folder, which opens when they are ready."
                     ).pack(anchor="w", padx=16, pady=(0, 14))

    def _set_appearance(self, mode: str) -> None:
        ctk.set_appearance_mode(mode)
        self.settings["appearance"] = mode
        save_settings(self.settings)

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
        if not self.gpu:
            args += ["--device", "cpu"]
        self._save()
        self._keep_awake(self.awake_var.get())
        self.start_btn.configure(state="disabled")
        self.extra_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.set(0)
        for tile in self.tiles.values():
            tile.set("-")
        self.status_var.set("Starting... (the first time it downloads the speech model, about 1.6 GB)")
        self._run(args, on_done=self._finished)

    def _finished(self, code: int) -> None:
        self._keep_awake(False)
        self.start_btn.configure(state="normal")
        self.extra_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if code == 0:
            self.progress.set(1)
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
        self.extra_btn.configure(state="disabled")

        def done(code):
            self.start_btn.configure(state="normal")
            self.extra_btn.configure(state="normal")
            m = re.search(r"Saved \d+ frame\(s\) to (.+)$", self._last_line)
            if code == 0 and m:
                open_folder(Path(m.group(1)))
            else:
                messagebox.showwarning("Lecture Digest", self._last_line or "Could not save pictures.")

        self._run(args, on_done=done)

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
        if text.startswith("Found "):
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
        t = self.tiles
        t["total"].set(str(p["total"]))
        t["completed"].set(str(p["completed"]))
        t["failed"].set(str(p["failed"]))
        t["remaining"].set(str(p["remaining"]))
        t["video"].set(f"{fmt_duration(done_sec)} / {fmt_duration(p['total_sec'])}")
        t["elapsed"].set(fmt_duration(p["elapsed_sec"]))
        if p["eta_sec"] is None:
            t["eta"].set("estimating" if p["remaining"] else "0 min")
            t["finish"].set("-")
        else:
            t["eta"].set(fmt_duration(p["eta_sec"]))
            t["finish"].set(time.strftime("%a %I:%M %p", time.localtime(time.time() + p["eta_sec"])))
        self.progress.set(done_sec / p["total_sec"] if p["total_sec"] else 0)

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
            "sound": self.sound_var.get(), "appearance": self.appearance_var.get()})
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
        if action == "Sleep":
            cmd = ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
        else:
            cmd = ["shutdown", "/s", "/t", "0"]
        job = self.after(60_000, lambda: subprocess.Popen(cmd, creationflags=NO_WINDOW))
        verb = "go to sleep" if action == "Sleep" else "shut down"
        if messagebox.askyesno("Lecture Digest",
                               f"All done. The PC will {verb} in 1 minute.\n\nCancel that?"):
            self.after_cancel(job)


if __name__ == "__main__":
    App().mainloop()
