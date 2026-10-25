"""JDownloader wait is bounded; cancel/cleanup/queue/final message (experimental). Local stub JD API only."""
import sys, os, json, threading, time, http.server, socketserver, asyncio, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import tv, FakeMsg
failed = []
def ok(c, n):
    print(("PASS: " if c else "FAIL: ") + n)
    if not c: failed.append(n)

MODE = {"m": "unknown"}; CALLS = []; JOB = ["/x"]; CASE = {"upper": False}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(s, *a): pass
    def do_POST(s):
        n = int(s.headers.get("Content-Length", 0)); s.rfile.read(n)
        CALLS.append(s.path); jd = JOB[0]; m = MODE["m"]
        shown = jd.upper() if CASE["upper"] else jd
        if s.path.endswith("addLinks"): out = {"id": 1}
        elif s.path == "/linkgrabberv2/queryPackages": out = [] if m == "nopkg" else [{"uuid": 1, "saveTo": shown}]
        elif s.path == "/linkgrabberv2/queryLinks": out = [{"uuid": 2, "availability": "ONLINE" if m == "online" else "TEMP_UNKNOWN"}]
        elif s.path == "/downloadsV2/queryPackages": out = [{"uuid": 3, "saveTo": shown}]
        elif s.path == "/downloadsV2/queryLinks":
            open(os.path.join(jd, "f.mp4"), "wb").write(b"x" * 10)
            out = [{"uuid": 4, "finished": True, "bytesLoaded": 10, "bytesTotal": 10}]
        else: out = {}
        b = json.dumps({"data": out}).encode(); s.send_response(200); s.send_header("Content-Length", str(len(b))); s.end_headers(); s.wfile.write(b)
class S(socketserver.ThreadingTCPServer): allow_reuse_address = True; daemon_threads = True
srv = S(("127.0.0.1", 8124), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
tv.JD_MODE = "local"; tv.JD_LOCAL_URL = "http://127.0.0.1:8124"
JD_DIR = "/tmp/jdjob_t"
def run(mode, resolve, cancel_after=None):
    MODE["m"] = mode; CALLS.clear(); tv.JD_RESOLVE_TIMEOUT_SEC = resolve; tv.JD_TIMEOUT_SEC = 60
    shutil.rmtree(JD_DIR, ignore_errors=True); os.makedirs(JD_DIR); JOB[0] = JD_DIR
    flag = [False]
    if cancel_after: threading.Timer(cancel_after, lambda: flag.__setitem__(0, True)).start()
    t = time.time()
    try: r = tv._jd_blocking_download("http://fixture/x.m3u8", JD_DIR, "pkg", flag, lambda *a: None)
    except Exception as e: r = e
    return r, time.time() - t

print("code defaults: JD_RESOLVE_TIMEOUT_SEC=%s JD_TIMEOUT_SEC(download)=%s" % (os.getenv("JD_RESOLVE_TIMEOUT_SEC", "90"), os.getenv("JD_TIMEOUT_SEC", "3600")))
r, dt = run("unknown", 3)
ok(isinstance(r, tv._JDError) and "timed out resolving" in str(r) and 3 <= dt < 7, "link stuck in TEMP_UNKNOWN: gives up at the resolve limit (%.1fs for a 3s limit)" % dt)
ok(any("linkgrabberv2/removeLinks" in c for c in CALLS), "stalled resolve: our JD entries are removed")
r, dt = run("unknown", 60, cancel_after=2)
ok(isinstance(r, tv._JDError) and "__CANCELLED__" in str(r) and dt < 5, "cancel during the wait stops it within seconds (%.1fs)" % dt)
r, dt = run("nopkg", 3)
ok(isinstance(r, tv._JDError) and "timed out resolving" in str(r) and dt < 7, "no package ever appears: bounded too")
r, dt = run("online", 5)
ok(isinstance(r, str) and r.endswith("f.mp4"), "healthy ONLINE link still downloads")

async def flow():
    # full caller: stalled JD -> final message (diagnostics) delivered, queue/dup guard released
    MODE["m"] = "unknown"; tv.JD_RESOLVE_TIMEOUT_SEC = 3; JOB[0] = tv.os.path.join(tv.ARIA2C_DOWNLOAD_DIR, "x")
    uid = 9; url = "http://fixture/x.m3u8"
    tv.link_queues[uid] = asyncio.Queue(); tv.pending_links[uid] = [url]; await tv.link_queues[uid].put(url)
    m = FakeMsg(); tv._diag_reset(uid); tv._diag(uid, "HLS via N_m3u8DL-RE", "probe failed", "HTTP 403")
    real_jd = tv._jd_blocking_download
    def stalled(u, job_dir, pkg, flag, cb):
        JOB[0] = job_dir; return real_jd(u, job_dir, pkg, flag, cb)
    tv._jd_blocking_download = stalled
    async def fake_do_dl(u, reply, chat):
        tv.dl_cancel_flags[chat] = [False]
        await tv._do_dl_via_jdownloader(u, reply, tv.dl_cancel_flags[chat], uid, chat, title="t")
    tv._do_dl = fake_do_dl
    t = time.time(); tv.link_workers[uid] = asyncio.create_task(tv._link_worker(uid, m)); await tv.link_workers[uid]
    dt = time.time() - t
    ok(dt < 8 and "could not get this link (timeout)" in m.last and "N_m3u8DL-RE: probe failed" in m.last, "stalled JD: final failure + diagnostics shown after %.1fs" % dt)
    ok(url not in tv.pending_links[uid] and uid not in tv.link_workers, "queue released: duplicate guard cleared, worker finished (same link can be sent again)")
    # cancel through the worker: pending cleared, worker ends
    tv.pending_links[uid] = [url]; await tv.link_queues[uid].put(url)
    tv.link_workers[uid] = asyncio.create_task(tv._link_worker(uid, m))
    await asyncio.sleep(1.5)
    tv.dl_cancel_flags[uid][0] = True
    t = time.time(); await tv.link_workers[uid]
    ok(time.time() - t < 6 and not tv.pending_links[uid] and uid not in tv.link_workers and m.last == "⛔ Cancelled.", "cancel mid-wait: ends, 'Cancelled.' shown, queue released")
asyncio.run(flow())
print("\nFAILED: %s" % failed if failed else "\nALL PASS")
sys.exit(1 if failed else 0)
