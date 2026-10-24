Tests used for this change (local fixtures only, no private files, no Telegram connection).
test_compress.py   - compression safety (real ffmpeg for the happy and silent-source cases, scripted fake ffmpeg for failure cases)
test_engines.py    - /stream direct mp4 + HLS, JDownloader local backend (needs a running JDownloader with the Deprecated API) and cancel/offline
test_bridge.py     - loopback receiver: Origin/Host/token/pairing/limits
test_kept.py       - kept files after a failed upload, cap/backpressure, Retry/Delete targeting
test_extension.py  - real Chromium with the extension loaded (test manifest grants host permissions because the permission prompt cannot be clicked headlessly)
Run from this folder: python3 test_compress.py  etc. They need /tmp/fx fixtures (see test files).
