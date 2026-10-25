"""Experimental download diagnostics: per-leg stage/status facts, redaction guarantees.
Local fixtures only; token-style secrets in URLs must never leak into diagnostics."""
import os, sys, asyncio, threading, types
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("API_ID", "1"); os.environ.setdefault("API_HASH", "x")
os.environ.setdefault("BOT_TOKEN", "x"); os.environ.setdefault("STORAGE_CHANNEL_ID", "-1")
os.environ.setdefault("OWNER_ID", "1"); os.environ.setdefault("SESSION_STRING", "")
os.environ.setdefault("DOWNLOAD_DIR", "/tmp/diag_dl")
os.makedirs("/tmp/diag_dl", exist_ok=True)
import titanvault as tv

EDITS = []
class FakeMsg:
    chat_id = 7
    def __init__(self): self.id = 1
    async def reply_text(self, text, **kw): EDITS.append(text); m = FakeMsg(); m.id = len(EDITS)+1; return m
    async def edit_text(self, text, **kw): EDITS.append(text); return self

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        path = self.path
        if path.startswith("/p403/"): return self._r(403)
        if path.startswith("/seg/master.m3u8"):
            body = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\n/seg/seg1.ts?token=SECRETSEG\n#EXT-X-ENDLIST\n"
            return self._r(200, body, "application/vnd.apple.mpegurl")
        if path.startswith("/seg/seg1.ts"): return self._r(403)
        if path.startswith("/direct.mp4"): return self._r(403)
        return self._r(404)
    def _r(self, code, body=b"", ctype="text/plain"):
        if isinstance(body, str): body = body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

failed = []
def ok(cond, name):
    print(("PASS" if cond else "FAIL") + ": " + name)
    if not cond: failed.append(name)

# units
ok(tv._classify_hls_stage("Unable to download webpage: HTTP Error 403") == "playlist fetch", "classify: playlist")
ok(tv._classify_hls_stage("Failed to download segment 3") == "segment fetch", "classify: segment")
ok(tv._classify_hls_stage("HTTP Error 403: Forbidden") == "download", "classify: unknown stays download")
ok(tv._diag_detail("HTTP Error 404: Not Found") == "HTTP 404", "detail: status kept")
ok(tv._diag_detail("The read operation timed out") == "timeout", "detail: timeout")
ok(tv._diag_detail("[generic] tok123: Unable to download webpage: HTTP Error 403") == "HTTP 403", "detail: token fragment dropped, status kept")
ok(tv._redact_url("https://host.tld/a/b?token=X") == "https://host.tld/...", "redact_url strips path+query")

async def main():
    # A: playlist 403 through the full HLS leg
    tv._diag_reset(7)
    r = await tv._do_dl_via_ffmpeg_hls(BASE + "/p403/master.m3u8?token=SECRETP", FakeMsg(), [False], 7, 7)
    ok(r is False, "HLS playlist-403: leg fails")
    stages = [(l, s) for l, s, d in tv.dl_diag[7]]
    ok(("HLS via yt-dlp", "playlist fetch") in stages, "HLS playlist-403: yt-dlp stage recorded")
    ok(("HLS via ffmpeg", "playlist fetch") in stages, "HLS playlist-403: ffmpeg stage recorded")
    ok(any(d == "HTTP 403" for l, s, d in tv.dl_diag[7]), "HLS playlist-403: status recorded")
    ok("SECRET" not in tv._diag_summary(7), "HLS playlist-403: no token in summary")

    # B: segment 403
    tv._diag_reset(7)
    r = await tv._do_dl_via_ffmpeg_hls(BASE + "/seg/master.m3u8?token=SECRETM", FakeMsg(), [False], 7, 7)
    ok(r is False, "HLS segment-403: leg fails")
    ok(any(s == "segment fetch" for l, s, d in tv.dl_diag[7]), "HLS segment-403: segment stage recorded")
    ok("SECRET" not in tv._diag_summary(7), "HLS segment-403: no token in summary")

    # C: cffi direct 403 + user-facing leak check
    tv._diag_reset(7); EDITS.clear()
    r = await tv._do_dl_via_cffi(BASE + "/direct.mp4?token=SECRETD", FakeMsg(), [False], 7, 7)
    ok(r is False, "cffi direct-403: leg fails")
    ok(("direct via curl", "download") in [(l, s) for l, s, d in tv.dl_diag[7]], "cffi direct-403: leg recorded")
    ok(any(d == "HTTP 403" for l, s, d in tv.dl_diag[7]), "cffi direct-403: status recorded")
    ok(all("SECRETD" not in e for e in EDITS), "cffi direct-403: user-facing message carries no token")
    ok("SECRETD" not in tv._diag_summary(7), "cffi direct-403: no token in summary")

    # D: summary shape
    tv._diag_reset(7)
    tv._diag(7, "resolver", "detection", "no stream found")
    tv._diag(7, "JDownloader", "-", "unavailable or failed")
    s = tv._diag_summary(7)
    ok("safe to share" in s and "resolver" in s and "JDownloader" in s, "summary: header + legs present")
    ok(tv._diag_summary(99) == "", "summary: empty for unknown uid")

asyncio.run(main())
print("RESULT:", "FAIL " + str(failed) if failed else "PASS")
