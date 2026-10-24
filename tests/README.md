Tests used for this change (local fixtures only, no private files, no Telegram connection).
test_compress.py   - compression safety (real ffmpeg for the happy and silent-source cases, scripted fake ffmpeg for failure cases)
test_engines.py    - /stream direct mp4 + HLS, JDownloader local backend (needs a running JDownloader with the Deprecated API) and cancel/offline
test_bridge.py     - loopback receiver: Origin/Host/token/pairing/limits
test_kept.py       - kept files after a failed upload, cap/backpressure, Retry/Delete targeting
test_extension.py  - real Chromium with the extension loaded (test manifest grants host permissions because the permission prompt cannot be clicked headlessly)
test_streamlink.py - Streamlink resolver fallback: unsupported URLs pass through, hls:// fixture, header allowlist
test_catcatch.py   - aria2 JSON-RPC endpoint for cat-catch: token auth, origin/host guard, option filtering, shared rate limit, /catcatch command
test_catcatch_e2e.py - real cat-catch extension end to end: options UI setup, popup detection, aria2 button click against the live bridge (needs playwright chromium, ffmpeg, xvfb and a cat-catch checkout; CC_EXT_DIR overrides the path)
Run from this folder: python3 test_compress.py  etc. They need /tmp/fx fixtures (see test files).
