from harness import *
import shutil
tv.init_db()
class Q:   # fake callback query
    def __init__(s, chat): s.message=types.SimpleNamespace(chat_id=chat, reply_text=None); s.edits=[]
    async def edit_message_text(s, t, **k): s.edits.append(t)
class StatusMsg(FakeMsg):
    chat_id = 7
    def __init__(s): super().__init__(); s.replies=[]
    async def reply_text(s, text, **kw): s.replies.append((text, kw)); return StatusMsg()
    def get_bot(s): return None
async def boom(*a, **k): raise RuntimeError("telegram down")
async def run():
    tv.fast_upload = boom
    f = os.path.join(tv.ARIA2C_DOWNLOAD_DIR, "orig.bin"); open(f,"wb").write(b"x"*5000)
    m = StatusMsg()
    try: await tv._finish_upload(f, "Orig", m, 7, 7)
    except RuntimeError: pass
    ok(os.path.exists(f), "failed upload: original kept")
    rows = tv._kept_rows(); ok(len(rows)==1 and rows[0][1]==f, "kept file registered")
    ok(m.replies and "kept" in m.replies[0][0].lower() and m.replies[0][1].get("reply_markup"), "user gets Retry/Delete buttons")
    ok(f in tv._KEPT_SOURCES, "caller cleanup is told to skip it")
    # backpressure
    tv.KEEP_CAP_BYTES = 4000
    ok(tv._kept_blocked(), "cap reached -> new jobs blocked")
    msgs=[]; 
    async def reply(t): msgs.append(t)
    await tv._kept_block_notice(reply); ok("Nothing was deleted" in msgs[0], "block notice says nothing auto-deleted")
    sm = StatusMsg(); await tv._do_dl("http://127.0.0.1:8099/clip.mp4", sm, 7)
    ok(any("paused" in r[0] for r in sm.replies), "_do_dl refuses new job while cap is reached")
    ok(os.path.exists(f), "block does not delete the kept file")
    # Delete button targets only registered files, only own chat
    rid = rows[0][0]
    q = Q(999); await tv.kept_button(q, f"KEPT_DEL|{rid}"); ok(os.path.exists(f), "other chat cannot delete it")
    # row was consumed by that attempt; re-register for the next checks
    rid = tv._kept_register(f, "Orig", 7)
    q = Q(7); await tv.kept_button(q, f"KEPT_DEL|{rid}"); ok(not os.path.exists(f) and "Deleted" in q.edits[-1], "Delete removes the kept file")
    # path outside the download folder is never touched
    outside = os.path.join(tempfile.mkdtemp(), "keep.bin"); open(outside,"wb").write(b"y")
    with tv.get_db() as c: cur=c.execute("INSERT INTO kept_jobs (path,title,chat_id,size) VALUES (?,?,?,?)",(outside,"x",7,1)); c.commit(); rid2=cur.lastrowid
    q = Q(7); await tv.kept_button(q, f"KEPT_DEL|{rid2}"); ok(os.path.exists(outside) and "no longer" in q.edits[-1], "file outside download folder is never deleted")
    ok(not tv._kept_blocked(), "cap clears after Delete")
    # Retry succeeds -> file removed after confirmed upload
    f2 = os.path.join(tv.ARIA2C_DOWNLOAD_DIR, "orig2.bin"); open(f2,"wb").write(b"z"*3000)
    sent = {}
    async def good_upload(client, path, progress_callback=None): sent["p"]=path; return "INPUT"
    tv.fast_upload = good_upload
    class TL:
        async def send_file(self, *a, **k): return types.SimpleNamespace(id=1)
        async def forward_messages(self,*a,**k): pass
    tv.tgclient = TL()
    rid3 = tv._kept_register(f2, "Two", 7)
    q = Q(7); q.message = StatusMsg(); q.message.chat_id = 7
    await tv.kept_button(q, f"KEPT_RETRY|{rid3}"); await asyncio.sleep(0.5)
    ok(sent.get("p")==f2 and not os.path.exists(f2), "Retry uploads and then removes the original")
asyncio.run(run())
sys.exit(1 if ok.failed else 0)
