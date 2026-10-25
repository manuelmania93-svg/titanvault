"""JD connection classification + /jdtest self-check against local fake servers. No JDownloader, no links."""
import sys, os, socket, threading, json, asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import tv
import requests
failed = []
def ok(c, n):
    print(("PASS: " if c else "FAIL: ") + n)
    if not c: failed.append(n)
tv.JD_MODE = "local"
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
# 1) closed port -> refused
tv.JD_LOCAL_URL = f"http://127.0.0.1:{free_port()}"
try: requests.post(tv.JD_LOCAL_URL + "/x", json={}, timeout=3)
except Exception as e: why = tv._jd_classify(e)
ok(why.startswith("connection refused"), "closed port classified as refused: " + why)
out = tv._jd_selftest()
ok("TCP 127.0.0.1" in out and "refused" in out and "API call: connection refused" in out, "selftest on closed port reports refused at TCP/HTTP/API level")
# 2) fake JD that answers like the API
class H(BaseHTTPRequestHandler):
    def log_message(s, *a): pass
    def do_GET(s): s.send_response(404); s.end_headers()
    def do_POST(s):
        s.rfile.read(int(s.headers.get("Content-Length") or 0))
        b = json.dumps({"data": "IDLE"}).encode() if MODE == "ok" else b"<html>nope</html>"
        s.send_response(200); s.send_header("Content-Length", str(len(b))); s.end_headers(); s.wfile.write(b)
MODE = "ok"
srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
tv.JD_LOCAL_URL = f"http://127.0.0.1:{srv.server_address[1]}"
out = tv._jd_selftest()
ok("TCP 127.0.0.1" in out and "accepted" in out and "HTTP GET /: status 404" in out and "API call: OK" in out, "working fake API reported OK")
MODE = "html"; out = tv._jd_selftest()
ok("not JSON" in out, "non-JSON reply reported as such")
# 3) listener that accepts and never answers -> read timeout class
ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(5); hold = []
threading.Thread(target=lambda: [hold.append(ls.accept()) for _ in range(5)], daemon=True).start()
try: requests.post(f"http://127.0.0.1:{ls.getsockname()[1]}/x", json={}, timeout=1)
except Exception as e: why = tv._jd_classify(e)
ok(why == "connected, but no reply in time", "silent listener -> " + why)
# 4) reset
rs = socket.socket(); rs.bind(("127.0.0.1", 0)); rs.listen(5)
def rst():
    c, _ = rs.accept(); c.recv(100)
    import struct; c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)); c.close()
threading.Thread(target=rst, daemon=True).start()
try: requests.post(f"http://127.0.0.1:{rs.getsockname()[1]}/x", json={}, timeout=3)
except Exception as e: why = tv._jd_classify(e)
ok("closed by the other side" in why, "reset -> " + why)
ok(tv._jd_classify(requests.exceptions.MissingSchema("http://SECRET")) == "configured JD address is not a valid URL", "bad URL class, no text leak")
tv.JD_MODE = ""; ok("applies to JD_MODE=local only" in tv._jd_selftest(), "non-local mode says so")
print("FAILED:" if failed else "ALL PASS", failed); sys.exit(1 if failed else 0)
