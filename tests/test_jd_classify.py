"""JD failure reasons use a fixed vocabulary from what the socket layer reported (local fake listeners only)."""
import sys, os, socket, threading, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import tv
import requests
failed = []
def ok(c, n):
    print(("PASS: " if c else "FAIL: ") + n)
    if not c: failed.append(n)
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
def reason(url, t=3):
    try: requests.post(url, json={}, timeout=t)
    except Exception as e: return tv._jd_classify(e)
ok(reason(f"http://127.0.0.1:{free_port()}/x").startswith("connection refused"), "closed port -> refused")
ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(5); hold = []
threading.Thread(target=lambda: [hold.append(ls.accept()) for _ in range(5)], daemon=True).start()
ok(reason(f"http://127.0.0.1:{ls.getsockname()[1]}/x", 1) == "connected, but no reply in time", "silent listener -> no reply in time")
rs = socket.socket(); rs.bind(("127.0.0.1", 0)); rs.listen(5)
def rst():
    c, _ = rs.accept(); c.recv(100); c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)); c.close()
threading.Thread(target=rst, daemon=True).start()
ok("closed by the other side" in reason(f"http://127.0.0.1:{rs.getsockname()[1]}/x"), "reset -> closed by the other side")
ok(tv._jd_classify(requests.exceptions.MissingSchema("http://SECRET")) == "configured JD address is not a valid URL", "bad URL, no text leak")
ok("SECRET" not in tv._jd_classify(RuntimeError("http://SECRET")), "unknown error: class name only")
print("FAILED:" if failed else "ALL PASS", failed); sys.exit(1 if failed else 0)
