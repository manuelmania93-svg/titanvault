const BRIDGE = "http://127.0.0.1:8765";
const $ = id => document.getElementById(id);
const say = t => { $("status").textContent = t; };

function shortUrl(u) {
  try { const x = new URL(u); return x.host + x.pathname + (x.search ? "?…" : ""); } catch (e) { return u.slice(0, 60); }
}
async function token() { return (await chrome.storage.local.get("token")).token || ""; }

async function api(path, body, tok) {
  const r = await fetch(BRIDGE + path, {
    method: body ? "POST" : "GET",
    headers: Object.assign({ "Content-Type": "application/json" }, tok ? { Authorization: "Bearer " + tok } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  let j = {}; try { j = await r.json(); } catch (e) {}
  return { status: r.status, body: j };
}

async function render() {
  const tok = await token();
  const granted = await chrome.permissions.contains({ origins: ["http://*/*", "https://*/*"] });
  $("pairbox").hidden = !!tok;
  $("permbox").hidden = granted;
  if (tok) {
    try { const p = await api("/ping", null, tok); console.log("ping", p.status, JSON.stringify(p.body)); say(p.status === 200 ? "Paired with your bot." : "Pairing invalid. Pair again."); if (p.status === 401) { await chrome.storage.local.remove("token"); $("pairbox").hidden = false; } }
    catch (e) { say("Bot not reachable on 127.0.0.1:8765 (is TitanVault running?)"); }
  }
  const forced = new URLSearchParams(location.search).get("tab");   // lets the popup page be opened in a tab for testing
  const [tab] = forced ? [await chrome.tabs.get(parseInt(forced, 10))] : await chrome.tabs.query({ active: true, currentWindow: true });
  const det = ((await chrome.storage.session.get("det")).det || {})[tab ? tab.id : -1] || [];
  const list = $("list"); list.textContent = "";
  if (granted && !det.length) { const d = document.createElement("div"); d.className = "small"; d.textContent = "No streams seen on this tab yet. Press play, then reopen."; list.appendChild(d); }
  for (const item of det) {
    const row = document.createElement("div"); row.className = "row";
    const t = document.createElement("div"); t.textContent = shortUrl(item.url); row.appendChild(t);
    const b = document.createElement("button"); b.textContent = "Send to TitanVault"; b.disabled = !tok;
    b.onclick = async () => {
      b.disabled = true;
      try {
        const r = await api("/send", { url: item.url, page_url: item.page, title: (tab && tab.title) || "" }, tok);
        say(r.status === 202 ? "Sent. Check Telegram." : "Failed: " + (r.body.error || r.status));
      } catch (e) { say("Bot not reachable."); }
      b.disabled = false;
    };
    row.appendChild(b); list.appendChild(row);
  }
}

$("perm").onclick = async () => {
  // must be called directly from the click (user gesture)
  const g = await chrome.permissions.request({ origins: ["http://*/*", "https://*/*"] });
  say(g ? "Detection enabled. Reload the video page." : "Permission not granted.");
  render();
};
$("pair").onclick = async () => {
  try {
    const r = await api("/pair", { code: $("code").value.trim() });
    if (r.status === 200 && r.body.token) { await chrome.storage.local.set({ token: r.body.token }); say("Paired."); } else say("Pairing failed: " + (r.body.error || r.status));
  } catch (e) { say("Bot not reachable on 127.0.0.1:8765"); }
  render();
};
$("unpair").onclick = async (e) => { e.preventDefault(); await chrome.storage.local.remove("token"); say("Forgotten on this browser. Send /unpair to the bot to revoke everywhere."); render(); };
render();
