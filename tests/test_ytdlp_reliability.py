"""yt-dlp HLS with the bot's reliability options (tv._YDL_RELIABLE) against a localhost fixture, plus the >1080p resize step."""
import os, sys, asyncio, threading, subprocess, tempfile, shutil, time, collections
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import tv, FakeMsg
import yt_dlp
failed = []
def ok(c, n):
    print(("PASS: " if c else "FAIL: ") + n)
    if not c: failed.append(n)
FF, FP = shutil.which("ffmpeg"), shutil.which("ffprobe")
tv.FFPROBE_PATH = FP
FX = tempfile.mkdtemp(prefix="ytr_"); os.makedirs(f"{FX}/w")
subprocess.run([FF, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=6", "-f", "lavfi", "-i", "sine=f=440:d=6",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                f"{FX}/w/index.m3u8"], check=True)
MODE = {"m": "ok", "fails": 0}; CNT = collections.Counter()
class H(SimpleHTTPRequestHandler):
    def log_message(s, *a): pass
    def translate_path(s, p): return f"{FX}/w" + p.split("?")[0]
    def do_GET(s):
        if s.path.endswith(".ts"):
            CNT["seg"] += 1
            m = MODE["m"]
            if (m in ("503", "429") and CNT["seg"] <= MODE["fails"]) or m == "403":
                s.send_response(int(m)); s.send_header("Content-Length", "0"); s.end_headers(); return
        super().do_GET()
srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
U = f"http://127.0.0.1:{srv.server_address[1]}/index.m3u8"
def dur(p):
    r = subprocess.run([FP, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p], capture_output=True, text=True)
    try: return float(r.stdout.strip())
    except ValueError: return 0.0
def run(mode, fails=0):
    MODE.update(m=mode, fails=fails); CNT.clear()
    d = tempfile.mkdtemp(dir=FX)
    o = {"quiet": True, "no_warnings": True, "noprogress": True, "outtmpl": d + "/o.%(ext)s", "concurrent_fragment_downloads": 4, **tv._YDL_RELIABLE}
    t = time.time(); err = None
    try:
        with yt_dlp.YoutubeDL(o) as y: y.download([U])
    except Exception as e: err = type(e).__name__
    f = os.path.join(d, "o.mp4")
    return err, (dur(f) if os.path.exists(f) else 0.0), CNT["seg"], time.time() - t
e, d, n, t = run("ok");        ok(e is None and abs(d - 6) < 0.5, f"complete download unchanged ({d:.1f}s)")
e, d, n, t = run("503", 1);    ok(e is None and abs(d - 6) < 0.5 and n > 3, f"one 503 retried, file complete ({d:.1f}s, {n} segment requests)")
e, d, n, t = run("503", 2);    ok(e is None and abs(d - 6) < 0.5, f"two 503s retried, file complete ({d:.1f}s)")
e, d, n, t = run("429", 1);    ok(e is None and abs(d - 6) < 0.5, f"one 429 retried, file complete ({d:.1f}s)")
e, d, n, t = run("403");       ok(e == "DownloadError" and d == 0.0 and n < 30 and t < 60, f"persistent 403: stops with an error, no short file ({n} requests, {t:.0f}s)")
# the old behaviour for comparison: no retries and fragment skipping -> short file with success
MODE.update(m="503", fails=1); CNT.clear(); d0 = tempfile.mkdtemp(dir=FX)
with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True, "outtmpl": d0 + "/o.%(ext)s", "concurrent_fragment_downloads": 4}) as y: y.download([U])
ok(dur(d0 + "/o.mp4") < 5, "setup: without the options a failed fragment gives a short file and no error")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "titanvault.py")).read()
ok(src.count("_YDL_RELIABLE") >= 3, "bot applies the options to the yt-dlp HLS leg and the main yt-dlp path")
# >1080p resize step (the one _finish_upload calls) on a small 4K fixture
big = f"{FX}/big.mp4"
subprocess.run([FF, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=3840x2160:r=10:d=1", "-f", "lavfi", "-i", "sine=f=440:d=1", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", big], check=True)
tv.FFMPEG_PATH = FF
async def rz():
    _, w, h = await asyncio.get_running_loop().run_in_executor(None, tv.get_video_info, big)
    out = await tv.apply_convert(big, {"scale": 1080}, FakeMsg())
    return h, await asyncio.get_running_loop().run_in_executor(None, tv.get_video_info, out)
h, (du, w2, h2) = asyncio.run(rz())
ok(h == 2160 and h2 == 1080 and w2 == 1920, f"2160p source resized to {w2}x{h2}, aspect kept")
print("FAILED:" if failed else "ALL PASS", failed); sys.exit(1 if failed else 0)
