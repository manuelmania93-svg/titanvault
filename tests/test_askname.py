"""Opt-in "ask for name" after upload + queue count feedback (experimental). Stubs only; no network, no real Telegram."""
from harness import *
import time
tv.init_db()
with tv.get_db() as c: c.execute("DELETE FROM settings"); c.commit()
sent_prompts = []; renames = []
class Bot:
    async def send_message(s, chat, text, **k):
        sent_prompts.append((chat, text, k.get("reply_markup"))); return types.SimpleNamespace(message_id=100 + len(sent_prompts))
    async def copy_message(s, **k): pass
class Msg(FakeMsg):
    chat_id = 7
    def __init__(s): super().__init__(); s.replies = []
    async def edit_text(s, text, **kw): s.edits.append(text)
    async def reply_text(s, text, **kw): s.replies.append(text); return Msg()
    def get_bot(s): return BOT
BOT = Bot()
tv._probe_has_media_stream = lambda p: True
class TL:
    n = 0
    async def send_file(self, *a, **k): TL.n += 1; return types.SimpleNamespace(id=500 + TL.n)
    async def forward_messages(self, *a, **k): pass
tv.tgclient = TL()
async def fast(client, path, progress_callback=None): return "INPUT"
tv.fast_upload = fast
async def fake_rename(bot, target, db_id, name, echo_chat=None): renames.append((db_id, name, echo_chat))
tv._do_rename = fake_rename
def mkfile(n):
    f = os.path.join(tv.ARIA2C_DOWNLOAD_DIR, n); open(f, "wb").write(b"x" * 3000); return f
class Upd:
    def __init__(s, msg, chat=7): s.message = msg; s.effective_user = types.SimpleNamespace(id=chat); s.effective_chat = types.SimpleNamespace(id=chat)
class Ctx:
    def __init__(s, args=None): s.args = args or []; s.bot = BOT
class RMsg(FakeMsg):
    chat_id = 7; photo = document = video = audio = animation = caption = forward_origin = None
    def __init__(s, text, reply_to=None): super().__init__(); s.text = text; s.reply_to_message = reply_to; s.replies = []
    async def reply_text(s, text, **kw): s.replies.append(text); return Msg()
class Q:
    def __init__(s, msg): s.data = "ASKN|skip"; s.from_user = types.SimpleNamespace(id=7); s.message = msg; s.edits = []
    async def answer(s, *a, **k): pass
    async def edit_message_text(s, t, **k): s.edits.append(t)
async def main():
    # default off: an upload asks nothing
    ok(not tv._ask_name_on(7), "default is off")
    m = Msg(); await tv._finish_upload(mkfile("a.bin"), "Title A", m, 7, 7); await asyncio.sleep(0.05)
    ok(not sent_prompts and not tv.pending_names, "toggle off: no naming question, nothing pending")
    # command: on / off / status, persisted, per chat
    m = Msg(); await tv.askname_cmd(Upd(m), Ctx(["on"]))
    ok(tv._ask_name_on(7) and not tv._ask_name_on(8) and "ON" in m.replies[0], "/askname on persists for this chat only")
    m = Msg(); await tv.askname_cmd(Upd(m), Ctx())
    ok("ON" in m.replies[0], "/askname without argument only shows the state")
    # on: prompt after the delivered upload, upload itself not delayed
    t0 = time.time(); m = Msg(); r = await tv._finish_upload(mkfile("b.bin"), "Title B", m, 7, 7); dt = time.time() - t0
    await asyncio.sleep(0.05)
    ok(r and len(sent_prompts) == 1 and "Title B" in sent_prompts[0][1], "toggle on: one naming question after the upload")
    ok(any(b.callback_data == "ASKN|skip" for row in sent_prompts[0][2].inline_keyboard for b in row), "question has a Skip button")
    ok(dt < 1.5 and len(tv.pending_names) == 1, "upload returned without waiting for an answer")
    (chat, pid), fid = next(iter(tv.pending_names.items()))
    # a plain message (no reply) is not taken as a name; a link still queues normally
    tv.link_queues.clear(); tv.link_workers.clear(); tv.pending_links.clear()
    async def nodl(url, m, uid): await asyncio.sleep(0)
    tv._do_dl = nodl
    await tv.on_message(Upd(RMsg("My Name")), Ctx())
    ok(not renames and len(tv.pending_names) == 1, "plain message is not used as a name")
    # reply to the question renames, entry consumed
    await tv.on_message(Upd(RMsg("  Bob's Burgers S01E01\n", reply_to=types.SimpleNamespace(message_id=pid))), Ctx())
    ok(renames == [(fid, "Bob's Burgers S01E01", 7)] and not tv.pending_names, "reply renames with the chosen name, once")
    # a reply that contains a URL is a name, not a new link
    m2 = Msg(); await tv._finish_upload(mkfile("c.bin"), "Title C", m2, 7, 7); await asyncio.sleep(0.05)
    (chat, pid2), fid2 = next(iter(tv.pending_names.items()))
    await tv.on_message(Upd(RMsg("name http://x.test/v", reply_to=types.SimpleNamespace(message_id=pid2))), Ctx())
    ok(renames[-1][0] == fid2 and not tv.link_queues, "reply with a URL inside is a name, not queued as a link")
    # skip clears the pending entry
    m3 = Msg(); await tv._finish_upload(mkfile("d.bin"), "Title D", m3, 7, 7); await asyncio.sleep(0.05)
    (chat, pid3), fid3 = next(iter(tv.pending_names.items()))
    pm = Msg(); pm.chat_id = 7; pm.message_id = pid3; q = Q(pm)
    await tv.button_handler(types.SimpleNamespace(callback_query=q), None)
    ok(not tv.pending_names and q.edits and "kept" in q.edits[0].lower() and len(renames) == 2, "Skip keeps the name and clears the question")
    # two uploads before any answer: both questions stay answerable
    for n in ("e", "f"):
        mm = Msg(); await tv._finish_upload(mkfile(n + ".bin"), "T" + n, mm, 7, 7)
    await asyncio.sleep(0.05)
    ok(len(tv.pending_names) == 2, "several unanswered questions do not overwrite each other")
    # off again: no more questions, other chat never asked
    await tv.askname_cmd(Upd(Msg()), Ctx(["off"])); n0 = len(sent_prompts)
    mm = Msg(); await tv._finish_upload(mkfile("g.bin"), "Tg", mm, 7, 7); await asyncio.sleep(0.05)
    ok(len(sent_prompts) == n0 and not tv._ask_name_on(7), "/askname off: no question again")
asyncio.run(main())

