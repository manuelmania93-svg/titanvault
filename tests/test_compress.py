from harness import *
import subprocess, shutil, stat
src_dir = tempfile.mkdtemp(); src = os.path.join(src_dir, "in.mp4")
shutil.copy("/tmp/fx/www/big.mp4", src)
real_getsize = os.path.getsize
def fake_getsize(p):  # pretend ONLY the source is 3 GiB so compression triggers
    return 3*1024**3 if os.path.abspath(p) == os.path.abspath(src) else real_getsize(p)
os.path.getsize = fake_getsize
async def run():
    # A) happy path with real ffmpeg
    m = FakeMsg(); out = await tv.compress_to_limit(src, m)
    ok(out.endswith("_tg_compressed.mp4") and os.path.exists(out), "A compressed output returned")
    ok(os.path.exists(src), "A original NOT deleted by compress_to_limit")
    d = float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",out]).decode())
    ok(abs(d-12)<0.5, f"A output duration {d:.2f}s matches 12s source")
    os.remove(out)
    # B) ffmpeg exits non-zero
    bad = os.path.join(src_dir, "badff"); open(bad,"w").write("#!/bin/sh\necho boom >&2\nexit 1\n"); os.chmod(bad, 0o755)
    tv.FFMPEG_PATH = bad
    out = await tv.compress_to_limit(src, FakeMsg())
    ok(out == src and os.path.exists(src), "B pass-1 failure -> original returned and kept")
    # C) pass 2 fails after pass 1 ok
    pf = os.path.join(src_dir, "pass2fail")
    open(pf,"w").write("#!/bin/sh\nfor a in \"$@\"; do if [ \"$a\" = 2 ]; then P2=1; fi; done\nif [ -n \"$P2\" ]; then echo partial > \"$(eval echo \\${$#})\"; head -c 2000000 /dev/zero > \"$(eval echo \\${$#})\"; exit 1; fi\nexit 0\n"); os.chmod(pf, 0o755)
    tv.FFMPEG_PATH = pf
    out = await tv.compress_to_limit(src, FakeMsg())
    ok(out == src and os.path.exists(src) and not os.path.exists(os.path.join(src_dir,"in_tg_compressed.mp4")), "C pass-2 nonzero exit -> original kept, bad output removed")
    # D) rc 0 but truncated output (size passes, duration wrong)
    td = os.path.join(src_dir, "trunc")
    open(td,"w").write("#!/bin/sh\nfor a in \"$@\"; do if [ \"$a\" = 2 ]; then P2=1; fi; done\nif [ -n \"$P2\" ]; then OUT=\"$(eval echo \\${$#})\"; /usr/bin/ffmpeg -loglevel error -y -f lavfi -i testsrc=duration=2:size=160x120:rate=25 -f lavfi -i sine=duration=2 -c:v libx264 -pix_fmt yuv420p -c:a aac -b:v 20M \"$OUT\"; head -c 1500000 /dev/urandom >> \"$OUT\"; fi\nexit 0\n"); os.chmod(td, 0o755)
    tv.FFMPEG_PATH = td
    out = await tv.compress_to_limit(src, FakeMsg())
    ok(out == src and os.path.exists(src) and not os.path.exists(os.path.join(src_dir,"in_tg_compressed.mp4")), "D rc=0 but short output (2s of 12s) -> rejected, original kept")
    # E) rc 0, full duration, but audio stream missing while source has audio
    ea = os.path.join(src_dir, "noaudio")
    open(ea,"w").write("#!/bin/sh\nfor a in \"$@\"; do if [ \"$a\" = 2 ]; then P2=1; fi; done\nif [ -n \"$P2\" ]; then OUT=\"$(eval echo \\${$#})\"; /usr/bin/ffmpeg -loglevel error -y -f lavfi -i testsrc2=duration=12:size=960x540:rate=25,noise=alls=30:allf=t -c:v libx264 -b:v 4M -pix_fmt yuv420p -an -f mp4 \"$OUT\"; fi\nexit 0\n"); os.chmod(ea, 0o755)
    tv.FFMPEG_PATH = ea
    out = await tv.compress_to_limit(src, FakeMsg())
    ok(out == src and os.path.exists(src), "E full-length output without audio (source has audio) -> rejected")
    # F) silent source: video-only output is accepted
    silent = os.path.join(src_dir, "silent.mp4")
    subprocess.check_call(["/usr/bin/ffmpeg","-loglevel","error","-y","-i","/tmp/fx/www/big.mp4","-an","-c:v","copy",silent])
    real = os.path.getsize
    os.path.getsize = lambda p: 3*1024**3 if os.path.abspath(p)==os.path.abspath(silent) else real(p)
    tv.FFMPEG_PATH = "ffmpeg"
    out = await tv.compress_to_limit(silent, FakeMsg())
    ok(out.endswith("_tg_compressed.mp4"), "F silent source -> video-only output accepted")

asyncio.run(run())
sys.exit(1 if ok.failed else 0)
