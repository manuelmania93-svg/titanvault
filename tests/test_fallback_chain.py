from harness import *
tv.init_db()
calls = []
# force: custom scrapers find nothing
tv._scrape_real_url = lambda url: (url, "", "")
# streamlink resolves an HLS playlist with session headers
tv._streamlink_resolve = lambda url: {"url": "https://cdn.example/obsidian/index.m3u8",
                                      "headers": {"User-Agent": "SL/9.9", "Referer": "https://host.example/watch"}}
async def fake_hls(url, status, cancel_flag, uid, chat_id, page_url="", title="", referer="", extra_headers=None):
    calls.append(("hls", url, referer, dict(extra_headers or {})))
tv._do_dl_via_ffmpeg_hls = fake_hls
async def fake_snatch(url, status=None): calls.append(("snatch", url)); return {}
tv.universal_snatch = fake_snatch

async def run():
    m = FakeMsg()
    captured = []
    orig_reply = m.reply_text
    async def cap_reply(text, **kw):
        s = await orig_reply(text, **kw); captured.append(s); return s
    m.reply_text = cap_reply
    await tv._do_dl("https://host.example/watch?v=1", m, 7)
    ok(calls and calls[0][0] == "hls", "resolved m3u8 routed to HLS engine")
    ok(calls[0][1].endswith("index.m3u8"), "streamlink URL used")
    ok(calls[0][3].get("User-Agent") == "SL/9.9" and calls[0][3].get("Referer", "").startswith("https://host.example"), "session headers preserved into HLS engine")
    ok(not any(c[0] == "snatch" for c in calls), "browser snatch not needed once streamlink resolves")
    ok(any("Streamlink resolved" in e for s in captured for e in s.edits), "status mentions the streamlink stage")

    # when streamlink finds nothing, the chain must continue to the browser snatch
    calls.clear()
    tv._streamlink_resolve = lambda url: None
    async def snatch_hit(url, status=None):
        calls.append(("snatch", url)); return {"url": "https://cdn.example/x.mp4", "title": "T", "headers": {"Referer": url}}
    tv.universal_snatch = snatch_hit
    async def fake_direct(url, out, status, cancel_flag, headers=None): calls.append(("aria2", url)); return False
    tv._do_dl_via_aria2c = fake_direct
    async def fake_cffi(url, status, cancel_flag, uid, chat_id, headers=None, title="", page_url=""): calls.append(("cffi", url))
    tv._do_dl_via_cffi = fake_cffi
    m2 = FakeMsg()
    await tv._do_dl("https://host.example/watch?v=2", m2, 8)
    ok(calls and calls[0] == ("snatch", "https://host.example/watch?v=2"), "streamlink miss -> chain continues to snatch")
    ok(("aria2", "https://cdn.example/x.mp4") in calls or ("cffi", "https://cdn.example/x.mp4") in calls, "snatch result still downloads")
asyncio.run(run())
sys.exit(1 if ok.failed else 0)
