"""Link queue (experimental): multi-URL messages, queue-instead-of-cancel, duplicates, Cancel.
Fully stubbed: no network, no Telegram, no media URLs."""
import os, sys, asyncio, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("API_ID", "1"); os.environ.setdefault("API_HASH", "x")
os.environ.setdefault("BOT_TOKEN", "x"); os.environ.setdefault("STORAGE_CHANNEL_ID", "-1")
os.environ.setdefault("OWNER_ID", "1"); os.environ.setdefault("SESSION_STRING", "")
import titanvault as tv

CALLS, DONE, CANCELLED, REPLIES = [], [], [], []

class FakeMsg:
    chat_id = 7
    photo = document = video = audio = animation = caption = forward_origin = None
    def __init__(self, text): self.text = text
    async def reply_text(self, text, **kw): REPLIES.append(text); return types.SimpleNamespace(id=len(REPLIES))

class FakeUpdate:
    def __init__(self, msg, uid=7):
        self.message = msg
        self.effective_user = types.SimpleNamespace(id=uid)
        self.effective_chat = types.SimpleNamespace(id=uid)

EVENTS = {}
async def fake_dl(url, reply_msg, uid):
    CALLS.append(url)
    try:
        await EVENTS.setdefault(url, asyncio.Event()).wait()
        DONE.append(url)
    except asyncio.CancelledError:
        CANCELLED.append(url); raise

async def send(text, uid=7):
    msg = FakeMsg(text)
    await tv.on_message(FakeUpdate(msg, uid), None)
    await asyncio.sleep(0.05)   # let the worker reach its first await
    return msg

def reset():
    for d in (tv.link_queues, tv.link_workers, tv.pending_links, tv.dl_tasks, tv.dl_cancel_flags): d.clear()
    CALLS.clear(); DONE.clear(); CANCELLED.clear(); REPLIES.clear(); EVENTS.clear()

async def finish_worker(uid=7):
    w = tv.link_workers.get(uid)
    if w: await asyncio.wait_for(w, 5)

ok = lambda cond, name: print(("PASS" if cond else "FAIL") + ": " + name) or (cond or failed.append(name))
failed = []

tv._do_dl = fake_dl
tv.kept_button = lambda q, data: asyncio.sleep(0, result=False)

async def main():
    A, B, C = "https://site-a.test/v/1", "https://site-b.test/v/2", "https://site-c.test/v/3"

    # 1. two URLs in one message: both run, FIFO, second noted as queued
    reset()
    await send(f"{A} {B}")
    ok(CALLS == [A], "multi-URL message: first starts immediately")
    ok(any("1 link queued" in r for r in REPLIES), "multi-URL message: second announced as queued")
    EVENTS[A].set(); await asyncio.sleep(0.05)
    ok(CALLS == [A, B], "multi-URL message: second starts after first finishes")
    EVENTS[B].set(); await finish_worker()
    ok(DONE == [A, B], "multi-URL message: both completed in order")

    # 1b. queue feedback: the note states how many are waiting in total (active one counted separately)
    reset()
    await send(f"{A} {B} {C}")
    ok(any("2 links queued" in r and "Waiting now: 2" in r and "plus 1 downloading" in r for r in REPLIES), "3 links at once: 2 waiting reported")
    REPLIES.clear(); await send("https://site-d.test/v/4")
    ok(any("1 link queued" in r and "Waiting now: 3" in r for r in REPLIES), "one more while active: total waiting is 3, not just 1")
    for u in (A, B, C): EVENTS.setdefault(u, asyncio.Event()).set()
    EVENTS.setdefault("https://site-d.test/v/4", asyncio.Event()).set(); await finish_worker()

    # 2. second message while active: queued, first NOT cancelled
    reset()
    await send(A)
    await send(B)
    ok(CALLS == [A] and not CANCELLED, "new link while active: first not cancelled")
    ok(any("queued" in r for r in REPLIES), "new link while active: queue note sent")
    EVENTS[A].set(); await asyncio.sleep(0.05)
    ok(CALLS == [A, B], "new link while active: second runs afterwards")
    EVENTS[B].set(); await finish_worker()
    ok(DONE == [A, B] and not CANCELLED, "both complete, zero cancellations")

    # 3. duplicate of active URL: skipped, no restart
    reset()
    await send(A)
    await send(A)
    ok(CALLS == [A], "same link twice: no restart")
    ok(any("duplicate" in r for r in REPLIES), "same link twice: duplicate note")
    EVENTS[A].set(); await finish_worker()
    ok(DONE == [A] and not CANCELLED, "same link twice: single uninterrupted run")

    # 4. Cancel button: active cancelled, queue cleared
    reset()
    await send(A)
    await send(B)
    q = types.SimpleNamespace(data="DL_CANCEL|7", from_user=types.SimpleNamespace(id=7),
                              answer=lambda **kw: asyncio.sleep(0))
    await tv.button_handler(types.SimpleNamespace(callback_query=q), None)
    await asyncio.sleep(0.1)
    ok(CANCELLED == [A], "cancel: active download cancelled")
    ok(CALLS == [A], "cancel: queued link never starts")
    ok(tv.link_queues[7].empty(), "cancel: queue cleared")

    # 5. text without URL: untouched
    reset()
    await send("just some words")
    ok(not CALLS and not REPLIES, "plain text: no download, no reply")

    # 6. embedded URLs + trailing punctuation stripped
    reset()
    await send(f"watch {A}. and {B}!")
    await asyncio.sleep(0.05)
    ok(CALLS == [A, B] or CALLS == [A], "embedded URLs found")
    ok(all(not u.endswith((".", "!")) for u in CALLS), "trailing punctuation stripped")
    EVENTS[A].set(); await asyncio.sleep(0.05); EVENTS[B].set(); await finish_worker()
    ok(DONE == [A, B], "embedded URLs: both downloaded")

asyncio.run(main())
print("RESULT:", "FAIL " + str(failed) if failed else "PASS")
