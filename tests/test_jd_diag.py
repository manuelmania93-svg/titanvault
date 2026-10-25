"""JDownloader failure messages end with the diagnostics block (experimental). Stubs only, no JD needed."""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import tv, FakeMsg
failed = []
def ok(c, n):
    print(("PASS: " if c else "FAIL: ") + n)
    if not c: failed.append(n)

uploads = []
async def fake_finish(path, title, status, uid, chat_id, skip_convert=False):
    uploads.append(path); return True
tv._finish_upload = fake_finish
tv.JD_MODE = "local"

def seed():
    tv._diag_reset(7)
    tv._diag(7, "HLS via yt-dlp", "playlist fetch", "HTTP 403")
    tv._diag(7, "HLS via N_m3u8DL-RE", "probe failed", "playlist fetch - HTTP 403 (exe request)")

def block(msg):
    return msg.last.split("Diagnostics", 1)[1] if "Diagnostics" in msg.last else ""

async def run(exc=None, path=None, flag=None):
    def stub(*a, **k):
        if exc: raise exc
        return path
    tv._jd_blocking_download = stub
    m = FakeMsg(); uploads.clear()
    r = await tv._do_dl_via_jdownloader("http://h/x.m3u8?token=SECRETJD", m, flag or [False], 7, 7, title="t")
    return r, m

async def main():
    seed(); r, m = await run(tv._JDError("link is offline or unsupported"))
    b = block(m)
    ok(r is True and "JDownloader could not get this link" in m.last and "N_m3u8DL-RE: probe failed - playlist fetch - HTTP 403 (exe request)" in b and "yt-dlp: playlist fetch - HTTP 403" in b, "offline: final message carries the pre-seeded engine entries")
    ok("JDownloader: download - link offline or unsupported" in b, "offline: fixed JD state recorded")
    seed(); r, m = await run(tv._JDError("JDownloader error: http://h/x.m3u8?token=SECRETJD"))
    b = block(m)
    ok("SECRETJD" not in b and "http" not in b and "download error" in b, "diagnostics block never carries URL/token text from the error")
    seed(); r, m = await run(tv._JDError("offline https://h/x.m3u8?token=SECRETJD C:\\Users\\priv\\f.mp4 /home/priv/f"))
    ok(all(t not in m.last for t in ("SECRETJD", "http", "priv", "C:\\")) and "could not get this link (link offline or unsupported)" in m.last, "whole message (not just block) has no URL/token/private path from the exception")
    seed(); r, m = await run(tv._JDError("timed out downloading"))
    ok("JDownloader: download - timeout" in block(m), "timeout state recorded")
    seed(); r, m = await run(RuntimeError("boom http://h/SECRETJD"))
    b = block(m)
    ok(r is True and "JDownloader request failed" in m.last and "/jdtest" in m.last and "JDownloader: launch - other connection problem (RuntimeError)" in b and "N_m3u8DL-RE: probe failed" in b and "SECRETJD" not in b, "unexpected error: block appended, fixed state, no error text")
    seed(); r, m = await run(tv._JDError("__CANCELLED__"))
    ok(r is True and "Cancelled" in m.last and "Diagnostics" not in m.last, "cancel: still plain 'Cancelled.', no block")
    seed(); f = os.path.join(tv.ARIA2C_DOWNLOAD_DIR, "okfile.mp4"); open(f, "w").write("x")
    r, m = await run(path=f)
    ok(r is True and uploads == [f] and "Diagnostics" not in m.last, "success: handed to upload, no failure block")
    tv.JD_MODE = ""; seed()
    r, m = await run(tv._JDError("x"))
    ok(r is False and not m.edits, "JD disabled: returns False, no message, engines' diagnostics untouched")
    ok(len(tv.dl_diag[7]) == 2, "disabled: no JD entry added")
asyncio.run(main())
print("\nFAILED: %s" % failed if failed else "\nALL PASS")
sys.exit(1 if failed else 0)
