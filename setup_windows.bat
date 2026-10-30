@echo off
REM One-time setup for Lecture Digest on Windows. Run from this folder.
where python >nul 2>nul || (echo Python not found. Install Python 3.11 or 3.12 from python.org and tick "Add to PATH". & exit /b 1)
where ffmpeg >nul 2>nul || (echo FFmpeg not found. Run:  winget install Gyan.FFmpeg   then open a NEW terminal. & exit /b 1)
python -m venv .venv || exit /b 1
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || exit /b 1
echo.
echo Setup done. Each time, open a terminal here and run:
echo   .venv\Scripts\activate
echo   python lecture_digest.py process "D:\Lectures" -o "D:\LectureDigests" --ocr
