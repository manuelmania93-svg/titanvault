"""test_convert.py - /convert spec parsing, apply_convert, live ffmpeg runner, upload hook.

Local generated media only, real ffmpeg/ffprobe, stubbed Telegram upload.
Run from this folder: python3 test_convert.py
"""
from harness import *
import subprocess, glob

tv.init_db()
DL = tv.ARIA2C_DOWNLOAD_DIR
os.makedirs(DL, exist_ok=True)

def gen_video(path, secs=6, w=320, h=240):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc=duration={secs}:size={w}x{h}:rate=10",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", path], check=True)

def streams(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                        "-of", "json", path], capture_output=True, text=True)
    import json as j
    return [s.get("codec_type") for s in j.loads(r.stdout).get("streams", [])]

# ── 1. spec parsing ────────────────────────────────────────────
s = tv.parse_convert_spec(["720p"]);           ok(s == {"scale": 720}, "parse 720p")
s = tv.parse_convert_spec(["mkv"]);            ok(s == {"container": "mkv"}, "parse mkv")
s = tv.parse_convert_spec(["mp3"]);            ok(s == {"audio_only": True, "acodec": "mp3"}, "parse mp3")
s = tv.parse_convert_spec(["crf=21", "h265"]); ok(s.get("crf") == 21 and s.get("vcodec") == "libx265", "parse crf=21 h265")
s = tv.parse_convert_spec(["500mb"]);          ok(s.get("target_mb") == 500, "parse 500mb")
s = tv.parse_convert_spec(["2gb"]);            ok(s.get("target_mb") == 2048, "parse 2gb")
s = tv.parse_convert_spec(["off"]);            ok(s == {}, "parse off clears")
s = tv.parse_convert_spec(["bogus", "128k"]);  ok(s == {"abr": "128k"}, "unknown tokens ignored, abr parsed")
ok(tv.describe_convert({}) == "off (stream-copy remux only)", "describe empty spec")
ok("720" in tv.describe_convert({"scale": 720}), "describe scale")

# ── 2. apply_convert with real ffmpeg ──────────────────────────
async def conv_tests():
    m = FakeMsg()

    src = os.path.join(DL, "c_audio.mp4"); gen_video(src)
    out = await tv.apply_convert(src, {"audio_only": True, "acodec": "mp3"}, m)
    ok(out.endswith(".mp3") and os.path.exists(out), "audio_only -> mp3 produced")
    st = streams(out)
    ok("audio" in st and "video" not in st, "mp3 has audio, no video")
    ok(not os.path.exists(src), "audio_only: source removed after success")

    src = os.path.join(DL, "c_mkv.mp4"); gen_video(src)
    out = await tv.apply_convert(src, {"container": "mkv"}, m)
    ok(out.endswith(".mkv") and "video" in streams(out), "container mkv -> remuxed playable mkv")

    src = os.path.join(DL, "c_scale.mp4"); gen_video(src)
    out = await tv.apply_convert(src, {"scale": 120}, m)
    _, _, hh = tv.get_video_info(out)
    ok(out.endswith(".mp4") and hh == 120, "scale 120 -> encoded height 120")

    src = os.path.join(DL, "c_target.mp4"); gen_video(src, secs=8)
    out = await tv.apply_convert(src, {"target_mb": 1}, m)
    dur, _, _ = tv.get_video_info(out)
    ok(os.path.getsize(out) <= 1.4 * 1024 * 1024 and 6 <= dur <= 10,
       "target_mb 1 -> two-pass output near target, duration kept")

    src = os.path.join(DL, "c_none.mp4"); gen_video(src)
    out = await tv.apply_convert(src, {}, m)
    ok(os.path.exists(out) and "video" in streams(out), "empty spec -> plain remux keeps video")

asyncio.run(conv_tests())

# ── 3. live ffmpeg runner: success + cancel ────────────────────
async def runner_tests():
    m = FakeMsg()
    src = os.path.join(DL, "r_src.mp4"); gen_video(src, secs=4)
    out = os.path.join(DL, "r_out.mp4")
    rc = await tv._run_ffmpeg_live(["ffmpeg", "-y", "-i", src, "-c", "copy", out], 4.0, m, "t")
    ok(rc == 0 and os.path.exists(out), "live runner: remux rc=0, output exists")

    src2 = os.path.join(DL, "r_src2.mp4"); gen_video(src2, secs=60, w=1280, h=720)
    out2 = os.path.join(DL, "r_out2.mp4")
    flag = [False]
    async def flip():
        await asyncio.sleep(0.5)
        flag[0] = True
    asyncio.ensure_future(flip())
    rc2 = await tv._run_ffmpeg_live(
        ["ffmpeg", "-y", "-i", src2, "-c:v", "libx264", "-preset", "veryslow", "-crf", "30", out2],
        60.0, m, "t", cancel_flag=flag)   # cancelled mid-run -> killed at next tick
    ok(rc2 != 0, "live runner: cancel flag kills ffmpeg")

asyncio.run(runner_tests())

# ── 4. _finish_upload convert hook wiring ──────────────────────
async def hook_tests():
    calls = {"apply": 0, "remux": 0}
    orig_apply, orig_remux = tv.apply_convert, tv._remux_live
    async def apply_spy(path, spec, status_msg=None, cancel_flag=None):
        calls["apply"] += 1
        return path
    async def remux_spy(path, status_msg=None, cancel_flag=None, **kw):
        calls["remux"] += 1
        return path
    async def good_upload(client, path, progress_callback=None): return "INPUT"
    sent = {}
    class TL:
        async def send_file(self, *a, **k): sent.update(k); return types.SimpleNamespace(id=1)
    tv.apply_convert, tv._remux_live = apply_spy, remux_spy
    tv.fast_upload, tv.tgclient = good_upload, TL()
    try:
        f = os.path.join(DL, "h_spec.mp4"); gen_video(f, secs=2)
        tv.user_convert[7] = {"scale": 120}
        r = await tv._finish_upload(f, "T", FakeMsg(), 7, 7)
        ok(r is True and calls["apply"] == 1, "spec set -> apply_convert called in upload path")
        ok(calls["remux"] == 0, "spec applied -> remux skipped")

        calls.update(apply=0, remux=0)
        f = os.path.join(DL, "h_skip.mp4"); gen_video(f, secs=2)
        r = await tv._finish_upload(f, "T", FakeMsg(), 7, 7, skip_convert=True)
        ok(calls["apply"] == 0, "skip_convert=True -> apply_convert not called")

        tv.user_convert.pop(7, None)
        calls.update(apply=0, remux=0)
        f = os.path.join(DL, "h_plain.mp4"); gen_video(f, secs=2)
        r = await tv._finish_upload(f, "T", FakeMsg(), 7, 7)
        ok(calls["apply"] == 0 and calls["remux"] == 1, "no spec -> remux runs as before")
    finally:
        tv.apply_convert, tv._remux_live = orig_apply, orig_remux
        tv.user_convert.pop(7, None)

asyncio.run(hook_tests())


# ── 5. quality policy: 1080p ceiling, closest-above fallback ──
import yt_dlp
def pick(fmts):
    ydl = yt_dlp.YoutubeDL({"quiet": True, "format": tv.YDL_FORMAT})
    sel = ydl.build_format_selector(tv.YDL_FORMAT)
    return [f.get("format_id") for f in sel({"formats": fmts, "id": "x", "title": "x", "extractor": "generic"})]
def vf(fid, h, vcodec="avc1.640028", tbr=1000, ext="mp4"):
    return {"format_id": fid, "vcodec": vcodec, "acodec": "none", "width": int(h*16/9), "height": h,
            "tbr": tbr, "fps": 25, "ext": ext, "protocol": "https", "format_note": f"{h}p", "filesize_approx": tbr*1000*60}
M4A = {"format_id": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128, "tbr": 128, "ext": "m4a", "protocol": "https"}
OPUS = {"format_id": "opus", "vcodec": "none", "acodec": "opus", "abr": 160, "tbr": 160, "ext": "webm", "protocol": "https"}

full = [vf("h264-480",480,tbr=800), vf("h264-1080",1080,tbr=4500),
        vf("vp9-1080",1080,vcodec="vp09.00.40.08",tbr=3000,ext="webm"),
        vf("vp9-1440",1440,vcodec="vp09.00.50.08",tbr=9000,ext="webm"),
        vf("vp9-2160",2160,vcodec="vp09.00.50.08",tbr=18000,ext="webm"), M4A, OPUS]
ok(pick(full) == ["vp9-1080+m4a"], "full menu -> highest <=1080p; codec only breaks same-res ties")
low = [vf("h264-480",480,tbr=800), vf("vp9-720",720,vcodec="vp09.00.30.08",tbr=2500,ext="webm"), M4A, OPUS]
ok(pick(low) == ["vp9-720+m4a"], "480p h264 vs 720p vp9 -> resolution wins")
mid = [vf("h264-720",720,tbr=2500), vf("vp9-1080",1080,vcodec="vp09.00.40.08",tbr=3000,ext="webm"), M4A, OPUS]
ok(pick(mid) == ["vp9-1080+m4a"], "1080p vp9 beats 720p h264 (resolution strictly first)")
hi = [vf("vp9-1440",1440,vcodec="vp09.00.50.08",tbr=9000,ext="webm"),
      vf("vp9-2160",2160,vcodec="vp09.00.50.08",tbr=18000,ext="webm"), M4A, OPUS]
ok(pick(hi) == ["vp9-1440+opus"], "only above 1080p -> closest higher (1440p, not 4K)")
uhd = [vf("vp9-2160",2160,vcodec="vp09.00.50.08",tbr=18000,ext="webm"), OPUS]
ok(pick(uhd) == ["vp9-2160+opus"], "only 4K -> still downloads, never fails on ceiling")

# ── 6. >1080p source is resized to 1080p in the upload path (aspect kept) ──
async def scale_test():
    seen = {}
    orig_remux = tv._remux_live
    async def remux_spy(path, status_msg=None, cancel_flag=None, **kw):
        seen["remux"] = seen.get("remux", 0) + 1
        return path
    async def good_upload(client, path, progress_callback=None):
        _, seen["w"], seen["h"] = tv.get_video_info(path)
        return "INPUT"
    class TL:
        async def send_file(self, *a, **k): return types.SimpleNamespace(id=1)
    tv._remux_live, tv.fast_upload, tv.tgclient = remux_spy, good_upload, TL()
    try:
        f = os.path.join(DL, "h_4k.mp4"); gen_video(f, secs=2, w=2560, h=1440)
        r = await tv._finish_upload(f, "T", FakeMsg(), 7, 7)
        ok(r is True and seen.get("h") == 1080 and seen.get("w") == 1920,
           "1440p 16:9 source resized to 1080p before upload")
        ok(seen.get("remux", 0) == 0, "resized file not remuxed again")

        seen.clear()
        f = os.path.join(DL, "h_43.mp4"); gen_video(f, secs=2, w=1600, h=1200)
        r = await tv._finish_upload(f, "T", FakeMsg(), 7, 7)
        ok(r is True and seen.get("h") == 1080 and seen.get("w") == 1440,
           "1200p 4:3 source resized with aspect ratio preserved (1440x1080)")

        seen.clear()
        f = os.path.join(DL, "h_ok.mp4"); gen_video(f, secs=2, w=1920, h=1080)
        r = await tv._finish_upload(f, "T", FakeMsg(), 7, 7)
        ok(r is True and seen.get("h") == 1080 and seen.get("remux") == 1,
           "1080p source untouched (no resize, normal remux)")
    finally:
        tv._remux_live = orig_remux
asyncio.run(scale_test())

print("RESULT:", "FAIL" if ok.failed else "PASS")
sys.exit(1 if ok.failed else 0)
