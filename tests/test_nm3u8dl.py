"""N_m3u8DL-RE leg (experimental): policy picker, argument building, real-binary runs against local
HLS fixtures, request headers, error diagnostics, cancel/cleanup. Local fixtures only, no Telegram.
Needs ffmpeg and the official Linux build of N_m3u8DL-RE (set NM_BIN, default ~/nm/N_m3u8DL-RE)."""
import os, sys, asyncio, threading, subprocess, tempfile, shutil, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
NM_BIN = os.environ.get("NM_BIN", os.path.expanduser("~/nm/N_m3u8DL-RE"))
os.environ["N_M3U8DL_PATH"] = NM_BIN
import harness
from harness import tv, FakeMsg

failed = []
def ok(cond, name):
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond: failed.append(name)

# ---------- fixtures ----------
FX = tempfile.mkdtemp(prefix="nmfx_")
def ff(*a):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *a], check=True)
os.makedirs(f"{FX}/mux"); os.makedirs(f"{FX}/sep"); os.makedirs(f"{FX}/hi")
ff("-f","lavfi","-i","testsrc2=s=1920x1080:r=25:d=4","-f","lavfi","-i","sine=f=440:d=4","-filter_complex","[0:v]split=3[a][b][c];[a]scale=640:360[v0];[b]scale=1280:720[v1];[c]copy[v2]",
   "-map","[v0]","-map","1:a","-map","[v1]","-map","1:a","-map","[v2]","-map","1:a","-c:v","libx264","-preset","ultrafast","-g","50","-c:a","aac",
   "-b:v:0","400k","-b:v:1","1200k","-b:v:2","3000k","-f","hls","-hls_time","2","-hls_playlist_type","vod","-master_pl_name","master.m3u8",
   "-var_stream_map","v:0,a:0 v:1,a:1 v:2,a:2",f"{FX}/mux/v%v.m3u8")
ff("-f","lavfi","-i","testsrc2=s=2560x1440:r=25:d=4","-f","lavfi","-i","sine=f=660:d=4","-filter_complex","[0:v]split=2[a][b];[a]scale=1280:720[v0];[b]copy[v1]",
   "-map","[v0]","-map","[v1]","-map","1:a","-c:v","libx264","-preset","ultrafast","-g","50","-c:a","aac","-b:v:0","1200k","-b:v:1","6000k",
   "-f","hls","-hls_time","2","-hls_playlist_type","vod","-master_pl_name","master.m3u8","-var_stream_map","v:0,agroup:aud v:1,agroup:aud a:0,agroup:aud,default:yes,language:eng",f"{FX}/sep/s%v.m3u8")
ff("-f","lavfi","-i","testsrc2=s=3840x2160:r=25:d=2","-f","lavfi","-i","sine=f=550:d=2","-filter_complex","[0:v]split=2[a][b];[a]scale=2560:1440[v0];[b]copy[v1]",
   "-map","[v0]","-map","[v1]","-map","1:a","-c:v","libx264","-preset","ultrafast","-g","50","-c:a","aac","-b:v:0","3000k","-b:v:1","9000k",
   "-f","hls","-hls_time","2","-hls_playlist_type","vod","-master_pl_name","master.m3u8","-var_stream_map","v:0,agroup:aud v:1,agroup:aud a:0,agroup:aud,default:yes,language:eng",f"{FX}/hi/h%v.m3u8")

os.makedirs(f"{FX}/dash"); os.makedirs(f"{FX}/same"); os.makedirs(f"{FX}/nosize")
ff("-f","lavfi","-i","testsrc2=s=1920x1080:r=25:d=4","-f","lavfi","-i","sine=f=440:d=4","-filter_complex","[0:v]split=3[a][b][c];[a]scale=640:360[v0];[b]scale=1280:720[v1];[c]copy[v2]",
   "-map","[v0]","-map","[v1]","-map","[v2]","-map","1:a","-c:v","libx264","-preset","ultrafast","-g","50","-c:a","aac","-b:v:0","400k","-b:v:1","1200k","-b:v:2","3000k",
   "-f","dash","-seg_duration","2","-adaptation_sets","id=0,streams=v id=1,streams=a",f"{FX}/dash/manifest.mpd")
# same-size variants (two bitrates at 1280x720) + two audio renditions
ff("-f","lavfi","-i","testsrc2=s=1280x720:r=25:d=4","-f","lavfi","-i","sine=f=300:d=4","-f","lavfi","-i","sine=f=900:d=4","-filter_complex","[0:v]split=2[a][b]",
   "-map","[a]","-map","[b]","-map","1:a","-map","2:a","-c:v","libx264","-preset","ultrafast","-g","50","-c:a","aac","-b:v:0","500k","-b:v:1","2500k",
   "-f","hls","-hls_time","2","-hls_playlist_type","vod","-master_pl_name","master.m3u8",
   "-var_stream_map","v:0,agroup:aud v:1,agroup:aud a:0,agroup:aud,default:yes,language:eng a:1,agroup:aud,language:deu",f"{FX}/same/s%v.m3u8")
# master without RESOLUTION: strip it from the muxed fixture
import re as _re
m = open(f"{FX}/mux/master.m3u8").read()
open(f"{FX}/nosize/master.m3u8","w").write(_re.sub(r",RESOLUTION=\d+x\d+","",m))
for f in os.listdir(f"{FX}/mux"):
    if f != "master.m3u8": shutil.copy(f"{FX}/mux/{f}", f"{FX}/nosize/{f}")
SEEN = []   # (path, headers)
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *a): pass
    def handle(self):
        try: super().handle()
        except (BrokenPipeError, ConnectionResetError): pass
    def do_GET(self):
        SEEN.append((self.path, dict(self.headers)))
        path = self.path.split("?")[0]
        if path.startswith("/deny/"):
            if path.endswith("master.m3u8") or path.endswith(".m3u8"):
                return self._r(403)
        if path.startswith("/segdeny/"):
            real = FX + "/mux/" + path[len("/segdeny/"):]
            if path.endswith(".ts"): return self._r(403)
            return self._file(real)
        if path.startswith("/slow/") and path.endswith(".ts"):
            time.sleep(3)
            return self._file(FX + "/mux/" + path[len("/slow/"):])
        if path.startswith("/slow/"):
            return self._file(FX + "/mux/" + path[len("/slow/"):])
        if path.startswith("/need/"):   # requires the exact headers
            h = {k.lower(): v for k, v in self.headers.items()}
            if h.get("referer") != "http://ref.example/p" or h.get("x-origin-check", "") != "":
                return self._r(403)
            return self._file(FX + "/mux/" + path[len("/need/"):])
        f = FX + path
        if os.path.isfile(f): return self._file(f)
        return self._r(404)
    def _file(self, f):
        b = open(f, "rb").read()
        self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _r(self, code):
        self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()
srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

# ---------- units ----------
M = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360\na.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080\nb.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=3840x2160\nc.m3u8\n#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=9,RESOLUTION=9999x9999,URI=\"i.m3u8\"\n"
vs = tv._hls_master_variants(M)
ok(vs == [(640,360,500000),(1920,1080,3000000),(3840,2160,9000000)], "variants parsed, I-frame lists ignored")
ok(tv._pick_policy_variant(vs) == (1920,1080), "policy: highest <=1080 wins over 2160")
ok(tv._pick_policy_variant([(2560,1440,3),(3840,2160,9)]) == (2560,1440), "policy: only higher -> closest higher")
ok(tv._pick_policy_variant([(1280,720,1),(1920,1080,1)]) == (1920,1080), "policy: 1080 is allowed")
ok(tv._pick_policy_variant([(1920,1080,1),(1920,1080,5)]) == (1920,1080), "policy: equal height ok")
ok(tv._pick_policy_variant([]) is None and tv._hls_master_variants("#EXTM3U\n#EXTINF:2,\nx.ts\n") == [], "media playlist/no master -> no pin")
a = tv._nm_build_args("http://h/x.m3u8", "/tmp/j", {"Referer": "http://r/\r\nEvil: 1", "Cookie": ""}, (1280,720), "")
ok("-sv" in a and 'res="^1280x720$":for=best' in a and "-sa" in a and "-ds" in a, "args always carry video/audio/subtitle selectors")
ok(not any("\n" in x or "\r" in x for x in a) and "Referer: http://r/Evil: 1" in a and "Cookie: " not in " ".join(a), "args: CR/LF stripped, empty header dropped")
ok(tv._nm_classify("Response status code does not indicate success: 403 (Forbidden). (3/10)", False) == ("playlist fetch","HTTP 403"), "classify playlist 403")
ok(tv._nm_classify("Selected streams: ... status code does not indicate success: 403", True) == ("segment fetch","HTTP 403"), "classify segment 403")
leak = tv._nm_classify("Loading URL: http://x/secret-token-abc.m3u8 refused", False)
ok("secret" not in " ".join(leak), "classify never echoes URLs")
ok(tv._nm_find_binary() == NM_BIN, "binary lookup honours N_M3U8DL_PATH")
ok(tv._nm_policy(M)[0] == "pin", "policy: master with sizes -> pin")
ok(tv._nm_policy(M.replace(",RESOLUTION=640x360", ""))[0] == "unknown", "policy: any variant without RESOLUTION -> unknown (skip)")
ok(tv._nm_policy("#EXTM3U\n#EXTINF:2,\nx.ts\n") == ("single", None), "policy: plain media playlist -> single stream, no choice")
ok(tv._nm_policy("<html>nope</html>")[0] == "unknown" and tv._nm_policy("")[0] == "unknown", "policy: unrecognised manifest -> unknown")
ok(tv._nm_policy('<MPD><Period><AdaptationSet mimeType="video/mp4"><Representation id="a" bandwidth="5"/></AdaptationSet></Period></MPD>')[0] == "unknown", "policy: DASH without width/height -> unknown")
_mpd = '<MPD><Period><AdaptationSet contentType="video" mimeType="video/mp4"><Representation id="0" bandwidth="1" width="1280" height="720"/><Representation id="1" bandwidth="2" width="2560" height="1440"/></AdaptationSet><AdaptationSet mimeType="audio/mp4"><Representation id="2" bandwidth="9" width="9999" height="9999"/></AdaptationSet></Period></MPD>'
ok(tv._nm_policy(_mpd) == ("pin", (1280, 720)), "policy: DASH picks 720 (<=1080), audio set ignored")

# discovery: a win-x64 folder in Downloads is found, the win-arm64 one never is
_fh = tempfile.mkdtemp(prefix="nmhome_")
for d in ("N_m3u8DL-RE_v0.6.0-beta_win-arm64_20260629", "N_m3u8DL-RE_v0.6.0-beta_win-x64_20260629"):
    os.makedirs(f"{_fh}/Downloads/{d}"); open(f"{_fh}/Downloads/{d}/N_m3u8DL-RE", "w").write("x")
_old = (os.environ.get("HOME"), tv.N_M3U8DL_PATH)
os.environ["HOME"] = _fh; tv.N_M3U8DL_PATH = ""; tv._NM_BIN_CACHE[:] = [None, False]
_found = tv._nm_find_binary()
ok("win-x64" in _found and "arm64" not in _found, "discovery: picks the win-x64 folder in Downloads, not arm64")
os.environ["HOME"] = _old[0]; tv.N_M3U8DL_PATH = NM_BIN; tv._NM_BIN_CACHE[:] = [None, False]

# ---------- real binary, real fixtures ----------
UPLOADED = []
async def fake_finish(path, title, status, uid, chat_id, skip_convert=False):
    kinds = tv._probe_stream_kinds(path)
    r = subprocess.run([tv.FFPROBE_PATH,"-v","error","-select_streams","v:0","-show_entries","stream=width,height","-of","csv=p=0",path],capture_output=True,text=True)
    UPLOADED.append((os.path.basename(path), kinds, r.stdout.strip(), title))
    return True
tv._finish_upload = fake_finish

async def run(url, **kw):
    UPLOADED.clear(); tv._diag_reset(7)
    st = FakeMsg()
    r = await tv._do_dl_via_nm3u8dl(url, st, [False], 7, 7, **kw)
    return r, (UPLOADED[0] if UPLOADED else None)

async def main():
    r, up = await run(f"{BASE}/mux/master.m3u8", title="t1", referer="http://ref.example/p")
    ok(r and up and up[2] == "1920,1080" and "audio" in up[1], "muxed master: 1080p picked, video+audio in one mp4")
    ok(len(os.listdir(tv.ARIA2C_DOWNLOAD_DIR)) == 0 or not [d for d in os.listdir(tv.ARIA2C_DOWNLOAD_DIR) if d.startswith("nm_")], "job dir removed after success")
    r, up = await run(f"{BASE}/sep/master.m3u8", title="t2")
    ok(r and up and up[2] == "1280,720" and "audio" in up[1] and "video" in up[1], "separate audio group: 720p (<=1080 preferred over 1440) merged with audio")
    r, up = await run(f"{BASE}/hi/master.m3u8", title="t3")
    ok(r and up and up[2] == "2560,1440" and "audio" in up[1], "only >1080 sources: closest higher (1440) chosen, audio merged")
    r, up = await run(f"{BASE}/dash/manifest.mpd", title="d1")
    ok(r and up and up[2] == "1920,1080" and "audio" in up[1] and "video" in up[1], "DASH fixture: 1080p variant pinned, video+audio merged")
    r, up = await run(f"{BASE}/same/master.m3u8", title="s1")
    ok(r and up and up[2] == "1280,720" and up[1].count("audio") == 1, "same-size variants + two audio renditions: one 720p video, exactly one audio stream")
    r, up = await run(f"{BASE}/nosize/master.m3u8", title="n1")
    ok(not r and not up and "resolution policy" in tv._diag_summary(7), "master without RESOLUTION: leg skipped with diagnostic, nothing downloaded")
    ok(not [x for x in os.listdir(tv.ARIA2C_DOWNLOAD_DIR) if x.startswith("nm_")], "skipped run created no job dir")
    # header fidelity: the origin only answers when Referer matches
    SEEN.clear()
    r, up = await run(f"{BASE}/need/master.m3u8", title="t4", referer="http://ref.example/p", extra_headers={"Origin": "http://ref.example", "X-Evil": "1"})
    ok(r, "request context: custom Referer reached playlist and segments (origin enforces it)")
    seg_reqs = [h for p, h in SEEN if p.endswith(".ts")]
    hl = [{k.lower(): v for k, v in h.items()} for h in seg_reqs]
    ok(hl and all(h.get("referer") == "http://ref.example/p" and h.get("origin") == "http://ref.example" and "x-evil" not in h for h in hl), "segments carried Referer+Origin; non-allowlisted header dropped")
    ok(all("TestUA" not in h.get("user-agent","") for h in hl) and any("Mozilla" in h.get("user-agent","") for h in hl), "standard UA sent")
    # failures: diagnostics are fixed-vocabulary, no URL, no leftovers
    r, up = await run(f"{BASE}/deny/master.m3u8?token=SECRETX", title="t5")
    d = tv._diag_summary(7)
    ok(not r and "N_m3u8DL-RE" in d and "HTTP 403" in d and "SECRETX" not in d and "127.0.0.1" not in d, "playlist 403: diagnostic stage/status only, no URL/token")
    r, up = await run(f"{BASE}/segdeny/master.m3u8?token=SECRETY", title="t6")
    d = tv._diag_summary(7)
    ok(not r and "segment fetch" in d and "HTTP 403" in d and "SECRETY" not in d, "segment 403 classified as segment fetch")
    ok(not [x for x in os.listdir(tv.ARIA2C_DOWNLOAD_DIR) if x.startswith("nm_")], "failed runs leave no job dirs")
    # cancel mid-download: process killed, dir cleaned, cancel propagates
    UPLOADED.clear(); tv._diag_reset(7)
    flag = [False]
    task = asyncio.ensure_future(tv._do_dl_via_nm3u8dl(f"{BASE}/slow/master.m3u8", FakeMsg(), flag, 7, 7, title="t7"))
    await asyncio.sleep(4); flag[0] = True
    try:
        await asyncio.wait_for(task, 15); cancelled = False
    except Exception as e:
        cancelled = "__CANCELLED__" in str(e)
    await asyncio.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "N_m3u8DL-RE.*slow/master"], capture_output=True, text=True).stdout.strip()
    ok(cancelled and not left, "cancel: raises cancel, binary process gone")
    ok(not [x for x in os.listdir(tv.ARIA2C_DOWNLOAD_DIR) if x.startswith("nm_")] and not UPLOADED, "cancel: job dir removed, nothing uploaded")
    # asyncio task cancel (DL_CANCEL button) also kills it
    task = asyncio.ensure_future(tv._do_dl_via_nm3u8dl(f"{BASE}/slow/master.m3u8", FakeMsg(), [False], 7, 7, title="t8"))
    await asyncio.sleep(4); task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    await asyncio.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "N_m3u8DL-RE.*slow/master"], capture_output=True, text=True).stdout.strip()
    ok(not left and not [x for x in os.listdir(tv.ARIA2C_DOWNLOAD_DIR) if x.startswith("nm_")], "task.cancel(): binary killed, dir removed")
    # integration into the HLS leg order: yt-dlp and ffmpeg legs fail on 403, N_m3u8DL-RE leg is the last one tried
    tv._diag_reset(7); UPLOADED.clear()
    ok_ = await tv._do_dl_via_ffmpeg_hls(f"{BASE}/deny/master.m3u8", FakeMsg(), [False], 7, 7, title="t9")
    legs = [e[0] for e in tv.dl_diag[7]]
    ok(not ok_ and legs[-1] == "HLS via N_m3u8DL-RE" and "HLS via yt-dlp" in legs, "HLS leg order: yt-dlp, ffmpeg, then N_m3u8DL-RE; all failures reported")
    # fallback actually rescues: yt-dlp + ffmpeg legs disabled by breaking their binaries
    real_ydl = tv.yt_dlp.YoutubeDL
    class Boom:
        def __init__(self,*a,**k): raise Exception("forced")
    tv.yt_dlp.YoutubeDL = Boom
    old_ff = tv.FFMPEG_PATH
    tv._diag_reset(7); UPLOADED.clear()
    # keep ffmpeg for N_m3u8DL-RE merging by pointing only the bot's own ffmpeg-leg call at a failing wrapper
    wrapper = os.path.join(FX, "badff"); open(wrapper, "w").write("#!/bin/sh\nexit 1\n"); os.chmod(wrapper, 0o755)
    nm_ff = shutil.which("ffmpeg")
    tv.FFMPEG_PATH = wrapper
    orig_which = shutil.which
    shutil.which = lambda n, *a, **k: nm_ff if n == wrapper else orig_which(n, *a, **k)
    # wrapper path is absolute so which(wrapper) returns it; map it to the real ffmpeg for the nm leg
    ok_ = await tv._do_dl_via_ffmpeg_hls(f"{BASE}/mux/master.m3u8", FakeMsg(), [False], 7, 7, title="t10")
    shutil.which = orig_which; tv.yt_dlp.YoutubeDL = real_ydl; tv.FFMPEG_PATH = old_ff
    ok(ok_ and UPLOADED and UPLOADED[0][2] == "1920,1080", "fallback rescues after yt-dlp and ffmpeg legs fail")
    # binary absent / off: leg is skipped silently
    tv._NM_BIN_CACHE[:] = [None, False]; tv.N_M3U8DL_PATH = "/nonexistent/x"
    old_home = os.environ.get("HOME"); os.environ["HOME"] = "/nonexistent"
    ok(tv._nm_find_binary() == "" and not await tv._do_dl_via_nm3u8dl(f"{BASE}/mux/master.m3u8", FakeMsg(), [False], 7, 7), "no binary: leg returns False without error")
    os.environ["HOME"] = old_home or ""
asyncio.run(main())
print("\nFAILED: %s" % failed if failed else "\nALL PASS")
sys.exit(1 if failed else 0)
