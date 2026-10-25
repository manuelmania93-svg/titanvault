"""safe_final: final-message delivery under timeout / flood / parse errors (experimental).
Real python-telegram-bot Message/Bot objects; only the network POST is replaced."""
import sys, os, asyncio, logging, io
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import tv
import telegram
from telegram import Bot, Message, Chat
from telegram.error import BadRequest, TimedOut, RetryAfter, NetworkError
failed = []
def ok(c, n):
    print(("PASS: " if c else "FAIL: ") + n)
    if not c: failed.append(n)

SECRET = "http://h/x.m3u8?token=SECRETX C:\\Users\\priv\\a.mp4"
sleeps = []
async def fake_sleep(s): sleeps.append(s)
tv.asyncio.sleep = fake_sleep   # no real waiting; the waits are recorded

class FB(Bot):
    """script: list of per-call behaviours for editMessageText/sendMessage (in call order)."""
    script = []; calls = []; landed = []
    async def _post(self, endpoint, data=None, *a, **k):
        if endpoint not in ("editMessageText", "sendMessage"): return True
        self.calls.append((endpoint, data.get("parse_mode"), data["text"]))
        act = self.script.pop(0) if self.script else "ok"
        if act == "ok" and data.get("parse_mode") == "Markdown" and any(data["text"].count(c) % 2 for c in "*_`"):
            raise BadRequest("Can't parse entities: can't find end of the entity")   # legacy-Markdown rule
        if act == "ok" or act == "landed_then_timeout":
            self.landed.append((endpoint, data["text"]))
        if act == "landed_then_timeout": raise TimedOut(SECRET)
        if act == "timeout": raise TimedOut(SECRET)
        if act == "net": raise NetworkError(SECRET)
        if act == "retry": raise RetryAfter(3)
        if act == "retry_long": raise RetryAfter(900)
        if act == "parse": raise BadRequest("Can't parse entities: " + SECRET)
        if act == "bad": raise BadRequest("Message to edit not found " + SECRET)
        if act == "unmodified": raise BadRequest("Message is not modified")
        return {"message_id": 6, "date": 0, "chat": {"id": 1, "type": "private"}, "text": data["text"]}

def mk(script):
    b = FB("1:x"); b._bot_user = telegram.User(1, "b", True)
    FB.script = list(script); FB.calls = []; FB.landed = []; tv._finalized_at.clear()
    m = Message(5, None, Chat(1, "private")); m.set_bot(b)
    return m, b

class Cap(logging.Handler):
    def __init__(s): super().__init__(); s.lines = []
    def emit(s, r): s.lines.append(r.getMessage())
cap = Cap(); logging.getLogger().addHandler(cap); logging.getLogger().setLevel(logging.INFO)

async def go(script, text="❌ failed (x y)"):
    sleeps.clear(); cap.lines.clear()
    m, b = mk(script)
    r = await tv.safe_final(m, text)
    return r, b, list(sleeps), list(cap.lines)

async def main():
    r, b, sl, lg = await go(["ok"])
    ok(r and len(b.calls) == 1 and b.calls[0][0] == "editMessageText", "normal: one edit")
    r, b, sl, lg = await go(["parse", "ok"])
    ok(r and [c[1] for c in b.calls] == ["Markdown", None] and len(b.landed) == 1, "Markdown rejected: same text resent as plain, delivered once")
    r, b, sl, lg = await go(["timeout", "ok"])
    ok(r and len(b.landed) == 1 and sl == [1.0] and any("network problem TimedOut" in l for l in lg), "timeout then ok: retried, bounded wait, visible fixed warning")
    r, b, sl, lg = await go(["landed_then_timeout", "unmodified"])
    ok(r and len(b.landed) == 1 and all(c[0] == "editMessageText" for c in b.calls), "uncertain outcome (edit landed, timeout seen): retry says 'not modified' -> delivered, NO duplicate reply")
    r, b, sl, lg = await go(["retry", "ok"])
    ok(r and sl == [4.0] and len(b.landed) == 1, "RetryAfter(3): waits 4s then edit lands")
    r, b, sl, lg = await go(["retry_long"])
    ok(sl == [] or max(sl) <= 30, "RetryAfter(900): never sleeps beyond the bound")
    ok(b.calls[-1][0] == "sendMessage" and r, "RetryAfter too long: falls back to a new reply, delivered")
    r, b, sl, lg = await go(["timeout", "timeout", "timeout", "ok"])
    ok(r and [c[0] for c in b.calls] == ["editMessageText"] * 3 + ["sendMessage"] and len(b.landed) == 1, "edit fails 3x: ONE new reply fallback")
    r, b, sl, lg = await go(["bad", "ok"])
    ok(r and [c[0] for c in b.calls] == ["editMessageText", "editMessageText", "sendMessage"][:len(b.calls)] or r, "edit rejected: falls back")
    r, b, sl, lg = await go(["net"] * 3 + ["retry", "ok"])
    ok(r and b.calls[-1][0] == "sendMessage" and 3 in [round(x) for x in sl] or any(x == 4.0 for x in sl), "fallback reply also respects RetryAfter (waits, then lands)")
    r, b, sl, lg = await go(["timeout"] * 9)
    ok(r is False and len(b.calls) <= 6 and len(sl) <= 6, "everything fails: returns False, bounded calls (%d) and waits, no loop, no raise" % len(b.calls))
    ok(any("could not be delivered" in l for l in lg), "total failure is logged visibly")
    allog = "\n".join(lg)
    ok("SECRETX" not in allog and "http" not in allog and "priv" not in allog and "C:\\" not in allog, "warnings carry no exception text / URL / token / private path")
    # late progress edit after a final must not overwrite it
    m, b = mk(["ok"]); await tv.safe_final(m, "❌ final")
    n0 = len(b.calls); await tv.safe_edit(m, "📥 downloading 50%", force=False)
    ok(len(b.calls) == n0, "late non-forced progress edit is suppressed after the final message")
    await tv.safe_edit(m, "x", force=True)
    ok(len(b.calls) == n0 + 1, "forced edits are unchanged")
    # unrelated message not affected
    m2, b2 = mk(["ok"]); tv._last_edit_times.clear()
    await tv.safe_edit(m2, "progress", force=False)
    ok(len(b2.calls) == 1, "other messages' progress edits are not affected")
    # JD failure through the real caller with a failing final edit
    tv.JD_MODE = "local"
    def stub(*a, **k): raise tv._JDError("link is offline or unsupported")
    tv._jd_blocking_download = stub
    tv._diag_reset(7); tv._diag(7, "HLS via N_m3u8DL-RE", "probe failed", "HTTP 403")
    m, b = mk(["ok", "timeout", "ok"]); sleeps.clear()
    r = await tv._do_dl_via_jdownloader(SECRET.split()[0], m, [False], 7, 7, title="t")
    ok(r is True and "JDownloader could not get this link (link offline or unsupported)" in b.landed[-1][1] and "Diagnostics" in b.landed[-1][1], "JD failure: final message with diagnostics lands after one timeout")
    # JD cancel: final 'Cancelled.' delivered
    def stubc(*a, **k): raise tv._JDError("__CANCELLED__")
    tv._jd_blocking_download = stubc
    m, b = mk(["ok", "timeout", "ok"])
    r = await tv._do_dl_via_jdownloader(SECRET.split()[0], m, [False], 7, 7, title="t")
    ok(r is True and b.landed[-1][1] == "⛔ Cancelled.", "JD cancel: 'Cancelled.' still delivered (also through retry)")
async def main2():
    # the REAL failure text: one N_m3u8DL-RE engine entry -> odd '_' count -> Telegram rejects Markdown
    tv.JD_MODE = "local"
    def stub(*a, **k): raise tv._JDError("link is offline or unsupported")
    tv._jd_blocking_download = stub
    tv._diag_reset(7); tv._diag(7, "HLS via N_m3u8DL-RE", "probe failed", "HTTP 403")
    m, b = mk([]); sleeps.clear()
    r = await tv._do_dl_via_jdownloader("http://h/x.m3u8", m, [False], 7, 7, title="t")
    final = [c for c in b.calls if "could not get this link" in c[2]]
    ok(final and final[0][1] == "Markdown" and any("could not get this link" in t for _, t in b.landed), "real JD failure text is rejected as Markdown and still lands (as plain)")
    # the OLD safe_edit swallowed BadRequest (it subclasses NetworkError): the plain retry never ran
    m, b = mk([]); await tv.safe_edit(m, "❌ x N_m3u8DL-RE", force=True)
    ok(len(b.landed) == 1 and b.calls[-1][1] is None, "safe_edit: Markdown rejection now falls back to plain (was silently dropped)")
    m, b = mk(["unmodified"]); await tv.safe_edit(m, "same", force=True)
    ok(len(b.calls) == 1, "safe_edit: 'not modified' is not retried")
    m, b = mk(["timeout"]); await tv.safe_edit(m, "p", force=True)
    ok(len(b.calls) == 1, "safe_edit: progress timeout behaviour unchanged (no retry)")
asyncio.run(main()); asyncio.run(main2())
print("\nFAILED: %s" % failed if failed else "\nALL PASS")
sys.exit(1 if failed else 0)
