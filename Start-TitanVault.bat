@echo off
rem ============================================================
rem  TitanVault launcher (extra version)
rem  Starts the bot with the JDownloader fallback enabled.
rem
rem  Before first use:
rem   1. Install JDownloader 2 and keep it RUNNING.
rem   2. In JDownloader: Settings -^> Advanced Settings -^>
rem      set RemoteAPI.deprecatedapienabled = true
rem      (leave port 3128, keep localhost-only enabled).
rem  This file does not configure JDownloader for you.
rem
rem  Optional HLS engine: keep the extracted N_m3u8DL-RE win-x64 folder in
rem  Downloads (found automatically), or set N_M3U8DL_PATH to the .exe.
rem  It runs only after the yt-dlp and FFmpeg steps fail. N_M3U8DL_MODE=off disables it.
rem ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3 or fix your PATH.
    pause
    exit /b 1
)

set JD_MODE=local
echo Starting TitanVault with JDownloader fallback enabled...
echo Close this window to stop the bot.
echo.
python titan_vault.py

echo.
echo TitanVault stopped. If it exited unexpectedly, copy the text above.
pause
