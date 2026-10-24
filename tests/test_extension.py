import json, time, tempfile, sys
from playwright.sync_api import sync_playwright
EXT="/tmp/fx/ext_test"; fails=[]
def ok(c,n):
    print(("PASS " if c else "FAIL ")+n); 
    if not c: fails.append(n)
with sync_playwright() as p:
    ctx=p.chromium.launch_persistent_context(tempfile.mkdtemp(), headless=False,
        args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--disable-features=DisableLoadExtensionCommandLineSwitch","--no-sandbox"],
        viewport={"width":900,"height":700})
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
    eid = sw.url.split("/")[2]; print("extension id", eid)
    page=ctx.new_page(); page.goto("http://127.0.0.1:8100/page.html"); page.wait_for_timeout(1500); page.evaluate("fetch('http://localhost:8099/hls/index.m3u8?sig=HLSSECRET',{mode:'no-cors'}).catch(()=>{})"); page.wait_for_timeout(2000)
    tabid = sw.evaluate("chrome.tabs.query({url:'http://127.0.0.1:8100/*'}).then(t=>t[0].id)")
    det = sw.evaluate("chrome.storage.session.get('det').then(d=>d.det)")
    urls=[x['url'] for x in det.get(str(tabid),[])]
    print(urls)
    ok(any("clip3.mp4" in u for u in urls), "detected cross-origin mp4 (video element on 127.0.0.1:8100, media from localhost:8099)")
    ok(any("index.m3u8" in u for u in urls), "detected cross-origin m3u8 fetch made by page script after load")
    pop=ctx.new_page(); pop.on("console", lambda m: print("POPUP-CONSOLE", m.text)); pop.goto(f"chrome-extension://{eid}/popup.html?tab={tabid}"); pop.wait_for_timeout(1000)
    pop.screenshot(path="/tmp/fx/shot_unpaired.png")
    ok("Pair" in pop.inner_text("body") and pop.is_visible("#pairbox"), "popup shows pairing box when unpaired")
    ok(all(b.is_disabled() for b in pop.query_selector_all("button:has-text('Send')")), "send buttons disabled until paired")
    code=open("/tmp/fx/paircode").read().strip()
    pop.fill("#code","WRONG"); pop.click("#pair"); pop.wait_for_timeout(600)
    ok("failed" in pop.inner_text("#status").lower(), "wrong code -> 'Pairing failed' shown")
    pop.fill("#code",code); pop.click("#pair"); pop.wait_for_timeout(900)
    st=pop.inner_text("#status"); print("status:",st)
    ok("Paired" in st, "pairing with valid code works")
    pop.reload(); pop.wait_for_timeout(900)
    pop.screenshot(path="/tmp/fx/shot_paired.png")
    btns=pop.query_selector_all("button:has-text('Send')")
    ok(len(btns)>=2 and not btns[0].is_disabled(), f"{len(btns)} detected streams listed with enabled Send buttons")
    shown=pop.inner_text("#list"); ok("SECRETTOKEN" not in shown and "HLSSECRET" not in shown, "popup shows URLs without query strings")
    btns[0].click(); pop.wait_for_timeout(900)
    ok("Sent" in pop.inner_text("#status"), "Send -> 'Sent. Check Telegram.'")
    pop.screenshot(path="/tmp/fx/shot_sent.png")
    ctx.close()
ev=[json.loads(l) for l in open("/tmp/fx/bridge_events.log")]
print(ev)
ok(any("stream" in e and e["chat"]==555 and e["referer"].startswith("http://127.0.0.1:8100/page.html") for e in ev), "bot received the stream job for the paired chat with the page as referer")
ok(not any("SECRETTOKEN" in e.get("msg","") or "HLSSECRET" in e.get("msg","") for e in ev), "bot chat messages did not echo the query string")
sys.exit(1 if fails else 0)
