"""Cancel button during convert/upload (experimental). Real asyncio tasks and the real DL_CANCEL button handler;
Telegram transfer and the storage channel are stubs. Real ffmpeg for the convert case."""
from harness import *
import shutil, subprocess, time
tv.init_db()
from telegram import InlineKeyboardMarkup

class Msg(FakeMsg):
    chat_id = 7
    def __init__(s): super().__init__(); s.kbs=[]; s.replies=[]
    async def edit_text(s, text, **kw): s.edits.append(text); s.kbs.append(kw.get("reply_markup"))
    async def reply_text(s, text, **kw): s.replies.append((text, kw)); return Msg()
    def get_bot(s): return None
class Q:
    def __init__(s, chat, data, msg=None):
        s.data=data; s.from_user=types.SimpleNamespace(id=chat); s.message=msg or Msg(); s.message.chat_id=chat; s.edits=[]
    async def answer(s,*a,**k): pass
    async def edit_message_text(s,t,**k): s.edits.append(t)
def cancel_cb(m): return any(k is not None and any(b.callback_data=="DL_CANCEL|7" for r in k.inline_keyboard for b in r) for k in m.kbs[-1:])
def mkfile(name, n=5000):
    f=os.path.join(tv.ARIA2C_DOWNLOAD_DIR,name); open(f,"wb").write(b"x"*n); return f
def mkvideo(name, secs=3, size="320x240"):
    f=os.path.join(tv.ARIA2C_DOWNLOAD_DIR,name)
    subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i",f"testsrc2=size={size}:rate=25","-f","lavfi","-i","sine","-t",str(secs),"-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p","-c:a","aac",f],check=True)
    return f
tv._probe_has_media_stream = lambda p: True
sent=[]
class TL:
    mode="fast"
    async def send_file(self,*a,**k):
        if TL.mode=="slow": await asyncio.sleep(0.8)
        if TL.mode=="hang": await asyncio.sleep(100)
        sent.append(1); return types.SimpleNamespace(id=len(sent))
    async def forward_messages(self,*a,**k): pass
    recent=[]; fail=False
    async def get_messages(self,*a,**k):
        if TL.fail: raise ConnectionError("down")
        return list(TL.recent)
tv.tgclient = TL()
def stored(title,size,id=900):
    import datetime
    return types.SimpleNamespace(id=id,message=title,file=types.SimpleNamespace(size=size),date=datetime.datetime.now(datetime.timezone.utc))

async def run():
    tv._SEND_SETTLE_SECS = 1
    # 1. cancel while bytes are uploading
    state={}
    async def slow_upload(client, path, progress_callback=None):
        state["started"]=True
        for i in range(100):
            await progress_callback(i, 100); await asyncio.sleep(0.05)
        return "INPUT"
    tv.fast_upload = slow_upload
    f=mkfile("a.bin"); m=Msg()
    tv.link_queues[7]=asyncio.Queue(); tv.link_queues[7].put_nowait("http://x/next"); tv.link_queues[8]=asyncio.Queue(); tv.link_queues[8].put_nowait("http://x/other")
    t=asyncio.create_task(tv._finish_upload(f,"A",m,7,7)); tv.dl_tasks[7]=t
    for _ in range(40):
        if state.get("started"): break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    ok(cancel_cb(m), "upload progress edits keep the Cancel button")
    await tv.button_handler(types.SimpleNamespace(callback_query=Q(7,"DL_CANCEL|7")), None)
    try: await t
    except asyncio.CancelledError: pass
    ok(t.cancelled(), "task cancelled during upload")
    ok(not sent, "nothing was sent to the storage channel")
    ok(os.path.exists(f), "downloaded file kept (not deleted)")
    ok(len(tv._kept_rows())==1, "kept file registered once")
    ok(m.replies and "cancelled" in m.replies[0][0].lower() and "kept" in m.replies[0][0].lower() and m.replies[0][1].get("reply_markup"), "user told: upload cancelled, file kept, Retry/Delete buttons")
    with tv.get_db() as c: n=c.execute("SELECT count(*) FROM files").fetchone()[0]
    ok(n==0, "no vault row created")
    ok(tv.link_queues[7].empty() and not tv.link_queues[8].empty(), "own queue cleared, other chat's queue untouched")
    ok(tv._msg_key(m) not in tv._status_kb, "no stale keyboard state left")
    # 2. cancel during convert: ffmpeg child is killed
    tv.fast_upload = slow_upload
    v=mkvideo("v.mp4", 25, "1280x720"); m2=Msg(); tv.user_convert[7]={"vcodec":"libx264","crf":18,"scale":1080,"container":"mp4"}
    t=asyncio.create_task(tv._finish_upload(v,"V",m2,7,7)); tv.dl_tasks[7]=t
    for _ in range(60):
        await asyncio.sleep(0.1)
        if subprocess.run(["pgrep","-f","ffmpeg.*"+os.path.basename(v)],capture_output=True).stdout: break
    ok(subprocess.run(["pgrep","-f","ffmpeg.*"+os.path.basename(v)],capture_output=True).stdout!=b"", "ffmpeg convert is running")
    await tv.button_handler(types.SimpleNamespace(callback_query=Q(7,"DL_CANCEL|7")), None)
    try: await t
    except asyncio.CancelledError: pass
    await asyncio.sleep(0.5)
    ok(t.cancelled(), "task cancelled during convert")
    ok(subprocess.run(["pgrep","-f","ffmpeg.*"+os.path.basename(v)],capture_output=True).stdout==b"", "ffmpeg process is gone")
    ok(os.path.exists(v), "source kept after cancelled convert")
    tv.user_convert.pop(7,None)
    # 3. cancel while the final send request is in flight and then completes
    async def quick_upload(client,path,progress_callback=None): return "INPUT"
    tv.fast_upload=quick_upload; TL.mode="slow"; sent.clear()
    f3=mkfile("c.bin"); m3=Msg()
    t=asyncio.create_task(tv._finish_upload(f3,"C",m3,7,7)); tv.dl_tasks[7]=t
    await asyncio.sleep(0.5)
    await tv.button_handler(types.SimpleNamespace(callback_query=Q(7,"DL_CANCEL|7")), None)
    try: res=await t
    except asyncio.CancelledError: res="cancelled"
    ok(res is True and len(sent)==1, "send already in flight: outcome recorded, not lost")
    with tv.get_db() as c: n=c.execute("SELECT count(*) FROM files").fetchone()[0]
    ok(n==1, "vault row recorded for the completed upload")
    ok(any("already received" in e for e in m3.edits), "user told the cancel came too late")
    ok(not os.path.exists(f3), "original removed after confirmed upload (existing rule)")
    ok(m3.kbs[-1] is None, "completed message has no Cancel button")
    async def cancel_hung(name, title, recent=None, fail=False):
        TL.mode="hang"; sent.clear(); TL.recent=recent if recent is not None else []; TL.fail=fail
        f=mkfile(name); m=Msg()
        t=asyncio.create_task(tv._finish_upload(f,title,m,7,7)); tv.dl_tasks[7]=t
        await asyncio.sleep(0.4)
        await tv.button_handler(types.SimpleNamespace(callback_query=Q(7,"DL_CANCEL|7")), None)
        try: await t
        except asyncio.CancelledError: pass
        rid=[r[0] for r in tv._kept_rows() if r[1]==f][-1]
        return f,m,rid,t
    def vault_rows():
        with tv.get_db() as c: return c.execute("SELECT count(*) FROM files").fetchone()[0]
    def buttons(m): return [b.callback_data.split("|")[0] for r in m.replies[0][1]["reply_markup"].inline_keyboard for b in r]
    # 4. cancel while the send request never answers; vault shows nothing
    n0=vault_rows(); f4,m4,rid4,t=await cancel_hung("d.bin","D")
    ok(t.cancelled() and os.path.exists(f4), "unanswered send: cancelled, file kept")
    ok(tv._kept_state(rid4)=="notfound" and "cannot prove" in m4.replies[0][0], "unconfirmed send is marked outcome-unknown (persisted in the database) and says so")
    ok(buttons(m4)==["KEPT_FORCE","KEPT_DEL"], "buttons: Upload anyway / Delete, no plain Retry")
    TL.mode="fast"; sent.clear()
    q=Q(7,"KEPT_RETRY|%d"%rid4); await tv.kept_button(q,"KEPT_RETRY|%d"%rid4); await asyncio.sleep(0.3)
    ok(not sent and os.path.exists(f4) and tv._kept_state(rid4)=="notfound" and "duplicate" in q.edits[-1], "a plain Retry press is refused, nothing uploaded, file and marker stay")
    # 4a. a caption+size lookalike is a hint only: nothing recorded, nothing deleted
    f4a,m4a,rid4a,t=await cancel_hung("g.bin","G",recent=[stored("G",os.path.getsize(mkfile("g_probe.bin")),901)])
    ok(tv._kept_state(rid4a)=="candidate" and os.path.exists(f4a) and vault_rows()==n0, "lookalike message: marked candidate, not recorded, original not deleted")
    ok("cannot be sure" in m4a.replies[0][0], "user told it is only a likely match")
    # 4c. vault unreadable
    f4c,m4c,rid4c,t=await cancel_hung("h.bin","H",fail=True)
    ok(tv._kept_state(rid4c)=="unknown" and "could not check" in m4c.replies[0][0], "unreadable vault: marked unknown, user told")
    # restart: all state lives in the database, nothing in memory
    tv._KEPT_SOURCES.clear()
    ok(tv._kept_state(rid4c)=="unknown" and tv._kept_state(rid4)=="notfound", "marker survives a restart (stored in kept_jobs)")
    TL.mode="fast"; sent.clear(); tv.fast_upload=quick_upload
    q=Q(7,"KEPT_RETRY|%d"%rid4c); await tv.kept_button(q,"KEPT_RETRY|%d"%rid4c)
    ok(not sent and "duplicate" in q.edits[-1], "after restart a plain Retry is still refused")
    q=Q(7,"KEPT_FORCE|%d"%rid4c); await tv.kept_button(q,"KEPT_FORCE|%d"%rid4c); await asyncio.sleep(0.5)
    ok(len(sent)==1 and not os.path.exists(f4c), "explicit Upload anyway uploads, then removes the original after confirmation")
    q=Q(7,"KEPT_DEL|%d"%rid4); await tv.kept_button(q,"KEPT_DEL|%d"%rid4)
    ok(not os.path.exists(f4) and "Deleted" in q.edits[-1], "Delete works on an outcome-unknown file")
    # migration: an old kept_jobs table without the column
    import sqlite3
    old=os.getcwd(); d=tempfile.mkdtemp(); os.chdir(d)
    c=sqlite3.connect("vault.db"); c.execute("CREATE TABLE kept_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, title TEXT, chat_id INTEGER, size INTEGER, added_at TEXT DEFAULT (datetime('now')))")
    c.execute("INSERT INTO kept_jobs (path,title,chat_id,size) VALUES ('p','t',7,1)"); c.commit(); c.close()
    tv.init_db(); tv.init_db()
    c=sqlite3.connect("vault.db"); st=c.execute("SELECT state FROM kept_jobs").fetchone()[0]; c.close(); os.chdir(old)
    ok(st in ("",None), "old kept_jobs table is migrated; existing rows read as ordinary (and init_db is repeatable)")
    # 5. normal completion: Cancel button disappears, nothing kept
    TL.mode="fast"; sent.clear(); f5=mkfile("e.bin"); m5=Msg(); rows0=len(tv._kept_rows())
    r=await tv._finish_upload(f5,"E",m5,7,7)
    ok(r is True and m5.kbs[-1] is None and len(tv._kept_rows())==rows0, "normal upload unaffected, no leftover keyboard")
    ok(tv._msg_key(m5) not in tv._status_kb, "keyboard state cleared after success")
    # 6. a retry from the kept list can be cancelled too
    tv.fast_upload=slow_upload; f6=mkfile("f.bin"); rid=tv._kept_register(f6,"F",7)
    q=Q(7,"KEPT_RETRY|%d"%rid); await tv.kept_button(q, "KEPT_RETRY|%d"%rid)
    await asyncio.sleep(0.4)
    ok(7 in tv.dl_tasks and not tv.dl_tasks[7].done(), "retry task is registered for Cancel")
    t6=tv.dl_tasks[7]
    await tv.button_handler(types.SimpleNamespace(callback_query=Q(7,"DL_CANCEL|7")), None)
    await asyncio.sleep(0.5)
    ok(t6.done() and os.path.exists(f6), "cancelling a retry keeps the file")
    ok(any("cancelled" in e.lower() for e in q.message.edits), "retry status says cancelled")
asyncio.run(run())
sys.exit(1 if ok.failed else 0)
