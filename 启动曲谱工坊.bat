@echo off
REM ============================================
REM  Score Studio launcher (ASCII only, per bat-encoding rule)
REM  Points the pipeline at this machine's Python venv (Pillow ready),
REM  then starts the frameless Tauri app.
REM ============================================
setlocal
set "SCORE_PYTHON=C:\Users\19388\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "SCORE_PIPELINE=D:\Lin_Agent\WB-WorkSpace\2026-08-17-00-34-56\score-studio\sheet_pipeline.py"
start "" "D:\Lin_Agent\WB-WorkSpace\2026-08-17-00-34-56\score-studio\src-tauri\target\release\score-studio.exe"
endlocal
