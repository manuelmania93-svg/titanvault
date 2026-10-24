from harness import *
import requests, json, time
tv.init_db()
rec = []
class FakeBot:
    async def send_message(self, chat_id, text, **kw): rec.append(("msg", chat_id, text)); return FakeMsg()
async def fake_stream(url, status, uid, chat_id, referer="", title=""): rec.append(("stream", url, uid, referer, title))
tv._run_direct_stream = fake_stream
B = f"http://127.0.0.1:{tv.BRIDGE_PORT}"
EXT = {"Origin": "chrome-extension://abcdefghijklmnop"}
def post(path, body, headers=None, raw=None):
    h = dict(EXT); h.update(headers or {})
    return requests.post(B+path, data=raw if raw is not None else json.dumps(body), headers=h, timeout=5)
async def run():
    srv = tv.start_bridge(asyncio.get_running_loop(), FakeBot())
    ok(srv is not None, "bridge bound")
    ok(srv.server_address[0] == "127.0.0.1", f"bound to loopback only {srv.server_address}")
    T = lambda f,*a,**k: asyncio.to_thread(f,*a,**k)
    r = await T(requests.get, B+"/ping", headers={}); ok(r.status_code==401, "GET without Origin but without token -> 401")
    r = await T(requests.get, B+"/ping", headers={"Origin":"https://evil.example"}); ok(r.status_code==403, "web-page Origin -> 403")
    r = await T(requests.get, B+"/ping", headers={"Origin":"chrome-extension://x","Host":"evil.example:%d"%tv.BRIDGE_PORT}); ok(r.status_code==403, "wrong Host header (DNS rebinding) -> 403")
    r = await T(requests.post, B+"/send", data="{}", headers={}); ok(r.status_code==403, "POST without Origin -> 403")
    r = await T(post, "/send", {"url":"http://a/b.m3u8"}); ok(r.status_code==401, "send without token -> 401")
    r = await T(post, "/pair", {"code":"nope"}); ok(r.status_code==403, "bad pairing code -> 403")
    # owner-only /pair command
    class U:  # fake update
        def __init__(s, uid): s.effective_user=types.SimpleNamespace(id=uid); s.effective_chat=types.SimpleNamespace(id=555); s.message=FakeMsg()
    u = U(1); await tv.pair_cmd(u, None); ok(not u.message.edits and not tv._pair_codes, "non-owner /pair ignored")
    u = U(42); await tv.pair_cmd(u, None); code = list(tv._pair_codes)[0]; ok(code in u.message.edits[-1], "owner /pair issues code")
    # fresh code (earlier bad attempt bumped fail counters by 1; still < 5)
    r = await T(post, "/pair", {"code":code}); tok = r.json().get("token"); ok(r.status_code==200 and tok, "pair code exchanged for token")
    r = await T(post, "/pair", {"code":code}); ok(r.status_code==403, "pairing code is single use")
    H = {"Authorization": "Bearer "+tok}
    r = await T(requests.get, B+"/ping", headers={**EXT, **H}); ok(r.status_code==200, "ping with token ok")
    r = await T(requests.get, B+"/ping", headers=H); ok(r.status_code==200, "ping with token and no Origin (how Chrome sends extension GETs) ok")
    r = await T(post, "/send", {"url":"file:///etc/passwd"}, H); ok(r.status_code==400, "file:// url rejected")
    r = await T(post, "/send", {"url":"javascript:alert(1)"}, H); ok(r.status_code==400, "javascript: url rejected")
    r = await T(post, "/send", None, H, raw="x"*9000); ok(r.status_code==413, "oversize body -> 413")
    r = await T(post, "/send", {"url":"https://cdn.example/v/index.m3u8?sig=ABC","page_url":"https://site.example/watch?id=1","title":"My\nVid"}, H)
    await asyncio.sleep(0.3)
    ok(r.status_code==202, "valid send accepted")
    ok(any(x[0]=="stream" and x[1].endswith("sig=ABC") and x[2]==555 and x[3].startswith("https://site.example") and "\n" not in x[4] for x in rec), "job started for paired chat with referer, newline stripped from title")
    ok(not any("sig=ABC" in str(x[2]) for x in rec if x[0]=="msg"), "chat message does not include query string")
    r = await T(post, "/send", {"url":"https://a.example/b.mp4","path":"/etc/passwd","cmd":"calc"}, H); ok(r.status_code==202, "extra fields (path/cmd) ignored, never used")
    for _ in range(12): await T(post, "/send", {"url":"https://a.example/b.mp4"}, H)
    r = await T(post, "/send", {"url":"https://a.example/b.mp4"}, H); ok(r.status_code==429, "rate limit after 10/min")
    # DB stores hash only
    import sqlite3; h = sqlite3.connect("vault.db").execute("select token_hash from bridge_tokens").fetchone()[0]
    ok(h != tok and len(h)==64, "token stored hashed")
    # unpair
    u = U(42); await tv.unpair_cmd(u, None)
    r = await T(requests.get, B+"/ping", headers={**EXT, **H}); ok(r.status_code==401, "revoked token -> 401")
    # busy chat is not interrupted
    srv.shutdown()
asyncio.run(run())
sys.exit(1 if ok.failed else 0)
