from harness import *
import subprocess
calls = []
async def fake_finish(file_path, title, status_msg, uid, chat_id):
    calls.append({"path": file_path, "exists": os.path.exists(file_path), "size": os.path.getsize(file_path) if os.path.exists(file_path) else 0, "title": title})
tv._finish_upload = fake_finish
def dur(p): return float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",p]).decode())
async def run():
    # 1) JD local backend, real JDownloader instance, local non-explicit fixture
    tv.JD_MODE = "local"; tv.JD_LOCAL_URL = "http://127.0.0.1:3128"
    m = FakeMsg(); cf = [False]; calls.clear()
    handled = await tv._do_dl_via_jdownloader("http://127.0.0.1:8099/clip3.mp4", m, cf, 7, 7, title="jdtest")
    ok(handled and len(calls)==1 and calls[0]["exists"] and calls[0]["size"]==os.path.getsize("/tmp/fx/www/clip3.mp4"), f"JD local: downloaded and handed to upload ({calls and calls[0]['size']} bytes)")
    ok(not os.path.exists(os.path.dirname(calls[0]["path"])) if calls else False, "JD local: job folder removed after upload hand-off")
    # JD list cleanup: our package gone
    import requests
    pk = requests.post("http://127.0.0.1:3128/downloadsV2/queryPackages", json={"saveTo":True}).json()["data"]
    ok(not any("jd_7_" in p.get("saveTo","") for p in pk), "JD local: our package removed from JD download list")
    # 2) JD offline link
    calls.clear(); m = FakeMsg()
    handled = await tv._do_dl_via_jdownloader("http://127.0.0.1:8099/does-not-exist.mp4", m, [False], 7, 7)
    ok(handled and not calls and "could not get" in m.last, f"JD local: offline link reports failure, no upload ({m.last[:60]!r})")
    # 3) JD cancel
    calls.clear(); m = FakeMsg(); cf=[True]
    handled = await tv._do_dl_via_jdownloader("http://127.0.0.1:8099/clip.mp4", m, cf, 7, 7)
    ok(handled and not calls and "Cancelled" in m.last, "JD local: cancel flag stops job")
    # 4) JD disabled
    tv.JD_MODE = ""
    ok(await tv._do_dl_via_jdownloader("http://x/y.mp4", FakeMsg(), [False], 7, 7) is False, "JD disabled -> not used")
    # 5) /stream direct mp4 (aria2c absent here -> cffi fallback path)
    calls.clear(); m = FakeMsg()
    await tv._run_direct_stream("http://127.0.0.1:8099/clip3.mp4?token=SECRET123", m, 8, 8, referer="http://127.0.0.1:8099/page.html?x=1", title="direct")
    ok(len(calls)==1 and calls[0]["size"]>10000, f"direct mp4 downloaded ({calls and calls[0]['size']} bytes)")
    ok(all("SECRET123" not in e for e in m.edits), "status messages never show the query string")
    # 6) /stream HLS playlist
    calls.clear(); m = FakeMsg()
    await tv._run_direct_stream("http://127.0.0.1:8099/hls/index.m3u8", m, 9, 9, referer="", title="hlsclip")
    ok(len(calls)==1 and abs(dur(calls[0]["path"])-12)<1.5 if calls else False, f"HLS playlist downloaded, duration ok ({calls and round(dur(calls[0]['path']),1)}s)")
    # 7) url validation / redaction
    ok(not tv._valid_http_url("file:///etc/passwd") and not tv._valid_http_url("javascript:alert(1)") and tv._valid_http_url("https://a.b/c"), "URL validation rejects non-http(s)")
    ok("SECRET" not in tv.redact_url("https://h.example/p/a.m3u8?sig=SECRET#f"), "redact_url strips query")
asyncio.run(run())
sys.exit(1 if ok.failed else 0)
