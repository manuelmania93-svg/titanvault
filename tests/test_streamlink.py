from harness import *
import threading, http.server, socketserver, sys, types as _t

ok(hasattr(tv, "_streamlink_resolve"), "_streamlink_resolve exists")
ok(tv.STREAMLINK_ENABLE, "streamlink fallback on by default")

# 1. URL no plugin handles -> None, no exception, fast (NoPluginError, no network)
r = tv._streamlink_resolve("https://no-such-plugin-host.invalid/watch/123")
ok(r is None, "unsupported URL -> None (chain continues)")

# 2. direct .mp4 page URL is not streamlink's job -> None, chain continues to snatch/yt-dlp
r = tv._streamlink_resolve("https://cdn.example.invalid/video.mp4")
ok(r is None, "plain file URL -> None")

# 3. hls:// passthrough against a local fixture playlist
PORT = 18991
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg0.ts\n#EXT-X-ENDLIST\n"
        self.send_response(200); self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
srv = socketserver.TCPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
r = tv._streamlink_resolve(f"hls://http://127.0.0.1:{PORT}/list.m3u8")
ok(r is not None and r["url"] == f"http://127.0.0.1:{PORT}/list.m3u8", f"hls:// passthrough resolves: {r and r.get('url')}")
ok(isinstance(r["headers"], dict) and not any(k.lower() == "cookie" for k in r["headers"]), "headers dict, never a Cookie")
srv.shutdown()

# 4. best-stream selection
ok(tv._streamlink_pick({}) is None, "pick: empty -> None")
class S: url = "u"
ok(tv._streamlink_pick({"worst": S(), "best": "B"}) == "B", "pick prefers best")
ok(tv._streamlink_pick({"360p": "L"}) == "L", "pick falls back to a known quality")

# 5. header allowlist + CRLF stripping with a fake streamlink module
real_mod = sys.modules.get("streamlink")
class FakeStream: url = "https://cdn.example/vid/720.m3u8"; args = {"headers": {"Referer": "https://host.example/watch\r\nX-Bad: 1", "Cookie": "sid=1"}}
class FakeHTTP: headers = {"User-Agent": "SL/1.0", "Authorization": "Bearer x", "Accept-Language": "en"}
class FakeSession:
    http = FakeHTTP()
    def set_option(self, *a): pass
    def streams(self, url): return {"best": FakeStream()}
fake = _t.ModuleType("streamlink"); fake.Streamlink = lambda: FakeSession()
sys.modules["streamlink"] = fake
r = tv._streamlink_resolve("https://anything.example/x")
ok(r["url"].endswith("720.m3u8"), "fake plugin resolves")
ok(r["headers"].get("Referer") == "https://host.example/watchX-Bad: 1" or "\r" not in r["headers"].get("Referer",""), "CRLF stripped from header values")
ok("Cookie" not in r["headers"] and "Authorization" not in r["headers"], "Cookie/Authorization filtered out")
ok(r["headers"].get("User-Agent") == "SL/1.0", "User-Agent kept")
if real_mod is not None: sys.modules["streamlink"] = real_mod
else: sys.modules.pop("streamlink", None)

sys.exit(1 if ok.failed else 0)
