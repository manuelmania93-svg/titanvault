@echo off
setlocal

rem ============================================================
rem  TitanVault launcher
rem  Starts the bot without JDownloader fallback.
rem ============================================================
cd /d "%~dp0"

if not exist "titan_vault.py" (
    echo titan_vault.py is missing next to this file.
    pause
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3 or fix your PATH.
    pause
    exit /b 1
)

rem --- required secrets live in the Windows user environment, never in this file ---
for %%V in (API_ID API_HASH BOT_TOKEN OWNER_ID STORAGE_CHANNEL_ID) do (
    if not defined %%V (
        echo Missing environment variable %%V - set it in your Windows user environment, then rerun this file.
        pause
        exit /b 1
    )
)

set TITANVAULT_DEBUG=1
set CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe

rem --- Cloudflare: a persistent per-host profile keeps cf_clearance alive ---
set CF_PROFILE=1
set CF_HEADFUL=1
set SNATCH_MAX_SEC=240

rem --- capture tiers: CDP tier on, and the exe engine only as a fallback ---
set NODRIVER_TIER=1
set N_M3U8DL_MODE=fallback
set DRM_PROBE=1

rem --- driver + browser deps; instant when they are already installed ---
python -m pip install -q --disable-pip-version-check python-telegram-bot telethon Pillow colorama requests patchright nodriver nodriver-turnstile curl_cffi yt-dlp
python -m patchright install chromium

echo Starting TitanVault...
echo Close this window to stop the bot.
echo.
python titan_vault.py

echo.
echo TitanVault stopped. If it exited unexpectedly, copy the text above.
pause