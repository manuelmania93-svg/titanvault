"""Completeness check on FFmpeg HLS output + early stop of N_m3u8DL-RE after repeated refusals. Linux, local fixtures only."""
import os, sys, asyncio, threading, subprocess, tempfile, shutil, time, functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
NM_BIN = os.environ.get("NM_BIN", os.path.expanduser("~/nm/N_m3u8DL-RE"))
os.environ["N_M3U8DL_PATH"] = NM_BIN
from harness import tv
failed = []
def ok(c, n):
    print(("PASS: " if c else "FAIL: ") + n)
    if not c: failed.append(n)
FF = shutil.which("ffmpeg"); tv.FFPROBE_PATH = shutil.which("ffprobe")
FX = tempfile.mkdtemp(prefix="cmp_"); os.makedirs(f"{FX}/w/mux")
subprocess.run([FF, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=12", "-f", "lavfi", "-i", "sine=f=440:d=12",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                f"{FX}/w/mux/index.m3u8"], check=True)
pl = open(f"{FX}/w/mux/index.m3u8").read()
open(f"{FX}/w/mux/live.m3u8", "w").write(pl.replace("#EXT-X-ENDLIST\n", "").replace("#EXT-X-PLAYLIST-TYPE:VOD\n", ""))
MODE = {"m": "ok"}
class H(SimpleHTTPRequestHandler):
    def log_message(s, *a): pass
    def translate_path(s, p): return f"{FX}/w" + p.split("?")[0]
    def do_GET(s):
        if MODE["m"] == "seg503" and s.path.endswith("2.ts"):
            s.send_response(503); s.end_headers(); return
        if MODE["m"] == "refuse" and s.path.endswith(".m3u8"):
            s.send_response(403); s.end_headers(); return
        super().do_GET()
srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
B = f"http://127.0.0.1:{srv.server_address[1]}/mux/"

def ffrun(url, out):
    r = subprocess.run([FF, "-y", "-err_detect", "ignore_err", "-i", url] + (["-t", "5"] if "live" in url else []) + ["-c", "copy", out], capture_output=True, timeout=60)
    return r.returncode, r.stderr.decode(errors="replace")

MODE["m"] = "ok"; rc, err = ffrun(B + "index.m3u8", f"{FX}/a.mp4")
ok(abs(tv._ff_input_duration(err) - 12) < 1, "input duration read from ffmpeg stderr (VOD)")
ok(not tv._ff_looks_truncated(err, f"{FX}/a.mp4"), "complete download is NOT flagged")
MODE["m"] = "seg503"; rc, err = ffrun(B + "index.m3u8", f"{FX}/b.mp4")
ok(rc == 0 and os.path.exists(f"{FX}/b.mp4"), "setup: ffmpeg exits 0 despite a failed segment")
ok(tv._ff_looks_truncated(err, f"{FX}/b.mp4"), "silently truncated output IS flagged")
MODE["m"] = "ok"; rc, err = ffrun(B + "live.m3u8", f"{FX}/c.mp4")
ok(not tv._ff_looks_truncated(err, f"{FX}/c.mp4"), "playlist without ENDLIST: no verdict (not flagged)")
ok(not tv._ff_looks_truncated("Duration: N/A", f"{FX}/a.mp4"), "N/A duration: no verdict")
ok(not tv._ff_looks_truncated("", f"{FX}/missing.mp4"), "empty stderr / missing file: no verdict")
ok(tv._ff_input_duration("Input #0, hls\n  Duration: 00:01:30.50, start") == 90.5, "duration parse hh:mm:ss.xx")

async def nm(stop):
    MODE["m"] = "refuse"; t = time.time()
    rc, tail, to = await tv._nm_exec(NM_BIN, [B + "index.m3u8", "--save-dir", f"{FX}/nm", "--tmp-dir", f"{FX}/nm/t", "--skip-download", "--no-log", "-sv", "best", "-sa", "best", "-ds", "for=all"],
                                     FX, [False], timeout=60, stop_re=tv._NM_REFUSAL_RE if stop else None)
    return rc, time.time() - t
rc, dt = asyncio.run(nm(True))
ok(rc == tv._NM_STOPPED_EARLY and dt < 8, f"exe stopped after 2nd refusal (rc={rc}, {dt:.1f}s)")
rc, dt = asyncio.run(nm(False))
ok(rc != tv._NM_STOPPED_EARLY and dt > 10, f"without stop_re the exe repeats ~10x ({dt:.1f}s)")
print("FAILED:" if failed else "ALL PASS", failed)
sys.exit(1 if failed else 0)
