# Real end-to-end test: the actual cat-catch browser extension (source checkout)
# clicks its aria2 send button against the live TitanVault loopback bridge.
# Needs: pip install playwright + `playwright install chromium`, ffmpeg, and a
# cat-catch source checkout (CC_EXT_DIR env or /tmp/catcatch/ext). Local fixtures only.
# Run: xvfb-run -a python3 test_catcatch_e2e.py   (headful Chromium required for extensions)
from harness import *
import threading, http.server, socketserver, functools, subprocess, shutil, re, json, time
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP test_catcatch_e2e: playwright not installed"); sys.exit(0)

EXT_DIR = os.environ.get("CC_EXT_DIR", "/tmp/catcatch/ext")
if not os.path.isdir(EXT_DIR):
    print("SKIP test_catcatch_e2e: cat-catch source not found at", EXT_DIR); sys.exit(0)

WWW = tempfile.mkdtemp(prefix="ccwww_")
MP4 = os.path.join(tempfile.mkdtemp(prefix="ccfx_"), "big.mp4")
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                "testsrc=duration=12:size=640x360:rate=25", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=12", "-c:v", "libx264", "-c:a", "aac", MP4], check=True)
shutil.copy(MP4, os.path.join(WWW, "big.mp4"))

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=WWW))
HTTP_PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
# the fixture page must point at the actual port
open(os.path.join(WWW, "page.html"), "w").write(
    '<!doctype html><title>Fixture Player</title>'
    f'<video controls autoplay muted width="640" src="http://127.0.0.1:{HTTP_PORT}/big.mp4"></video>')
# bridge on a free port too, so parallel runs cannot collide
_s = socketserver.TCPServer(("127.0.0.1", 0), Quiet); tv.BRIDGE_PORT = _s.server_address[1]; _s.server_close()

tv.init_db()
jobs = []
class FakeBot:
    async def send_message(self, chat_id, text, **kw): jobs.append(("msg", chat_id, text)); return FakeMsg()
async def fake_stream(url, status, uid, chat_id, referer="", title=""): jobs.append(("stream", url, chat_id, referer, title))
tv._run_direct_stream = fake_stream

TOKEN = []
def bridge_thread():
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    tv.start_bridge(loop, FakeBot())
    class U:
        effective_user = types.SimpleNamespace(id=42)
        effective_chat = types.SimpleNamespace(id=555)
        message = FakeMsg()
    u = U()
    loop.run_until_complete(tv.catcatch_cmd(u, None))
    TOKEN.append(re.search(r"Token/secret: `([^`]+)`", u.message.edits[-1]).group(1))
    loop.run_forever()
threading.Thread(target=bridge_thread, daemon=True).start()
for _ in range(50):
    if TOKEN: break
    time.sleep(0.2)
RPC = f"http://127.0.0.1:{tv.BRIDGE_PORT}/jsonrpc"

def run_e2e():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="ccprof_"), headless=False,
            args=[f"--disable-extensions-except={EXT_DIR}", f"--load-extension={EXT_DIR}",
                  "--no-sandbox", "--autoplay-policy=no-user-gesture-required"],
            viewport={"width": 1000, "height": 750})
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        eid = sw.url.split("/")[2]
        # 1. configure through the real options UI (save= inputs autosave on input)
        opt = ctx.new_page(); opt.goto(f"chrome-extension://{eid}/options.html")
        opt.wait_for_timeout(1500)
        opt.locator("#anchorAria2Rpc").scroll_into_view_if_needed()
        opt.locator("#aria2Rpc").fill(RPC); opt.wait_for_timeout(800)
        opt.locator("#aria2RpcToken").fill(TOKEN[0]); opt.wait_for_timeout(800)
        opt.locator("#enableAria2Rpc").evaluate("el => el.click()")
        opt.wait_for_timeout(1500)
        opt.screenshot(path="/tmp/cc_e2e_options.png")
        stored = opt.evaluate("chrome.storage.sync.get(['aria2Rpc','aria2RpcToken','enableAria2Rpc'])")
        ok(stored.get("aria2Rpc") == RPC, "options UI saved RPC address")
        ok(stored.get("aria2RpcToken") == TOKEN[0], "options UI saved token")
        ok(stored.get("enableAria2Rpc") is True, "Aria2 RPC toggle enabled")
        opt.close()
        # 2. play the fixture so cat-catch sniffs it
        page = ctx.new_page(); page.goto(f"http://127.0.0.1:{HTTP_PORT}/page.html")
        page.wait_for_timeout(4000)
        tabid = sw.evaluate(f"chrome.tabs.query({{url:'http://127.0.0.1:{HTTP_PORT}/*'}}).then(t=>t[0].id)")
        # 3. real popup, click the row's aria2 send icon
        pop = ctx.new_page(); pop.goto(f"chrome-extension://{eid}/popup.html?tabId={tabid}")
        pop.wait_for_timeout(2500)
        pop.screenshot(path="/tmp/cc_e2e_popup.png")
        rows = pop.locator("#aria2")
        ok(rows.count() > 0, f"popup shows {rows.count()} detected media row(s) with aria2 button")
        rows.first.click(); pop.wait_for_timeout(2500)
        pop.screenshot(path="/tmp/cc_e2e_sent.png")
        tips = pop.locator("#TipsFixed").inner_text() if pop.locator("#TipsFixed").count() else ""
        ok(re.search(r'"result"\s*:\s*"[0-9a-f]{16}"', tips) is not None, f"popup shows TitanVault gid ({tips[:80]})")
        ctx.close()

run_e2e()
time.sleep(1)
handoffs = [j for j in jobs if j[0] == "stream"]
ok(len(handoffs) >= 1, "bridge received the handoff job from the real cat-catch click")
if handoffs:
    j = handoffs[0]
    ok(j[1].endswith("/big.mp4"), f"handoff URL is the fixture media ({j[1]})")
    ok(j[3].startswith(f"http://127.0.0.1:{HTTP_PORT}"), f"referer forwarded ({j[3]})")
    ok(j[2] == 555, "job routed to the paired chat")
httpd.shutdown()
sys.exit(1 if ok.failed else 0)
