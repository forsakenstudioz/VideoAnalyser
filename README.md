# Lecture Digest

Turn a big library of lecture videos into small, searchable, timestamped text
digests that Claude can read to answer your questions and clear your doubts.

Everything runs on your own PC, for free. No paid API, nothing is uploaded.

- **Speech → transcript** with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your NVIDIA GPU, with timestamps.
- **Slides and board → keyframes.** A picture is saved whenever the slide or board content changes (moving teachers/webcams are ignored).
- **Optional OCR** of those keyframes with RapidOCR, so slide text and equations become searchable text too.
- **One `digest.md` per lecture**: outline of the slides, then the transcript grouped under the slide that was on screen, every paragraph stamped `[HH:MM:SS]`.
- **Resumable**: stop any time (Ctrl+C) and run the same command again. Finished lectures are skipped.

A 1 hour lecture becomes roughly 60 KB of text plus 20–80 small JPGs, so 500 GB of
video ends up as a few GB of digests.

---

## Easy way (Windows): LectureDigest.exe

1. Double-click **LectureDigest.exe**. Windows may say *"Windows protected your PC"*
   because the app is new and unsigned: click **More info → Run anyway**.
2. The first time, a black window downloads and installs everything it needs
   (Python, FFmpeg, the speech and OCR packages: about 2 GB, 10–30 minutes).
   Nothing is installed system-wide; it all goes into `%LOCALAPPDATA%\LectureDigest`.
   Later starts take a few seconds.
3. In the window: **Choose folder...** (or **Choose video...**), pick where to save the
   digests, and press **Start**. The first job also downloads the speech model (1.6 GB, once).
   The **Progress** box shows how many videos are done, failed and left, and the estimated time left.
4. You can press **Stop** or close it any time. Press **Start** again later and it
   carries on where it stopped.
5. The **Extra frames** tab saves more pictures from part of a lecture.
6. Extras: **Dark mode** (top right), **Keep the PC awake** while it runs, and **When done** can put the PC to
   sleep or shut it down (with a 1-minute chance to cancel). Your folders and options are remembered.

The only thing you need beforehand is an up-to-date NVIDIA driver (NVIDIA App / GeForce
Experience). To uninstall, delete `%LOCALAPPDATA%\LectureDigest` and the exe.

**One digest per video, or everything in one file?** You get both. Each video gets its
own `digest.md` (best for Claude: it can read just the lecture it needs, with the slide
pictures). The **combined file** option also writes `ALL_LECTURES.md` with every lecture
in one file, which is handy for uploading to a Claude Project. 12 GB of video is roughly
15–30 hours, which makes a combined file of a few hundred thousand words: fine as Project
knowledge, but too long to paste into a single chat.

The rest of this README is the manual / command-line way, for Linux or if you prefer a terminal.

---

## 1. Setup on Windows (one time)

1. **NVIDIA driver**: make sure it is recent (GeForce Experience / NVIDIA App → update).
   In a terminal, `nvidia-smi` should print your GPU. You do **not** need the CUDA Toolkit.
2. **Python 3.11 or 3.12** from <https://www.python.org/downloads/>. Tick **"Add python.exe to PATH"** in the installer.
3. **FFmpeg**: open *Terminal* / *PowerShell* and run
   ```
   winget install Gyan.FFmpeg
   ```
   then close and reopen the terminal. `ffmpeg -version` should work.
4. Put this folder somewhere (e.g. `C:\lecture-digest`), open a terminal in it and run
   ```
   setup_windows.bat
   ```
   This creates a private Python environment in `.venv` and installs everything
   (about 1.5 GB, mostly the CUDA libraries).

Every time you want to use it afterwards:
```
cd C:\lecture-digest
.venv\Scripts\activate
```

## 1b. Setup on Linux (one time)

```
sudo apt install ffmpeg python3-venv     # or your distro's equivalent
nvidia-smi                               # should show your GPU
./setup_linux.sh
. .venv/bin/activate                     # each time you use it
```

---

## 2. Make the digests

Process one video to check everything works:
```
python lecture_digest.py process "D:\Lectures\Physics\Lecture 01.mp4" -o "D:\LectureDigests" --ocr
```
The first run downloads the Whisper model (about 1.6 GB, once).

Then the whole library (folders are searched recursively and mirrored in the output):
```
python lecture_digest.py process "D:\Lectures" -o "D:\LectureDigests" --ocr
```

Useful options:

| Option | What it does |
|---|---|
| `--language en` | Skip auto-detection. Use `hi`, `ta`, `te`… for other languages. For Hinglish lectures try `--language en` first, or `--language hi`. |
| `--task translate` | Write an English transcript of non-English speech. |
| `--prompt "enthalpy, Carnot, Kirchhoff"` | Subject words so Whisper spells technical terms right. |
| `--model large-v3` | Most accurate, about 2× slower. Default `large-v3-turbo` is the best speed/quality balance. Use `medium` or `small` for GPUs with ≤4 GB VRAM. |
| `--compute-type int8_float16` | Uses less GPU memory if you get "out of memory". |
| `--no-frames` | Transcript only (for audio-only or talking-head lectures). |
| `--change 0.03` | Fewer keyframes (if a moving teacher creates too many). Lower, e.g. `0.005`, catches smaller changes. |
| `--max-gap 120` | Take a frame at least every N seconds even if nothing changed (`0` = off). |
| `--force` | Redo lectures that are already finished. |
| `--combined` | Also write `ALL_LECTURES.md` with every digest in one file (split into ~20 MB parts if large). |

You can run without `--ocr` first and add it later: the next run only does the OCR step.

**How long will it take?** On a mid-range GPU (RTX 3060 class) `large-v3-turbo` runs
around 30–60× real time, so a 1 hour lecture takes 1–2 minutes plus about a minute
for keyframes. 500 GB is very roughly 500–1000 hours of video, so plan for 1–3 days
of background processing. It is fine to run it overnight in chunks.

### What you get

```
D:\LectureDigests\
  CLAUDE.md                ← tells Claude how to use this folder
  INDEX.md                 ← every lecture, duration, link to its digest
  errors.log               ← only if some video failed
  Physics\Lecture 01\
    digest.md              ← read this: outline + timestamped transcript + slide text
    transcript.json        ← exact sentence timings
    transcript.srt         ← subtitles, load next to the video in VLC
    frames.json            ← keyframe times + OCR text
    frames\frame_0003_00-12-30.jpg
    meta.json
```

---

## 3. Pull extra frames for a moment

When the keyframes do not show enough (e.g. a derivation written step by step):
```
python lecture_digest.py frames "Lecture 01" --start 12:30 --end 14:00 -o "D:\LectureDigests"
python lecture_digest.py frames "D:\Lectures\Physics\Lecture 01.mp4" --start 1:02:00 --end 1:03:00 --every 2 -o "D:\LectureDigests"
```
Images go to `<lecture>\extra_frames\...`. Default is one frame every 5 seconds.

## 4. Search across all lectures

```
python lecture_digest.py search "fourier transform" -o "D:\LectureDigests"
```
Prints `lecture [HH:MM:SS] (said|slide) text…` for every lecture where those words
were spoken or shown on a slide.

---

## 5. Ask Claude about your lectures

Give Claude access to the **digests folder** (not the videos). Any of these work:

- **Claude desktop app / Claude Code**: open or connect the `LectureDigests` folder.
  Claude reads `CLAUDE.md` automatically, then searches the digests, reads the right
  part and looks at the slide images. This scales to the whole library.
- **A Claude Project on claude.ai**: upload `ALL_LECTURES.md` (made with the combined
  option, or `--combined`), or the `digest.md` files of one course, as project knowledge.

Then just ask, e.g. *"In the thermodynamics lectures, why did the teacher say the
Carnot cycle is reversible? Explain it simply"* or *"Which lecture covers Laplace
transforms, and what example was used?"* Claude answers and cites the lecture and
timestamp, so you can jump to that moment in the video.

---

## Troubleshooting

- **"Library cublas64_12.dll is not found" / "cudnn… not found" / CUDA errors**:
  run `pip install -r requirements.txt` again inside the activated `.venv`, and update
  your NVIDIA driver. To check the rest works meanwhile, add `--device cpu --model small`.
- **"WARNING: no CUDA GPU detected"**: `nvidia-smi` must work in the same terminal.
  Update the driver and reboot.
- **Out of GPU memory**: `--compute-type int8_float16`, or `--model medium`.
- **Wrong language / gibberish transcript**: set `--language` explicitly.
- **Too many or too few keyframes**: adjust `--change` (see the table above).
- **A video failed**: see `errors.log` in the output folder. The rest keep going;
  run the command again later to retry the failed ones.
- **Moved the videos?** Digests still work. Only the `frames` command needs the
  original video; point it at the video file directly.
