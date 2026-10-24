import os, sys, importlib.util, asyncio, types, tempfile
os.environ.update(API_ID="1", API_HASH="x", BOT_TOKEN="1:x", OWNER_ID="42", STORAGE_CHANNEL_ID="-100", SESSION_STRING="")
os.environ.setdefault("DOWNLOAD_DIR", tempfile.mkdtemp(prefix="tvdl_"))
os.environ.setdefault("BRIDGE_PORT", "18765")
os.chdir(tempfile.mkdtemp(prefix="tvcwd_"))
spec = importlib.util.spec_from_file_location("titanvault", os.path.join(os.path.dirname(__file__), "..", "titanvault.py"))
tv = importlib.util.module_from_spec(spec); spec.loader.exec_module(tv)

class FakeMsg:
    chat_id = 7
    def __init__(self): self.edits = []
    async def edit_text(self, text, **kw): self.edits.append(text)
    async def reply_text(self, text, **kw): m = FakeMsg(); self.edits.append(text); return m
    def get_bot(self): return None
    @property
    def last(self): return self.edits[-1] if self.edits else ""

def ok(cond, name):
    print(("PASS " if cond else "FAIL ") + name); 
    if not cond: ok.failed = True
ok.failed = False
