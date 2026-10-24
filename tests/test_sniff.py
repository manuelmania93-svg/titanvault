"""test_sniff.py - generic sniffer upgrades: body sniffing, iframe play, meta scrape.

Self-contained local fixtures only (no external network, no Telegram connection).
Needs real Chromium via playwright (same requirement as test_extension.py).
Run from this folder: python3 test_sniff.py
"""
import http.server, socketserver, threading, sys, os, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

M3U8 = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg0.ts\n#EXT-X-ENDLIST\n"
MP4_FAKE = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2" + b"0" * 100
ZIP_FAKE = b"PK\x03\x04" + b"0" * 200

PAGES = {
    "/case_opaque_hls": (
        "<html><body><script>fetch('/api/stream?sid=42').then(r=>r.text());</script></body></html>"),
    "/case_iframe_play": (
        "<html><body><iframe src='/frame' width='640' height='360'></iframe></body></html>"),
    "/case_meta": (
        "<html><head><meta property='og:video' content='/files/clip.mp4'></head><body>x</body></html>"),
    "/case_control": (
        "<html><body><script>fetch('/hls/master.m3u8').then(r=>r.text());</script></body></html>"),
    "/case_opaque_mp4": (
        "<html><body><script>fetch('/cdn/obj/9f8e7d6c').then(r=>r.arrayBuffer());</script></body></html>"),
    "/case_not_media": (
        "<html><body><script>fetch('/files/archive.zip').then(r=>r.arrayBuffer());</script></body></html>"),
    "/case_big_body": (
        "<html><body><script>fetch('/big/chunk').then(r=>r.arrayBuffer());</script></body></html>"),
    "/case_textplain_hls": (
        "<html><body><script>fetch('/plist/get?id=7').then(r=>r.text());</script></body></html>"),
    "/case_evil_meta": (
        "<html><head><meta property='og:video' content='javascript:alert(1)'>"
        "<meta property='og:video:secure_url' content='/media/ok.mp4'></head><body>x</body></html>"),
}
FRAME = ("<html><body><button class='play-button' id='pb'>Play</button><video></video>"
         "<script>document.getElementById('pb').onclick=function(){"
         "fetch('/media/master.m3u8').then(r=>r.text());};</script></body></html>")


class S(socketserver.TCPServer):
    allow_reuse_address = True


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _h(self, body, ct="text/html"):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _b(self, body, ct):
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path
        if p in PAGES:
            return self._h(PAGES[p])
        if p == "/frame":
            return self._h(FRAME)
        if p == "/api/stream?sid=42":
            return self._h(M3U8, "application/octet-stream")
        if p in ("/hls/master.m3u8", "/media/master.m3u8"):
            return self._h(M3U8, "application/vnd.apple.mpegurl")
        if p == "/plist/get?id=7":
            return self._h(M3U8, "text/plain")
        if p == "/cdn/obj/9f8e7d6c":
            return self._b(MP4_FAKE, "application/octet-stream")
        if p in ("/files/clip.mp4", "/media/ok.mp4"):
            return self._b(MP4_FAKE, "video/mp4")
        if p == "/files/archive.zip":
            return self._b(ZIP_FAKE, "application/octet-stream")
        if p == "/big/chunk":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(10 * 1024 * 1024))
            self.end_headers()
            try:
                self.wfile.write(b"0" * (10 * 1024 * 1024))
            except Exception:
                pass
            return
        self.send_response(404)
        self.end_headers()


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    return cond


def main():
    srv = S(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    import titanvault as tv
    ok = True

    r = tv._universal_snatch_sync(base + "/case_opaque_hls")
    ok &= check("opaque HLS URL (octet-stream) -> body sniff", r and r.get("type") == "hls"
                and r["url"].endswith("/api/stream?sid=42"))

    r = tv._universal_snatch_sync(base + "/case_iframe_play")
    ok &= check("play button inside iframe -> activated", r and r["url"].endswith("/media/master.m3u8"))

    r = tv._universal_snatch_sync(base + "/case_meta")
    ok &= check("og:video metadata only -> scraped", r and r["url"].endswith("/files/clip.mp4"))

    r = tv._universal_snatch_sync(base + "/case_control")
    ok &= check("plain .m3u8 on load (control)", r and r["url"].endswith("/hls/master.m3u8"))

    r = tv._universal_snatch_sync(base + "/case_opaque_mp4")
    ok &= check("opaque mp4 URL (octet-stream) -> ftyp sniff", r and r.get("type") == "direct"
                and r["url"].endswith("/cdn/obj/9f8e7d6c"))

    r = tv._universal_snatch_sync(base + "/case_not_media")
    ok &= check("zip at octet-stream -> no false positive", not r)

    r = tv._universal_snatch_sync(base + "/case_big_body")
    ok &= check("10MB body -> sniff bound respected, skipped", not r)

    r = tv._universal_snatch_sync(base + "/case_textplain_hls")
    ok &= check("text/plain manifest -> body sniff", r and r.get("type") == "hls")

    r = tv._universal_snatch_sync(base + "/case_evil_meta")
    ok &= check("javascript: meta rejected, legit secure_url accepted",
                r and r["url"].endswith("/media/ok.mp4"))

    srv.shutdown()
    print("RESULT:", "PASS" if ok else "FAIL", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
