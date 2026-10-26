"""Rate limits: Telegram flood wait (pause + resume, no duplicate send) and source HTTP 429 (cooldown, full Retry-After,
bounded retries, other hosts continue, Cancel works). Local fixture server and stubs; no real hosts, no real Telegram."""
import os, sys, threading, subprocess, tempfile, shutil, time, asyncio, types, collections
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import tv, FakeMsg
from telethon.errors import FloodWaitError
tv.init_db()
failed = []
def ok(c, n):
    print(("PASS: " if c else "FAIL: ") + n)
    if not c: failed.append(n)
class Msg(FakeMsg):
    chat_id = 7
    def __init__(s): super().__init__(); s.replies = []
    async def edit_text(s, text, **kw): s.edits.append(text)
    async def reply_text(s, text, **kw): s.replies.append(text); return Msg()
    def get_bot(s): return None
def flood(sec):
    e = FloodWaitError.__new__(FloodWaitError); e.seconds = sec; e.args = (f"flood {sec}",); return e

# ---------- A. retry-after parsing ----------
class Resp: headers = {"Retry-After": "37"}
class E1(Exception): response = Resp()
ok(tv._retry_after_from(E1("x")) == 37, "Retry-After seconds read from exception.response.headers")
try:
    try: raise E1("inner")
    except E1 as i: raise RuntimeError("outer") from i
except RuntimeError as o: ok(tv._retry_after_from(o) == 37, "Retry-After found through the cause chain")
class E2(Exception): headers = {"Retry-After": "Wed, 21 Oct 2037 07:28:00 GMT"}
ok((tv._retry_after_from(E2()) or 0) > 3600, "Retry-After HTTP-date parsed")
ok(tv._retry_after_from(Exception("none")) is None, "no header -> None")

# ---------- B. Telegram flood wait in the final send ----------
tv._probe_has_media_stream = lambda p: True
sends = []; script = []
class TL:
    async def send_file(self, *a, **k):
        act = script.pop(0) if script else "ok"
        if isinstance(act, int): raise flood(act)
        sends.append(1); return types.SimpleNamespace(id=len(sends))
    async def forward_messages(self, *a, **k): pass
tv.tgclient = TL()
async def quick(client, path, progress_callback=None): return "INPUT"
tv.fast_upload = quick
def mkfile(n):
    f = os.path.join(tv.ARIA2C_DOWNLOAD_DIR, n); open(f, "wb").write(b"x" * 3000); return f
sleeps = []
real_sleep = asyncio.sleep
async def fast_sleep(s, *a, **k): sleeps.append(s); await real_sleep(0)
async def partB():
    tv.asyncio.sleep = fast_sleep
    try:
        sends.clear(); script[:] = [30, 45]; f = mkfile("f1.bin"); m = Msg()
        r = await tv._finish_upload(f, "T1", m, 7, 7)
        ok(r and len(sends) == 1 and not os.path.exists(f), "two flood waits then success: exactly one send, file removed after the confirmed upload")
        ok(sum(1 for x in sleeps if x == 5) >= 6 and any("kept and sends automatically" in e for e in m.edits), "flood wait is shown and waited out (countdown), queue note included")
        ok(tv._user_flood_until[0] > time.time() + 40, "flood end time recorded for the worker")
        # many floods: bounded, file kept, no send
        sends.clear(); script[:] = [1] * 30; f = mkfile("f2.bin"); m = Msg(); tv._user_flood_until[0] = 0
        r = await tv._finish_upload(f, "T2", m, 7, 7)
        ok(not r and not sends and os.path.exists(f) and any("persistent" in e for e in m.edits), "more than the allowed flood waits: stops, file kept, nothing sent")
    finally:
        tv.asyncio.sleep = real_sleep
    tv._user_flood_until[0] = 0

# ---------- C. worker: Telegram wait pauses the queue ----------
CALLS = []; EV = {}
async def fake_dl_ok(url, reply_msg, uid):
    CALLS.append((url, time.time())); tv._upload_count[uid] = tv._upload_count.get(uid, 0) + 1
def reset():
    for d in (tv.link_queues, tv.link_workers, tv.pending_links, tv.dl_tasks, tv.dl_cancel_flags, tv._host_cooldown, tv._rl_tries): d.clear()
    CALLS.clear()
async def start(urls, uid=7):
    q = tv.link_queues.setdefault(uid, asyncio.Queue()); pend = tv.pending_links.setdefault(uid, [])
    for u in urls: pend.append(u); q.put_nowait(u)
    m = Msg(); tv.link_workers[uid] = asyncio.create_task(tv._link_worker(uid, m)); return m
async def partC():
    tv._do_dl = fake_dl_ok; reset()
    tv._user_flood_until[0] = time.time() + 1.5
    t0 = time.time(); m = await start(["http://a.test/1", "http://a.test/2"])
    await asyncio.wait_for(tv.link_workers[7], 10)
    ok(CALLS and CALLS[0][1] - t0 >= 1.4 and len(CALLS) == 2, f"queue waits for the Telegram wait, then both links run ({[round(c[1]-t0,1) for c in CALLS]})")
    ok(any("Telegram upload limit active" in r and "Nothing is lost" in r for r in m.replies), "user told about the pause and the waiting count")
    # Cancel during the pause clears the queue and the worker ends
    reset(); tv._user_flood_until[0] = time.time() + 4
    m = await start(["http://a.test/1", "http://a.test/2"]); await asyncio.sleep(0.2)
    q = tv.link_queues[7]
    while not q.empty(): q.get_nowait()
    tv.pending_links[7].clear()
    await asyncio.wait_for(tv.link_workers.get(7) or asyncio.sleep(0), 40)
    ok(not CALLS, "Cancel during the pause: no link starts afterwards")
    tv._user_flood_until[0] = 0

# ---------- D. worker: source HTTP 429 ----------
async def partD():
    reset(); state = {"n": 0}
    async def dl429(url, reply_msg, uid):
        CALLS.append((url, time.time()))
        tv._diag_reset(uid)
        if "x.test" in url and state["n"] < 1:
            state["n"] += 1; tv._diag(uid, "HLS via yt-dlp", "playlist fetch", "HTTP 429"); tv._rl_seen["ts"] = time.time(); tv._rl_seen["after"] = 2
            return
        tv._upload_count[uid] = tv._upload_count.get(uid, 0) + 1
    tv._do_dl = dl429
    t0 = time.time(); m = await start(["http://x.test/1", "http://y.test/2"])
    await asyncio.wait_for(tv.link_workers[7], 20)
    order = [c[0] for c in CALLS]
    ok(order == ["http://x.test/1", "http://y.test/2", "http://x.test/1"], f"429: other site continues, the limited link is retried last ({order})")
    ok(CALLS[2][1] - CALLS[0][1] >= 2.0, f"the server's Retry-After (2s) is waited out in full ({CALLS[2][1]-CALLS[0][1]:.1f}s)")
    ok(any("asked to slow down" in r and "try 2 of 3" in r for r in m.replies), "user told: slow down, retry time, try count")
    ok(tv.pending_links[7] == [] and not tv._rl_tries, "queue bookkeeping clean after success")
    # bounded
    reset()
    async def always429(url, reply_msg, uid):
        CALLS.append((url, time.time())); tv._diag_reset(uid); tv._diag(uid, "HLS via yt-dlp", "playlist fetch", "HTTP 429")
        tv._rl_seen["ts"] = time.time(); tv._rl_seen["after"] = 1
    tv._do_dl = always429
    m = await start(["http://x.test/1"]); await asyncio.wait_for(tv.link_workers[7], 30)
    ok(len(CALLS) == 3 and any("dropped" in r for r in m.replies), f"still limited: {len(CALLS)} tries, then dropped with a message")
    # absurd wait is reported, never cut short and retried early
    reset()
    async def huge(url, reply_msg, uid):
        CALLS.append((url, time.time())); tv._diag_reset(uid); tv._diag(uid, "HLS via yt-dlp", "playlist fetch", "HTTP 429")
        tv._rl_seen["ts"] = time.time(); tv._rl_seen["after"] = 10 * 3600
    tv._do_dl = huge
    m = await start(["http://x.test/1"]); await asyncio.wait_for(tv.link_workers[7], 10)
    ok(len(CALLS) == 1 and any("too long" in r for r in m.replies), "wait longer than 6h: reported, not retried early")
    # cancel while a link is cooling down
    reset(); state["n"] = 0
    async def dl429b(url, reply_msg, uid):
        CALLS.append((url, time.time())); tv._diag_reset(uid); tv._diag(uid, "HLS via yt-dlp", "playlist fetch", "HTTP 429")
        tv._rl_seen["ts"] = time.time(); tv._rl_seen["after"] = 60
    tv._do_dl = dl429b
    m = await start(["http://x.test/1"]); await asyncio.sleep(0.6)
    ok(tv.link_queues[7].qsize() == 1, "limited link waits in the queue during the cooldown")
    tv._cancel_chat(7)
    await asyncio.wait_for(tv.link_workers.get(7) or asyncio.sleep(0), 40)
    ok(len(CALLS) == 1, "Cancel during the cooldown: the link is not retried")

# ---------- E. real _do_dl + real yt-dlp against a local 429 server ----------
FF = shutil.which("ffmpeg"); FX = tempfile.mkdtemp(prefix="rl_"); os.makedirs(f"{FX}/w")
subprocess.run([FF, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=4", "-f", "lavfi", "-i", "sine=f=440:d=4",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod", f"{FX}/w/index.m3u8"], check=True)
HITS = []; LIMIT = {"on": True}
class H(SimpleHTTPRequestHandler):
    def log_message(s, *a): pass
    def translate_path(s, p): return f"{FX}/w" + p.split("?")[0]
    def do_GET(s):
        HITS.append(s.path)
        if LIMIT["on"]:
            s.send_response(429); s.send_header("Retry-After", "2"); s.send_header("Content-Length", "0"); s.end_headers(); return
        super().do_GET()
srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
tv.FFPROBE_PATH = shutil.which("ffprobe")
tv._YDL_RELIABLE = {**tv._YDL_RELIABLE, "retry_sleep_functions": {"http": lambda n: 0, "fragment": lambda n: 0}}
async def partE():
    reset(); up = []
    async def fin(path, title, status, uid, chat_id, skip_convert=False):
        up.append(path); tv._upload_count[chat_id] = tv._upload_count.get(chat_id, 0) + 1; return True
    tv._finish_upload = fin
    u = f"http://127.0.0.1:{PORT}/index.m3u8"
    async def snatch(url, status=None): return {"url": u, "title": "t", "type": "hls", "headers": {"Referer": u}}
    tv.universal_snatch = snatch; tv._scrape_real_url = lambda url: (None, "", "")
    tv._RL_MAX_TRIES = 3
    async def release():
        await real_sleep(3.0); LIMIT["on"] = False
    asyncio.create_task(release())
    m = await start([u]); t0 = time.time()
    await asyncio.wait_for(tv.link_workers[7], 60)
    ok(len(up) == 1 and tv._probe_content_duration(up[0]) > 3, f"real _do_dl: 429 with Retry-After, link retried after the wait, one complete file ({time.time()-t0:.1f}s)")
    ok(any("asked to slow down" in r and "127.0.0.1" in r for r in m.replies), "user told which site and when it retries (host only, no link or token)")
    ok(not any("index.m3u8" in r or "?" in r for r in m.replies), "no URL path or query in the messages")

async def main():
    global _real_do_dl
    await partB(); await partC()
    _keep = tv._do_dl
    await partD()
    tv._do_dl = _REAL
    await partE()
_REAL = tv._do_dl
asyncio.run(main())
sys.exit(1 if failed else 0)
