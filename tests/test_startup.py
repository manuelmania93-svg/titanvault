from harness import *
import urllib.request
added=[]; cmds=[]
class FakeUpdater:
    async def start_polling(self,*a,**k): pass
    async def stop(self): pass
class FakeBotApi:
    async def set_my_commands(self, c): cmds.extend(x.command for x in c)
    async def send_message(self,*a,**k): pass
class FakeApp:
    bot=FakeBotApi(); updater=FakeUpdater()
    def add_error_handler(s,h): pass
    def add_handler(s,h): added.append(h)
    async def __aenter__(s): return s
    async def __aexit__(s,*a): pass
    async def start(s): pass
    async def stop(s): pass
class FakeBuilder:
    def token(s,t): return s
    def read_timeout(s,x): return s
    def write_timeout(s,x): return s
    def connect_timeout(s,x): return s
    def build(s): return FakeApp()
tv.ApplicationBuilder = FakeBuilder
class FakeTL:
    session=types.SimpleNamespace(dc_id=2)
    async def start(self,**k): pass
    async def get_me(self): return types.SimpleNamespace(first_name="t", username="t")
    async def run_until_disconnected(self):
        # the bridge must be up by now
        import urllib.error
        try: urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{tv.BRIDGE_PORT}/ping", headers={"Origin":"chrome-extension://x"}), timeout=3)
        except urllib.error.HTTPError as e: print("bridge alive, status", e.code); ok(e.code==401, "bridge reachable at startup (401 = needs token)")
    async def disconnect(self): pass
tv.tgclient=FakeTL(); tv.tgbot=FakeTL()
asyncio.run(tv.main())
names=set()
for h in added:
    if hasattr(h,"commands"): names |= set(h.commands)
for c in ["stream","kept","pair","unpair","dl","mp3","files"]: ok(c in names, f"handler wired: /{c}")
for c in ["stream","kept","pair","unpair"]: ok(c in cmds, f"command registered in menu: /{c}")
ok(any(type(h).__name__=="CallbackQueryHandler" for h in added), "callback handler wired")
sys.exit(1 if ok.failed else 0)
