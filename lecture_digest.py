#!/usr/bin/env python3
"""
Lecture Digest - turn lecture videos into compact, timestamped digests that
Claude (or any LLM) can read to answer questions about the lectures.

Everything runs locally and for free:
  * faster-whisper (NVIDIA GPU) for the timestamped transcript
  * FFmpeg + change detection to save a keyframe whenever the slide / board changes
  * RapidOCR (optional) to read the text on those keyframes

Commands
  process  VIDEO_OR_FOLDER   build digests (resumable, skips finished work)
  frames   LECTURE --start --end   pull extra frames for a time range
  search   "words"           find where something was said / shown
  index                      rebuild INDEX.md and CLAUDE.md in the output folder

Run  python lecture_digest.py <command> -h  for all options.
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v",
              ".mpg", ".mpeg", ".ts", ".3gp"}
DEFAULT_OUT = "digests"
FORMAT_VERSION = 1

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def fmt_srt(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def parse_ts(text: str) -> float:
    """Accepts 90, 90.5, 1:30, 01:02:03."""
    parts = [float(p) for p in str(text).strip().split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"ERROR: '{tool}' was not found on PATH. Install FFmpeg first (see README.md).")


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def find_videos(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def lecture_dir_for(video: Path, input_root: Path, out_root: Path) -> Path:
    if input_root.is_file():
        rel = Path(video.stem)
    else:
        rel = video.relative_to(input_root).with_suffix("")
    return out_root / rel


# --------------------------------------------------------------------------- #
# CUDA libraries installed with pip (nvidia-cublas-cu12 / nvidia-cudnn-cu12)
# --------------------------------------------------------------------------- #
def setup_cuda_libs() -> None:
    try:
        import nvidia  # namespace package created by the nvidia-* wheels
    except ImportError:
        return
    roots = list(getattr(nvidia, "__path__", []))
    if os.name == "nt":
        for root in roots:
            for bin_dir in glob.glob(os.path.join(root, "*", "bin")):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                try:
                    os.add_dll_directory(bin_dir)
                except OSError:
                    pass
    else:
        # Preload so CTranslate2's dlopen() finds them without LD_LIBRARY_PATH.
        patterns = ["cublas/lib/libcublasLt.so*", "cublas/lib/libcublas.so*",
                    "cudnn/lib/libcudnn*.so*"]
        for root in roots:
            for pat in patterns:
                for lib in sorted(glob.glob(os.path.join(root, pat))):
                    try:
                        ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass


# --------------------------------------------------------------------------- #
# Transcription
# --------------------------------------------------------------------------- #
_MODEL = None


class Progress:
    """Counts files and video time for a whole run and prints a PROGRESS line
    (human-readable text + JSON for the window) with an estimated time left."""

    def __init__(self, durations: list[float]):
        self.durations = durations
        self.total = len(durations)
        self.completed = self.failed = self.skipped = 0
        self.total_sec = sum(durations)
        self.finished_sec = 0.0        # video time of files completed, failed or skipped
        self.work_sec = 0.0            # video time actually processed in this run
        self.current_sec = 0.0         # transcribed part of the file being worked on
        self.current_name = ""
        self.started = time.time()
        self.last_emit = 0.0

    def begin(self, name: str) -> None:
        self.current_name, self.current_sec = name, 0.0
        self.emit(force=True)

    def partial(self, sec: float) -> None:
        self.current_sec = sec
        if time.time() - self.last_emit >= 5:
            self.emit()

    def end(self, index: int, outcome: str) -> None:
        dur = self.durations[index]
        self.finished_sec += dur
        if outcome == "skipped":
            self.skipped += 1
            self.completed += 1
        elif outcome == "done":
            self.completed += 1
            self.work_sec += dur
        else:
            self.failed += 1
        self.current_sec, self.current_name = 0.0, ""
        self.emit(force=True)

    def emit(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_emit < 5:
            return
        self.last_emit = now
        elapsed = now - self.started
        # transcription is ~most of the work; count the part done of the current file
        done_work = self.work_sec + self.current_sec * 0.8
        left_sec = max(0.0, self.total_sec - self.finished_sec - self.current_sec * 0.8)
        speed = done_work / elapsed if elapsed > 1 and done_work > 0 else 0.0
        eta = left_sec / speed if speed > 0 else None
        remaining = self.total - self.completed - self.failed
        info = {"total": self.total, "completed": self.completed, "failed": self.failed,
                "skipped": self.skipped, "remaining": remaining, "current": self.current_name,
                "total_sec": round(self.total_sec), "left_sec": round(left_sec),
                "elapsed_sec": round(elapsed), "speed": round(speed, 1),
                "eta_sec": None if eta is None else round(eta)}
        eta_txt = "00:00:00" if remaining == 0 else "estimating..." if eta is None else fmt_ts(eta)
        print(f"PROGRESS {self.completed}/{self.total} done, {self.failed} failed, {remaining} left, "
              f"time left {eta_txt} | {json.dumps(info, ensure_ascii=False)}", flush=True)


PROGRESS: Progress | None = None


class ModelLoadError(RuntimeError):
    """The speech model could not be downloaded or loaded; every video would fail."""


def get_model(args):
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    setup_cuda_libs()
    from faster_whisper import WhisperModel

    device = args.device
    compute = args.compute_type
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
        if device == "cpu":
            log("WARNING: no CUDA GPU detected, using CPU (much slower). See README troubleshooting.")
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    log(f"Loading Whisper model '{args.model}' on {device} ({compute}) - first run downloads it...")
    try:
        _MODEL = WhisperModel(args.model, device=device, compute_type=compute)
    except Exception as e:
        raise ModelLoadError(f"could not load the Whisper model '{args.model}': {e}") from e
    return _MODEL


def load_audio(video: Path):
    """Decode the soundtrack to 16 kHz mono float32 with FFmpeg.

    Done here instead of letting faster-whisper use PyAV, whose API changes
    between releases and broke decoding (unexpected keyword 'metadata_errors').
    """
    import numpy as np
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(video),
           "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 and not proc.stdout:
        raise RuntimeError("could not read the audio: " + proc.stderr.decode(errors="replace").strip()[-300:])
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(video: Path, duration: float, args) -> dict:
    model = get_model(args)
    audio = load_audio(video)
    if audio.size < 16000:
        log("  no audio track (or under 1 s of sound)")
        return {"language": "", "model": args.model, "task": args.task, "segments": []}
    segments, info = model.transcribe(
        audio,
        language=None if args.language == "auto" else args.language,
        task=args.task,
        beam_size=args.beam_size,
        vad_filter=True,
        condition_on_previous_text=False,  # avoids repetition loops on long audio
        initial_prompt=args.prompt or None,
    )
    log(f"  language: {info.language} (p={info.language_probability:.2f})")
    out, last_report, started = [], 0.0, time.time()
    for seg in segments:
        text = seg.text.strip()
        if text:
            out.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})
        if PROGRESS is not None:
            PROGRESS.partial(seg.end)
        if duration and seg.end - last_report >= 300:
            last_report = seg.end
            speed = seg.end / max(1e-6, time.time() - started)
            log(f"  transcribed {fmt_ts(seg.end)} / {fmt_ts(duration)} ({speed:.0f}x realtime)")
    return {"language": info.language, "model": args.model, "task": args.task, "segments": out}


def write_srt(segments: list[dict], path: Path) -> None:
    lines = []
    for i, s in enumerate(segments, 1):
        lines += [str(i), f"{fmt_srt(s['start'])} --> {fmt_srt(s['end'])}", s["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Keyframes (slides / board)
# --------------------------------------------------------------------------- #
SAMPLE_W, SAMPLE_H = 160, 90


def sample_frames(video: Path, fps: float, hwaccel: str):
    """Yield (time, small grayscale frame) at `fps` samples per second."""
    import numpy as np
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-v", "error"]
    if hwaccel != "none":
        cmd += ["-hwaccel", hwaccel]
    cmd += ["-i", str(video), "-an", "-sn", "-dn",
            "-vf", f"fps={fps},scale={SAMPLE_W}:{SAMPLE_H}:flags=area,format=gray",
            "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size, i = SAMPLE_W * SAMPLE_H, 0
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield i / fps, np.frombuffer(buf, dtype=np.uint8).astype(np.int16)
            i += 1
    finally:
        proc.stdout.close()
        proc.wait()
    if i == 0 and hwaccel != "none":
        log("  hardware decoding gave no frames, retrying on CPU")
        yield from sample_frames(video, fps, "none")


def detect_keyframe_times(video: Path, args) -> list[tuple[float, float]]:
    """Pick moments when the slide / board content changed.

    Every sample is compared with the last saved keyframe, so a new slide, a
    wiped board or slowly accumulated board writing all count as a change.
    Pixels that moved in the last few seconds (the teacher, a webcam inset, a
    hand writing, a slide transition) are ignored, so a keyframe is taken once
    the new content has settled. A frame is also taken at least every --max-gap s.
    Returns (time the content appeared, time to grab the settled image).
    """
    import numpy as np
    settle = 3.0  # seconds a pixel must stay still before it counts
    times: list[tuple[float, float]] = []
    ref = prev = last_moved = None
    for t, frame in sample_frames(video, args.sample_fps, args.hwaccel):
        if ref is None:
            ref, prev = frame, frame
            last_moved = np.full(frame.shape, -1e9)
            times.append((t, t))
            continue
        last_moved[np.abs(frame - prev) > 20] = t
        static = (t - last_moved) >= settle
        diff = float(((np.abs(frame - ref) > 30) & static).mean())
        since = t - times[-1][1]
        if diff > args.change and since >= args.min_gap:
            times.append((max(times[-1][1], t - settle), t))
            ref = frame
        elif args.max_gap > 0 and since >= args.max_gap:
            times.append((t, t))  # nothing changed for a long time: take one anyway
            ref = frame
        prev = frame
    return times


def grab_frame(video: Path, t: float, dest: Path, width: int) -> bool:
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
           "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-q:v", "4", str(dest)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dest.exists() and dest.stat().st_size > 0


def extract_keyframes(video: Path, duration: float, lec_dir: Path, args) -> list[dict]:
    frames_dir = lec_dir / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)
    times = detect_keyframe_times(video, args)
    log(f"  {len(times)} keyframes detected, saving images")
    kept: list[dict] = []
    for shown, grab in times:
        name = f"frame_{len(kept) + 1:04d}_{fmt_ts(shown).replace(':', '-')}.jpg"
        if grab_frame(video, grab, frames_dir / name, args.frame_width):
            kept.append({"time": round(shown, 2), "file": name, "ocr": ""})
    return kept


# --------------------------------------------------------------------------- #
# OCR (optional)
# --------------------------------------------------------------------------- #
_OCR = None


def ocr_image(path: Path) -> str:
    global _OCR
    if _OCR is None:
        try:
            from rapidocr import RapidOCR
        except ImportError:
            sys.exit("ERROR: OCR needs 'pip install rapidocr onnxruntime' (or run without --ocr).")
        import logging
        logging.getLogger("RapidOCR").setLevel(logging.WARNING)  # its INFO lines flood the log
        _OCR = RapidOCR()
        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
    result = _OCR(str(path))
    txts = getattr(result, "txts", None)
    if txts is None and isinstance(result, tuple):  # older rapidocr API
        txts = [r[1] for r in (result[0] or [])]
    return "\n".join(t.strip() for t in (txts or []) if t and t.strip())


# --------------------------------------------------------------------------- #
# Digest writing
# --------------------------------------------------------------------------- #
def paragraphs(segments: list[dict], max_seconds: float = 45) -> list[tuple[float, str]]:
    """Group short Whisper segments into ~45 s paragraphs, each with a timestamp."""
    out, cur, start = [], [], None
    for s in segments:
        if start is None:
            start = s["start"]
        cur.append(s["text"])
        ends_sentence = s["text"].rstrip().endswith((".", "?", "!", "।"))
        if s["end"] - start >= max_seconds and ends_sentence or s["end"] - start >= max_seconds * 2:
            out.append((start, " ".join(cur)))
            cur, start = [], None
    if cur:
        out.append((start, " ".join(cur)))
    return out


def write_digest(lec_dir: Path, meta: dict, transcript: dict | None, frames: list[dict]) -> None:
    segs = (transcript or {}).get("segments", [])
    title = meta["title"]
    L = [f"# {title}", "",
         f"- Source video: `{meta['source']}`",
         f"- Duration: {fmt_ts(meta['duration'])}",
         f"- Language: {(transcript or {}).get('language', '?')}",
         f"- Keyframes: {len(frames)} (images in `frames/`, OCR text below each)",
         f"- Generated: {meta['updated']}", "",
         "Timestamps are [HH:MM:SS] into the video. Exact per-sentence timings are in "
         "`transcript.json`; an `.srt` subtitle file is next to it.", ""]

    if frames:
        L += ["## Outline (keyframes)", ""]
        for i, f in enumerate(frames, 1):
            first = next((ln for ln in f.get("ocr", "").splitlines() if len(ln) > 3), "")
            L.append(f"- [{fmt_ts(f['time'])}] Frame {i}" + (f": {first[:90]}" if first else ""))
        L.append("")

    L += ["## Timeline", ""]
    marks = frames or [{"time": 0.0, "file": None, "ocr": ""}]
    for i, f in enumerate(marks):
        start = f["time"] if i else 0.0
        stop = marks[i + 1]["time"] if i + 1 < len(marks) else float("inf")
        chunk = [s for s in segs if start <= s["start"] < stop]
        if f.get("file"):
            L.append(f"### [{fmt_ts(f['time'])}] Frame {i + 1} - `frames/{f['file']}`")
            if f.get("ocr"):
                L += ["", "On screen:", "```", f["ocr"], "```"]
            L.append("")
        for t, text in paragraphs(chunk):
            L.append(f"[{fmt_ts(t)}] {text}")
            L.append("")
    if not segs:
        L += ["_(no speech transcribed)_", ""]
    (lec_dir / "digest.md").write_text("\n".join(L), encoding="utf-8")


def process_video(video: Path, input_root: Path, out_root: Path, args) -> bool:
    lec_dir = lecture_dir_for(video, input_root, out_root)
    lec_dir.mkdir(parents=True, exist_ok=True)
    meta_path = lec_dir / "meta.json"
    meta = read_json(meta_path, {}) if not args.force else {}
    stat = video.stat()
    fingerprint = [stat.st_size, int(stat.st_mtime)]
    if meta.get("fingerprint") != fingerprint:
        meta = {}  # new or changed video: redo everything
    done = set(meta.get("done", []))
    want = {"transcript"} | (set() if args.no_frames else {"frames"}) | ({"ocr"} if args.ocr else set())
    if want <= done and (lec_dir / "digest.md").exists():
        log(f"skip (already done): {video.name}")
        return False

    log(f"=== {video}")
    duration = meta.get("duration") or probe_duration(video)
    meta.update({"format": FORMAT_VERSION, "title": video.stem, "source": str(video.resolve()),
                 "fingerprint": fingerprint, "duration": duration})

    if "transcript" not in done:
        t0 = time.time()
        transcript = transcribe(video, duration, args)
        write_json(lec_dir / "transcript.json", transcript)
        write_srt(transcript["segments"], lec_dir / "transcript.srt")
        done.add("transcript")
        log(f"  transcript: {len(transcript['segments'])} segments in {time.time() - t0:.0f}s")
    else:
        transcript = read_json(lec_dir / "transcript.json")

    frames = read_json(lec_dir / "frames.json", [])
    if "frames" in want and "frames" not in done:
        t0 = time.time()
        frames = extract_keyframes(video, duration, lec_dir, args)
        write_json(lec_dir / "frames.json", frames)
        done.add("frames")
        done.discard("ocr")
        log(f"  keyframes done in {time.time() - t0:.0f}s")

    if "ocr" in want and "ocr" not in done and frames:
        t0 = time.time()
        for f in frames:
            f["ocr"] = ocr_image(lec_dir / "frames" / f["file"])
        write_json(lec_dir / "frames.json", frames)
        done.add("ocr")
        log(f"  OCR done in {time.time() - t0:.0f}s")

    meta["done"] = sorted(done)
    meta["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    write_digest(lec_dir, meta, transcript, frames if "frames" in done else [])
    write_json(meta_path, meta)
    log(f"  -> {lec_dir / 'digest.md'}")
    return True


# --------------------------------------------------------------------------- #
# Index + CLAUDE.md for the output folder
# --------------------------------------------------------------------------- #
CLAUDE_MD = """# Lecture digests - guide for Claude

This folder holds digests of the user's study lecture videos, made by
`lecture_digest.py`. Use them to answer the user's questions and clear doubts.

Layout (one folder per lecture, mirroring the original video folders):
- `INDEX.md` - list of every lecture with duration and keyframe count.
- `<lecture>/digest.md` - START HERE. Outline of the slides/board, then the full
  transcript in ~45 s paragraphs, grouped under the keyframe that was on screen.
- `<lecture>/frames/*.jpg` - the keyframes themselves (slides, board, diagrams).
  Open them when a question is about a diagram, equation or anything visual.
- `<lecture>/transcript.json` - exact sentence-level timings
  (`segments[].start/end/text`, seconds).
- `<lecture>/frames.json` - keyframe times, file names and OCR text.

How to answer:
1. Find the relevant lecture(s): read INDEX.md, or grep across `*/digest.md`
   (e.g. `grep -ril "fourier" --include=digest.md .`), or run
   `python lecture_digest.py search "fourier transform" -o <this folder>`.
2. Read the matching part of digest.md and look at the nearby frames.
3. Answer, citing lecture name and [HH:MM:SS] so the user can jump to it.
4. Transcripts come from speech recognition and may mis-hear technical terms;
   cross-check with the on-screen text and say when something is uncertain.
5. If you need to see more of a moment than the keyframes show, ask the user to run:
   `python lecture_digest.py frames "<lecture folder>" --start 12:30 --end 14:00`
   (writes images to `<lecture>/extra_frames/`).
"""


def build_index(out_root: Path) -> int:
    rows = []
    for meta_path in sorted(out_root.rglob("meta.json")):
        meta = read_json(meta_path, {})
        if not meta.get("title"):
            continue
        rel = meta_path.parent.relative_to(out_root).as_posix()
        frames = read_json(meta_path.parent / "frames.json", [])
        rows.append(f"| {rel} | {fmt_ts(meta.get('duration', 0))} | {len(frames)} | "
                    f"[digest]({rel.replace(' ', '%20')}/digest.md) |")
    total = sum(read_json(p, {}).get("duration", 0) for p in out_root.rglob("meta.json"))
    lines = ["# Lecture index", "",
             f"{len(rows)} lectures, {fmt_ts(total)} total. Read CLAUDE.md for how this folder is organised.", "",
             "| Lecture | Duration | Keyframes | Digest |", "|---|---|---|---|", *rows, ""]
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    (out_root / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")
    return len(rows)


def write_combined(out_root: Path, max_mb: float = 20) -> list[Path]:
    """Join every digest.md into ALL_LECTURES.md (split into parts of ~max_mb),
    handy for uploading to a Claude Project as knowledge files."""
    for old in out_root.glob("ALL_LECTURES*.md"):
        old.unlink()
    parts, cur, size = [], [], 0
    for digest in sorted(out_root.rglob("digest.md")):
        rel = digest.parent.relative_to(out_root).as_posix()
        text = digest.read_text(encoding="utf-8").replace("`frames/", f"`{rel}/frames/")
        body = text.split("\n", 1)[1] if text.startswith("# ") else text
        block = f"\n\n---\n\n# {rel}\n{body}"
        n = len(block.encode("utf-8"))
        if cur and size + n > max_mb * 1024 * 1024:
            parts.append(cur)
            cur, size = [], 0
        cur.append(block)
        size += n
    if cur:
        parts.append(cur)
    written = []
    for i, blocks in enumerate(parts, 1):
        name = "ALL_LECTURES.md" if len(parts) == 1 else f"ALL_LECTURES_part{i}.md"
        head = (f"# All lecture digests{'' if len(parts) == 1 else f' (part {i} of {len(parts)})'}\n\n"
                "Each lecture starts with a `# <folder>/<lecture>` heading. "
                "Timestamps are [HH:MM:SS] into that lecture's video.")
        path = out_root / name
        path.write_text(head + "".join(blocks) + "\n", encoding="utf-8")
        written.append(path)
    return written


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_process(args) -> None:
    require_ffmpeg()
    src = Path(args.input).expanduser()
    if not src.exists():
        sys.exit(f"ERROR: {src} does not exist")
    out_root = Path(args.output).expanduser()
    videos = find_videos(src)
    log(f"Found {len(videos)} video(s). Output: {out_root.resolve()}")
    log("Measuring video lengths for the time estimate...")
    global PROGRESS
    PROGRESS = Progress([probe_duration(v) for v in videos])
    log(f"Total video length: {fmt_ts(PROGRESS.total_sec)}")
    failures = successes = 0
    recent_errors: list[str] = []
    for i, video in enumerate(videos, 1):
        log(f"[{i}/{len(videos)}]")
        PROGRESS.begin(video.name)
        try:
            worked = process_video(video, src, out_root, args)
            successes += 1
            PROGRESS.end(i - 1, "done" if worked else "skipped")
        except KeyboardInterrupt:
            log("Stopped. Run the same command again to resume.")
            break
        except Exception as e:
            failures += 1
            PROGRESS.end(i - 1, "failed")
            msg = f"{datetime.now():%Y-%m-%d %H:%M} {video}\n{traceback.format_exc()}\n"
            out_root.mkdir(parents=True, exist_ok=True)
            with open(out_root / "errors.log", "a", encoding="utf-8") as fh:
                fh.write(msg)
            log(f"  FAILED: {e} (details in errors.log)")
            if isinstance(e, ModelLoadError):
                log("  The speech model is needed for every video, so stopping here. Check your internet")
                log("  connection (the first run downloads it) and free disk space, then start again.")
                break
            if any(k in str(e).lower() for k in ("cublas", "cudnn", "cuda")):
                log("  This is a GPU/CUDA setup problem, so every video would fail. Stopping.")
                log("  See README 'Troubleshooting', or add --device cpu to run (slowly) without the GPU.")
                break
            recent_errors.append(f"{type(e).__name__}: {e}")
            if successes == 0 and len(recent_errors) >= 3 and len(set(recent_errors)) == 1:
                log("  The same error happened for the first 3 videos, so it is probably a setup")
                log("  problem rather than bad videos. Stopping. Send errors.log to Claude for help.")
                break
        build_index(out_root)
    n = build_index(out_root)
    if args.combined:
        for path in write_combined(out_root):
            log(f"Combined file: {path}")
    log(f"Done. {n} lectures in index, {failures} failure(s).")


def resolve_lecture(target: str, out_root: Path) -> tuple[Path, Path]:
    """Accept a video file, a lecture digest folder, or a lecture name; return (video, lecture_dir)."""
    p = Path(target).expanduser()
    if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
        for meta_path in out_root.rglob("meta.json"):
            if Path(read_json(meta_path, {}).get("source", "")) == p.resolve():
                return p, meta_path.parent
        return p, out_root / p.stem
    candidates = [p, out_root / target]
    candidates += [m.parent for m in out_root.rglob("meta.json") if m.parent.name == target]
    for d in candidates:
        meta = read_json(d / "meta.json")
        if meta:
            video = Path(meta["source"])
            if not video.exists():
                sys.exit(f"ERROR: original video moved or missing: {video}")
            return video, d
    sys.exit(f"ERROR: could not find a video or lecture called '{target}'")


def cmd_frames(args) -> None:
    require_ffmpeg()
    video, lec_dir = resolve_lecture(args.lecture, Path(args.output))
    start, end = parse_ts(args.start), parse_ts(args.end)
    if end <= start:
        sys.exit("ERROR: --end must be after --start")
    dest = Path(args.dest) if args.dest else (
        lec_dir / "extra_frames" / f"{fmt_ts(start).replace(':', '-')}_to_{fmt_ts(end).replace(':', '-')}")
    dest.mkdir(parents=True, exist_ok=True)
    t, n = start, 0
    while t <= end:
        name = dest / f"{fmt_ts(t).replace(':', '-')}_{int(round(t * 1000)) % 1000:03d}.jpg"
        if grab_frame(video, t, name, args.frame_width):
            n += 1
        t += args.every
    log(f"Saved {n} frame(s) to {dest}")


def cmd_search(args) -> None:
    out_root = Path(args.output)
    words = [w.lower() for w in args.query.split()]
    hits = 0
    for tpath in sorted(out_root.rglob("transcript.json")):
        lec = tpath.parent.relative_to(out_root).as_posix()
        segs = read_json(tpath, {}).get("segments", [])
        frames = read_json(tpath.parent / "frames.json", [])
        items = []
        for i in range(len(segs)):
            window = " ".join(s["text"] for s in segs[i:i + 3])
            if all(w in window.lower() for w in words):
                items.append((segs[i]["start"], "said", window))
        for f in frames:
            if f.get("ocr") and all(w in f["ocr"].lower() for w in words):
                items.append((f["time"], "slide", f["ocr"].replace("\n", " | ")))
        last = -1e9
        for t, kind, text in sorted(items):
            if t - last < 20:  # overlapping windows
                continue
            last = t
            hits += 1
            print(f"{lec}  [{fmt_ts(t)}]  ({kind})  {text[:200]}")
            if hits >= args.limit:
                print(f"... stopped at {args.limit} hits (use --limit)")
                return
    if not hits:
        print("No matches.")


def cmd_index(args) -> None:
    n = build_index(Path(args.output))
    if args.combined:
        for path in write_combined(Path(args.output)):
            log(f"Combined file: {path}")
    log(f"INDEX.md and CLAUDE.md written for {n} lectures in {Path(args.output).resolve()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("process", help="build digests for a video or a whole folder (resumable)")
    p.add_argument("input", help="a video file or a folder (searched recursively)")
    p.add_argument("-o", "--output", default=DEFAULT_OUT, help=f"output folder (default: {DEFAULT_OUT})")
    p.add_argument("--model", default="large-v3-turbo",
                   help="Whisper model: large-v3-turbo (default, fast+accurate), large-v3 (best, slower), "
                        "medium, small, base (for weak GPUs/CPU)")
    p.add_argument("--language", default="auto", help="e.g. en, hi, ta... (default: auto-detect)")
    p.add_argument("--task", default="transcribe", choices=["transcribe", "translate"],
                   help="'translate' turns non-English speech into English text")
    p.add_argument("--prompt", default="", help="subject words to help spelling, e.g. 'thermodynamics, enthalpy, Carnot'")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    p.add_argument("--compute-type", default="auto",
                   help="auto (float16 on GPU), int8_float16 (less VRAM), int8 (CPU)")
    p.add_argument("--beam-size", type=int, default=5)
    p.add_argument("--ocr", action="store_true", help="read text on keyframes with RapidOCR (free, local)")
    p.add_argument("--no-frames", action="store_true", help="transcript only, no keyframes")
    p.add_argument("--change", type=float, default=0.01,
                   help="fraction of the screen that must change to count as a new keyframe "
                        "(default 0.01 = 1%%; raise to 0.03 if a moving teacher/webcam gives too many frames)")
    p.add_argument("--min-gap", type=float, default=5.0, help="at least this many seconds between keyframes (default 5)")
    p.add_argument("--max-gap", type=float, default=120.0,
                   help="take a frame at least this often even if nothing changed, in seconds; 0 = off (default 120)")
    p.add_argument("--sample-fps", type=float, default=1.0, help="how often to check the screen (default 1/s)")
    p.add_argument("--frame-width", type=int, default=1280, help="max keyframe width in px (default 1280)")
    p.add_argument("--hwaccel", default="auto", help="FFmpeg decode acceleration: auto, cuda, none")
    p.add_argument("--force", action="store_true", help="redo lectures that are already finished")
    p.add_argument("--combined", action="store_true",
                   help="also write ALL_LECTURES.md with every digest in one file (for Claude Projects)")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("frames", help="save extra frames for a time range of one lecture")
    p.add_argument("lecture", help="video file, lecture digest folder, or lecture name")
    p.add_argument("--start", required=True, help="e.g. 12:30 or 1:02:03")
    p.add_argument("--end", required=True)
    p.add_argument("--every", type=float, default=5.0, help="seconds between frames (default 5)")
    p.add_argument("--frame-width", type=int, default=1920)
    p.add_argument("--dest", help="where to save (default: <lecture>/extra_frames/...)")
    p.add_argument("-o", "--output", default=DEFAULT_OUT, help="digests folder")
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser("search", help="find where words were said or shown on a slide")
    p.add_argument("query")
    p.add_argument("-o", "--output", default=DEFAULT_OUT, help="digests folder")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("index", help="rebuild INDEX.md and CLAUDE.md")
    p.add_argument("-o", "--output", default=DEFAULT_OUT, help="digests folder")
    p.add_argument("--combined", action="store_true", help="also write ALL_LECTURES.md")
    p.set_defaults(func=cmd_index)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
