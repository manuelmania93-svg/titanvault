"""N_m3u8DL-RE gets the bot's ffmpeg explicitly (probe AND download). Real Linux exe, local fixture.
ffmpeg is hidden from PATH; it only exists in the bot's script folder (Windows-style layout)."""
import os, sys, asyncio, threading, subprocess, tempfile, shutil, functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
NM_BIN = os.environ.get("NM_BIN", os.path.expanduser("~/nm/N_m3u8DL-RE"))
os.environ["N_M3U8DL_PATH"] = NM_BIN
from harness import tv, FakeMsg
failed = []
def ok(c, n):
    print(("PASS: " if c else "FAIL: ") + n)
    if not c: failed.append(n)

REAL_FF = shutil.which("ffmpeg"); REAL_FP = shutil.which("ffprobe")
FX = tempfile.mkdtemp(prefix="nmff_"); os.makedirs(f"{FX}/w/mux")
subprocess.run([REAL_FF, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=4", "-f", "lavfi", "-i", "sine=f=440:d=4",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                f"{FX}/w/mux/index.m3u8"], check=True)
srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=f"{FX}/w"))
srv.RequestHandlerClass.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}/mux/index.m3u8"

# hide ffmpeg from PATH; put it only in a fake bot script folder
SCRIPT = os.path.join(FX, "script"); os.makedirs(SCRIPT); os.symlink(REAL_FF, os.path.join(SCRIPT, "ffmpeg"))
EMPTY = os.path.join(FX, "empty"); os.makedirs(EMPTY)
os.environ["PATH"] = EMPTY
tv._SCRIPT_DIR = SCRIPT; tv.FFMPEG_PATH = "ffmpeg"; tv.FFPROBE_PATH = REAL_FP   # the bot's own ffprobe call is not under test
ok(shutil.which("ffmpeg") is None, "setup: ffmpeg is NOT on PATH")
ok(tv._nm_find_ffmpeg() == os.path.join(SCRIPT, "ffmpeg"), "bot resolves ffmpeg to an absolute path in its own folder")
ok("--ffmpeg-binary-path" in tv._nm_probe_args("u", "/p", {}, "/x/ffmpeg") and "--ffmpeg-binary-path" not in tv._nm_probe_args("u", "/p", {}), "probe args carry --ffmpeg-binary-path only when resolved")

UP = []
async def fake_finish(path, title, status, uid, chat_id, skip_convert=False):
    UP.append(os.path.basename(path)); return True
tv._finish_upload = fake_finish

async def run():
    UP.clear(); tv._diag_reset(7)
    r = await tv._do_dl_via_nm3u8dl(URL, FakeMsg(), [False], 7, 7, title="t")
    return r, list(tv.dl_diag[7])

async def main():
    # 1) with the fix: probe + download succeed although PATH has no ffmpeg
    r, d = await run()
    ok(r and UP, "ffmpeg only in the bot folder: probe and download succeed, file handed to upload")
    ok(not any("ffmpeg" in x[1] for x in d), "no ffmpeg diagnostic when it was located")
    # 2) control: the OLD behaviour (probe without the flag) really fails on this exe
    orig = tv._nm_probe_args
    tv._nm_probe_args = lambda url, pd, h, ff="": orig(url, pd, h, "")
    r, d = await run()
    tv._nm_probe_args = orig
    ok(not r and any("probe failed" in x[1] for x in d), "control: probe WITHOUT --ffmpeg-binary-path fails when ffmpeg is not on the exe's PATH (the user's symptom)")
    # 3) nothing to find: honest fixed diagnostic, leg still fails cleanly
    os.remove(os.path.join(SCRIPT, "ffmpeg"))
    ok(tv._nm_find_ffmpeg() == "", "no ffmpeg anywhere -> empty")
    r, d = await run()
    ok(not r and any(x[1] == "ffmpeg" and "not located" in x[2] for x in d), "no ffmpeg: fixed 'not located' diagnostic recorded")
    ok(all("http" not in " ".join(x) and FX not in " ".join(x) for x in d), "diagnostics carry no URL or local path")
    os.symlink(REAL_FF, os.path.join(SCRIPT, "ffmpeg"))
asyncio.run(main())
print("\nFAILED: %s" % failed if failed else "\nALL PASS")
sys.exit(1 if failed else 0)
