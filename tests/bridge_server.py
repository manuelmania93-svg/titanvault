import os; os.environ["BRIDGE_PORT"]="8765"
from harness import *
import time, json
tv.init_db()
log=open("/tmp/fx/bridge_events.log","w")
class FakeBot:
    async def send_message(self, chat_id, text, **kw): log.write(json.dumps({"msg":text,"chat":chat_id})+"\n"); log.flush(); return FakeMsg()
async def fake_stream(url, status, uid, chat_id, referer="", title=""): log.write(json.dumps({"stream":url,"chat":uid,"referer":referer,"title":title})+"\n"); log.flush()
tv._run_direct_stream = fake_stream
async def main():
    tv.start_bridge(asyncio.get_running_loop(), FakeBot())
    tv._pair_codes["TESTCODE1"]={"chat_id":555,"exp":time.time()+600,"fails":0}
    open("/tmp/fx/paircode","w").write("TESTCODE1"); 
    while True: await asyncio.sleep(1)
asyncio.run(main())
