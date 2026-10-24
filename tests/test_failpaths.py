"""test_failpaths.py - failure-recovery fixes: media gate, chain continuation, header handoff.

Self-contained local fixtures (two loopback servers), stubbed Telegram UI,
no external network beyond localhost, no Telegram connection.
Needs ffmpeg/ffprobe and real Chromium (same as test_sniff.py).
Run from this folder: python3 test_failpaths.py
"""
import http.server, socketserver, threading, sys, os, time, subprocess, asyncio, glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DOWNLOAD_DIR", "/tmp/failpath_dl")

HTML_FAKE = ("<html><body><h1>Error</h1>" + "x" * 6000 + "</body></html>").encode()
PAGE_HLS404 = "<html><body><iframe src='/frame404'></iframe></body></html>"
FRAME404 = ("<html><body><button class='play-button' id='pb'>P</button>"
            "<script>document.getElementById('pb').onclick=function(){fetch('/media/master.m3u8');};</script></body></html>")
PAGE_META_FAKEMP4 = "<html><head><meta property='og:video' content='/files/fake.mp4'></head><body>x</body></html>"

M3U8_BODY = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nseg0.ts\n#EXT-X-ENDLIST\n"


class S(socketserver.TCPServer):
    allow_reuse_address = True


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _h(self, body, ct="text/html", code=200, extra=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path
        if p == "/hls404":
            return self._h(PAGE_HLS404)
        if p == "/frame404":
            return self._h(FRAME404)
        if p == "/media/master.m3u8":
            return self._h("<html>404</html>", "text/html", 404)
        if p == "/metafake":
            return self._h(PAGE_META_FAKEMP4)
        if p == "/files/fake.mp4":
            return self._h(HTML_FAKE, "video/mp4")
        if p == "/trunc.mp4":
            payload = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2" + b"0" * 50000
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except Exception:
                pass
            self.connection.shutdown(1)
            self.connection.close()
            return
        if p == "/xorigin_page":
            # page on server A; its script fetches media on server B (cross-origin -> Origin header sent)
            port_b = self.server.port_b
            html = (f"<html><body><script>fetch('http://127.0.0.1:{port_b}/vid/pl', "
                    "{mode:'cors'}).then(r=>r.text());</script></body></html>")
            return self._h(html)
        self.send_response(404)
        self.end_headers()


class HB(H):
    """Server B: only serves media when the Origin header points at server A."""

    def do_GET(self):
        p = self.path
        origin_ok = self.headers.get("Origin", "").startswith("http://127.0.0.1:")
        if p == "/vid/pl":
            if not origin_ok:
                return self._h("forbidden", "text/plain", 403)
            return self._h(M3U8_BODY, "application/vnd.apple.mpegurl",
                           extra={"Access-Control-Allow-Origin": "*"})
        if p == "/vid/seg0.ts":
            if not origin_ok and "Referer" not in self.headers:
                return self._h("forbidden", "text/plain", 403)
            return self._b(open("/tmp/failpath_seg0.ts", "rb").read(), "video/mp2t")
        self.send_response(404)
        self.end_headers()

    def _b(self, body, ct):
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


EDITS = []


class FakeStatus:
    async def edit_text(self, text, **kw):
        EDITS.append(text)
        return self

    async def reply_text(self, text, **kw):
        EDITS.append("REPLY: " + text)
        return self


class FakeMsg:
    chat_id = 1

    async def reply_text(self, text, **kw):
        EDITS.append("REPLY: " + text)
        return FakeStatus()


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    return cond


def main():
    os.makedirs("/tmp/failpath_dl", exist_ok=True)
    # tiny real media segment for the header-handoff fixture
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=5",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    "-f", "mpegts", "/tmp/failpath_seg0.ts"], check=True)

    srv_a = S(("127.0.0.1", 0), H)
    srv_b = S(("127.0.0.1", 0), HB)
    srv_a.port_b = srv_b.server_address[1]
    threading.Thread(target=srv_a.serve_forever, daemon=True).start()
    threading.Thread(target=srv_b.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv_a.server_address[1]}"

    import titanvault as tv
    tv._kept_blocked = lambda: False
    ok = True

    # T0: probe gate unit behavior
    ok &= check("probe gate: real segment accepted", tv._probe_has_media_stream("/tmp/failpath_seg0.ts"))
    open("/tmp/failpath_dl/html.mp4", "wb").write(HTML_FAKE)
    ok &= check("probe gate: HTML-as-mp4 rejected", not tv._probe_has_media_stream("/tmp/failpath_dl/html.mp4"))

    # T1: HLS stage fails -> chain continues (native yt-dlp + JDownloader tried), honest message
    EDITS.clear()
    jd_calls = {"n": 0}
    orig_jd = tv._do_dl_via_jdownloader
    async def jd_spy(*a, **k):
        jd_calls["n"] += 1
        return await orig_jd(*a, **k)
    tv._do_dl_via_jdownloader = jd_spy
    try:
        asyncio.run(tv._do_dl(base + "/hls404", FakeMsg(), 1))
    except Exception as e:
        EDITS.append("EXC: " + str(e))
    tv._do_dl_via_jdownloader = orig_jd
    ok &= check("HLS fail -> JDownloader attempted", jd_calls["n"] >= 1)
    ok &= check("HLS fail -> honest final message",
                any("Could not download the detected stream" in e for e in EDITS))
    ok &= check("no fabricated 403 message", not any("403 / token expired" in e for e in EDITS))

    # T2: HTML masquerading as mp4 -> rejected before upload, chain continues
    EDITS.clear()
    jd_calls["n"] = 0
    tv._do_dl_via_jdownloader = jd_spy
    try:
        asyncio.run(tv._do_dl(base + "/metafake", FakeMsg(), 1))
    except Exception as e:
        EDITS.append("EXC: " + str(e))
    tv._do_dl_via_jdownloader = orig_jd
    ok &= check("HTML payload rejected with honest message",
                any("did not deliver a playable media file" in e for e in EDITS))
    ok &= check("HTML payload -> upload stage never reached",
                not any("Step 4/4" in e for e in EDITS))
    ok &= check("HTML payload -> chain continued to fallbacks", jd_calls["n"] >= 1)

    # T3: truncated transfer still fails safe on cffi path
    EDITS.clear()
    res = asyncio.run(tv._do_dl_via_cffi(base + "/trunc.mp4", FakeStatus(), [False], 1, 1, headers={}, title="t"))
    ok &= check("truncated download fails safe", res is False
                and any("Download failed" in e for e in EDITS))

    # T4: Origin header handoff - cross-origin media gated on Origin header.
    # A playable file reaching _finish_upload proves the browser-captured Origin
    # survived into the downloader (server B 403s without it).
    EDITS.clear()
    captured = {}
    orig_fu = tv._finish_upload
    async def fu_spy(file_path, title, status_msg, uid, chat_id):
        captured["playable"] = tv._probe_has_media_stream(file_path)
        return True
    tv._finish_upload = fu_spy
    try:
        asyncio.run(tv._do_dl(base + "/xorigin_page", FakeMsg(), 1))
    except Exception as e:
        EDITS.append("EXC: " + str(e))
    finally:
        tv._finish_upload = orig_fu
    ok &= check("Origin-gated HLS downloaded via handed-off headers (playable file reached upload)",
                captured.get("playable") is True)

    srv_a.shutdown(); srv_b.shutdown()
    print("RESULT:", "PASS" if ok else "FAIL", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
