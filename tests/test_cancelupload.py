"""/cancelupload: acts only during the upload step, same effect as the inline button there. Stubs only."""
from harness import *
tv.init_db()
class Msg(FakeMsg):
    chat_id = 7
    def __init__(s): super().__init__(); s.replies = []
    async def edit_text(s, text, **kw): s.edits.append(text)
    async def reply_text(s, text, **kw): s.replies.append(text); return Msg()
    def get_bot(s): return None
class Upd:
    def __init__(s, chat=7): s.message = Msg(); s.effective_user = types.SimpleNamespace(id=chat); s.effective_chat = types.SimpleNamespace(id=chat)
tv._probe_has_media_stream = lambda p: True
sent = []
class TL:
    async def send_file(self, *a, **k): sent.append(1); return types.SimpleNamespace(id=len(sent))
    async def forward_messages(self, *a, **k): pass
tv.tgclient = TL()
state = {}
async def slow_upload(client, path, progress_callback=None):
    state["started"] = True
    for i in range(100): await progress_callback(i, 100); await asyncio.sleep(0.05)
    return "INPUT"
tv.fast_upload = slow_upload
def mkfile(n):
    f = os.path.join(tv.ARIA2C_DOWNLOAD_DIR, n); open(f, "wb").write(b"x" * 3000); return f
async def main():
    # not uploading: says so and touches nothing
    tv.link_queues[7] = asyncio.Queue(); tv.link_queues[7].put_nowait("http://x/next"); tv.pending_links[7] = ["http://x/next"]
    async def idle(): await asyncio.sleep(5)
    t_idle = asyncio.create_task(idle()); tv.dl_tasks[7] = t_idle
    u = Upd(); await tv.cancelupload_cmd(u, None)
    ok("No upload is running" in u.message.replies[0] and not t_idle.cancelled() and tv.link_queues[7].qsize() == 1, "outside the upload step: message only, download and queue untouched")
    t_idle.cancel(); tv.dl_tasks.pop(7, None)
    # during upload: cancelled, file kept, 7 only
    f = mkfile("a.bin"); m = Msg(); state.clear()
    tv.link_queues[8] = asyncio.Queue(); tv.link_queues[8].put_nowait("http://x/other")
    t = asyncio.create_task(tv._finish_upload(f, "A", m, 7, 7)); tv.dl_tasks[7] = t
    for _ in range(40):
        if state.get("started"): break
        await asyncio.sleep(0.05)
    ok(7 in tv._uploading, "chat is marked as uploading during Step 4")
    u = Upd(); await tv.cancelupload_cmd(u, None)
    try: await t
    except asyncio.CancelledError: pass
    ok(t.cancelled() and not sent, "command cancels the running upload; nothing reached the storage channel")
    ok(os.path.exists(f) and len(tv._kept_rows()) == 1, "downloaded file kept and registered")
    ok("Cancelling" in u.message.replies[0], "user is told what happens")
    ok(7 not in tv._uploading, "uploading mark cleared afterwards")
    ok(tv.link_queues[7].qsize() == 0 and tv.link_queues[8].qsize() == 1, "same as the button: this chat's waiting links cleared, other chats untouched")
    # a completed upload also clears the mark
    state.clear(); f2 = mkfile("b.bin")
    async def quick(client, path, progress_callback=None): return "INPUT"
    tv.fast_upload = quick
    r = await tv._finish_upload(f2, "B", Msg(), 7, 7)
    ok(r and 7 not in tv._uploading, "normal upload unaffected, mark cleared")
    # command and menu are wired
    import inspect
    src = inspect.getsource(tv)
    ok('CommandHandler("cancelupload"' in src and 'BotCommand("cancelupload"' in src, "command handler and menu entry registered")
    ok('callback_data=f"DL_CANCEL|{uid}"' in src, "inline Cancel button still present")
asyncio.run(main())
