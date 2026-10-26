"""Bounded request-profile fallback for HLS (desktop UA first, mobile UA after an HTTP 403, and the reverse).
Local fixture server that answers by User-Agent class; real yt-dlp and FFmpeg; _finish_upload stubbed."""
import os, sys, threading, subprocess, tempfile, shutil, time, collections, asyncio
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import tv, FakeMsg
failed = []
def ok(c, n):
    print(("PASS: " if c else "FAIL: ") + n)
    if not c: failed.append(n)
FF = shutil.which("ffmpeg")
FX = tempfile.mkdtemp(prefix="uaf_"); os.makedirs(f"{FX}/w")
subprocess.run([FF, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=6", "-f", "lavfi", "-i", "sine=f=440:d=6",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                f"{FX}/w/index.m3u8"], check=True)
MODE = {"m": "open"}; LOG = []
def cls(ua): return "desktop" if "Windows NT" in ua else "mobile" if "Android" in ua and "Mobile" in ua else "other"
class H(SimpleHTTPRequestHandler):
    def log_message(s, *a): pass
    def translate_path(s, p): return f"{FX}/w" + p.split("?")[0]
    def do_GET(s):
        u = cls(s.headers.get("User-Agent", "")); kind = "seg" if s.path.endswith(".ts") else "pl"
        LOG.append((kind, u)); m = MODE["m"]
        if (m == "block_desktop" and u == "desktop") or (m == "block_mobile" and u == "mobile") or m == "block_all":
            s.send_response(403); s.send_header("Content-Length", "0"); s.end_headers(); return
        if m == "notfound":
            s.send_response(404); s.send_header("Content-Length", "0"); s.end_headers(); return
        super().do_GET()
srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
tv.FFPROBE_PATH = shutil.which("ffprobe")
tv._YDL_RELIABLE = {**tv._YDL_RELIABLE, "retry_sleep_functions": {"http": lambda n: 0, "fragment": lambda n: 0}}   # keep the test fast
up = []
async def fake_finish(path, title, status, uid, chat_id, skip_convert=False):
    up.append(path); return True
tv._finish_upload = fake_finish
async def run(mode, url=None, extra=None, finish_ok=True, uid=7):
    MODE["m"] = mode; LOG.clear(); up.clear(); tv._diag_reset(uid)
    async def ff(path, *a, **k):
        up.append(path); return finish_ok
    tv._finish_upload = ff
    cf = [False]
    r = await tv._do_dl_via_ffmpeg_hls(url or f"http://127.0.0.1:{PORT}/index.m3u8", FakeMsg(), cf, uid, uid, title="t", extra_headers=extra)
    return r, collections.Counter(LOG)
def uas(c): return {u for (_, u) in c}
async def main():
    # 1. normal host: desktop only, one pass, nothing else requested
    r, c = await run("open")
    ok(r and uas(c) == {"desktop"}, "normal host: only the desktop profile is used, no fallback request")
    # 2. desktop refused, mobile works: playlist AND segments carry the mobile profile in the fallback pass
    tv._UA_PREF.clear()
    r, c = await run("block_desktop")
    ok(r and c[("pl", "mobile")] >= 1 and c[("seg", "mobile")] >= 3, f"desktop refused: mobile pass downloads playlist and segments ({dict(c)})")
    ok(c[("seg", "desktop")] == 0 and c[("pl", "desktop")] <= 4, "desktop refusal stops quickly (no segment requests, few playlist requests)")
    ok(tv._UA_PREF.get(f"127.0.0.1:{PORT}") == "mobile", "worked profile remembered for the host")
    ok(len(up) == 1 and os.path.exists(up[0]) and tv._probe_content_duration(up[0]) > 5, "one complete file handed to upload")
    # 3. next link from that host starts with mobile
    r, c = await run("block_desktop")
    ok(r and c[("pl", "desktop")] == 0, "remembered host starts with the mobile profile (no desktop request)")
    # 4. and the reverse: mobile now refused -> desktop is the fallback
    r, c = await run("block_mobile")
    ok(r and c[("seg", "desktop")] >= 3 and tv._UA_PREF.get(f"127.0.0.1:{PORT}") == "desktop", f"mobile refused: desktop fallback works and is remembered ({dict(c)})")
    # 5. desktop works, no fallback ever
    tv._UA_PREF.clear()
    r, c = await run("block_mobile")
    ok(r and uas(c) == {"desktop"}, "mobile-only refusal never matters when desktop is first and works")
    # 6. both refused: bounded
    tv._UA_PREF.clear()
    t0 = time.time(); r, c = await run("block_all")
    n = sum(c.values())
    ok(not r and n < 60 and not up, f"both refused: ends with failure, bounded ({n} requests, {time.time()-t0:.0f}s)")
    ok(any(e[0] == "Request profile" for e in tv.dl_diag.get(7, [])), "diagnostics show the profile retry (no URLs or tokens)")
    # 7. a non-refusal failure does not trigger the fallback
    r, c = await run("notfound")
    ok(not r and "mobile" not in uas(c), "HTTP 404 is not a refusal: no fallback pass")
    # 8. sniffer-captured User-Agent is used as is
    r, c = await run("block_desktop", extra={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Captured/1"})
    ok(not r and uas(c) == {"desktop"}, "captured browser User-Agent: no profile fallback")
    # 9. a file that reached upload is never downloaded again
    tv._UA_PREF.clear()
    r, c = await run("open", finish_ok=False)
    ok(not r and c[("pl", "mobile")] == 0 and len(up) == 1, "upload failure after a good download: no second pass")
    # 10. N_m3u8DL-RE leg receives the pass's User-Agent
    seen = []
    tv._nm_find_binary = lambda: "/x/nm"
    orig_probe = tv._nm_probe_args
    def spy(url, d, headers, ff=""):
        seen.append(headers.get("User-Agent")); return orig_probe(url, d, headers, ff)
    tv._nm_probe_args = spy
    async def fake_exec(*a, **k): return (tv._NM_STOPPED_EARLY, "", False)
    tv._nm_exec = fake_exec
    st = {}
    await tv._do_dl_via_nm3u8dl("http://127.0.0.1:1/x.m3u8", FakeMsg(), [False], 7, 7, ua=tv._MOBILE_UA, state=st)
    await tv._do_dl_via_nm3u8dl("http://127.0.0.1:1/x.m3u8", FakeMsg(), [False], 7, 7)
    ok(seen == [tv._MOBILE_UA, tv._STANDARD_UA] and st.get("refused"), "N_m3u8DL-RE gets the profile's User-Agent (default unchanged) and reports a refusal")
    # 11. FFmpeg leg alone (yt-dlp made to fail with a non-403 error): refusal is read from FFmpeg, mobile profile carried by FFmpeg
    tv._nm_find_binary = lambda: ""
    real_ydl = tv.yt_dlp.YoutubeDL
    class Boom:
        def __init__(s, *a, **k): pass
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def download(s, *a): raise RuntimeError("engine unavailable")
    tv.yt_dlp.YoutubeDL = Boom
    tv._UA_PREF.clear()
    r, c = await run("block_desktop")
    tv.yt_dlp.YoutubeDL = real_ydl
    ok(r and c[("seg", "mobile")] >= 1 and c[("seg", "desktop")] == 0 and len(up) == 1, f"FFmpeg leg: 403 detected from FFmpeg, fallback pass runs FFmpeg with the mobile profile ({dict(c)})")
asyncio.run(main())
sys.exit(1 if failed else 0)
