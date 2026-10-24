from harness import *
import requests, json, time, re
tv.init_db()
rec = []
class FakeBot:
    async def send_message(self, chat_id, text, **kw): rec.append(("msg", chat_id, text)); return FakeMsg()
async def fake_stream(url, status, uid, chat_id, referer="", title=""): rec.append(("stream", url, uid, referer, title))
tv._run_direct_stream = fake_stream
B = f"http://127.0.0.1:{tv.BRIDGE_PORT}"
CC = {"Origin": "chrome-extension://catcatchid00000000000000000000", "Content-Type": "application/json; charset=utf-8"}
def rpc(body, headers=None, token=None):
    b = dict(body); h = dict(CC); h.update(headers or {})
    return requests.post(B+"/jsonrpc", data=json.dumps(b), headers=h, timeout=5)
def add_uri(tok, url, **opts):
    params = [f"token:{tok}", [url]]
    if opts: params.append(opts)
    return {"jsonrpc": "2.0", "id": "cat-catch-1", "method": "aria2.addUri", "params": params}
async def run():
    srv = tv.start_bridge(asyncio.get_running_loop(), FakeBot())
    ok(srv is not None, "bridge bound")
    T = lambda f,*a,**k: asyncio.to_thread(f,*a,**k)
    # pair to obtain a real token
    class U:
        def __init__(s, uid): s.effective_user=types.SimpleNamespace(id=uid); s.effective_chat=types.SimpleNamespace(id=555); s.message=FakeMsg()
    u = U(42); await tv.pair_cmd(u, None); code = list(tv._pair_codes)[0]
    r = await T(requests.post, B+"/pair", data=json.dumps({"code": code}), headers=CC, timeout=5)
    tok = r.json()["token"]

    r = await T(rpc, {"jsonrpc":"2.0","id":"v","method":"aria2.getVersion","params":[]})
    ok(r.status_code == 401 and "error" in r.json(), "getVersion without token -> 401 jsonrpc error")
    r = await T(rpc, {"jsonrpc":"2.0","id":"v","method":"aria2.getVersion","params":[f"token:{tok}"]})
    ok(r.status_code == 200 and r.json()["result"]["version"].startswith("TitanVaultBridge"), "getVersion with token ok")
    r = await T(rpc, add_uri("wrong-token", "https://cdn.example/v.m3u8"))
    ok(r.status_code == 401, "addUri with wrong token -> 401")
    r = await T(rpc, {"jsonrpc":"2.0","id":"x","method":"aria2.pauseAll","params":[f"token:{tok}"]})
    ok(r.status_code == 200 and r.json()["error"]["code"] == -32601, "unknown aria2 method -> -32601")
    r = await T(rpc, add_uri(tok, "file:///etc/passwd"))
    ok(r.status_code == 200 and r.json()["error"]["code"] == -32002, "file:// uri -> jsonrpc error, not launched")
    ok(not any(x[0]=="stream" for x in rec), "nothing launched so far")
    r = await T(rpc, add_uri(tok, "https://cdn.example/v/index.m3u8?sig=ABC",
        out="My Movie.mp4", dir="/etc", header=["User-Agent: CC/1.0", "Referer: https://site.example/watch?x=1&utm=2", "Cookie: sid=1", "Authorization: Bearer z"]))
    await asyncio.sleep(0.3)
    ok(r.status_code == 200 and re.fullmatch(r"[0-9a-f]{16}", r.json().get("result","")), "valid addUri -> gid")
    job = [x for x in rec if x[0]=="stream"]
    ok(len(job)==1 and job[0][1].endswith("sig=ABC"), "job launched with media url")
    ok(job[0][3] == "https://site.example/watch", "referer forwarded (cleaned)")
    ok(job[0][4] == "My Movie.mp4", "out= used as display title only")
    ok(not any("sid=1" in str(x) or "Bearer z" in str(x) for x in rec), "cookie/authorization headers never reach the job")
    r = await T(rpc, add_uri(tok, "https://a.example/b.mp4"), {"Origin": "https://evil.example"})
    ok(r.status_code == 403, "web-page Origin -> 403")
    r = await T(rpc, add_uri(tok, "https://a.example/b.mp4"), {"Host": "evil.example"})
    ok(r.status_code == 403, "wrong Host (DNS rebinding) -> 403")
    # the BRIDGE_EXT_ID pin must not lock cat-catch out of /jsonrpc (token is the auth there)
    tv.BRIDGE_EXT_ID = "someotherextensionid000000000000"
    r = await T(rpc, add_uri(tok, "https://a.example/c.mp4"))
    ok(r.status_code == 200, "jsonrpc accepts other extension origins even when pin set")
    r = await T(requests.post, B+"/send", data=json.dumps({"url":"https://a.example/c.mp4"}), headers={**CC, "Authorization": "Bearer "+tok}, timeout=5)
    ok(r.status_code == 403, "/send still respects the extension pin")
    tv.BRIDGE_EXT_ID = ""
    # shared rate limit (10/min across /send and /jsonrpc); 2 used above
    for _ in range(9): await T(rpc, add_uri(tok, "https://a.example/b.mp4"))
    r = await T(rpc, add_uri(tok, "https://a.example/b.mp4"))
    ok(r.status_code == 429 and r.json()["error"]["code"] == -32029, "shared rate limit -> 429 jsonrpc error")
    # /catcatch owner command issues a working token, non-owner ignored
    u2 = U(1); await tv.catcatch_cmd(u2, None); ok(not u2.message.edits, "non-owner /catcatch ignored")
    u3 = U(42); await tv.catcatch_cmd(u3, None)
    m = re.search(r"Token/secret: `([^`]+)`", u3.message.edits[-1])
    ok(m is not None, "owner /catcatch shows setup with token")
    r = await T(rpc, add_uri(m.group(1), "https://a.example/d.mp4")) if m else None
    ok(r is not None and r.status_code in (200, 429), "catcatch token authenticates")
    srv.shutdown()
asyncio.run(run())
sys.exit(1 if ok.failed else 0)
