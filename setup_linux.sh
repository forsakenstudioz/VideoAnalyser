#!/usr/bin/env bash
# One-time setup for Lecture Digest on Linux. Run from this folder.
set -e
command -v ffmpeg >/dev/null || { echo "FFmpeg not found: sudo apt install ffmpeg  (or your distro's package)"; exit 1; }
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo
echo "Setup done. Each time:"
echo "  . .venv/bin/activate"
echo "  python lecture_digest.py process ~/Lectures -o ~/LectureDigests --ocr"
