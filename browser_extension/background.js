// Detects media requests, keeps them in session memory only (cleared when the browser closes),
// and sends a URL to the local bot ONLY when you press a button in the popup.
// No cookies, no request headers and no page content are read or sent.
const MEDIA_RE = /\.(m3u8|mpd|mp4|webm|m4v)(\?|#|$)/i;
const MEDIA_CT = /(mpegurl|dash\+xml|^video\/)/i;
const MAX_PER_TAB = 15;

async function getMap() { return (await chrome.storage.session.get("det")).det || {}; }
async function setMap(m) { await chrome.storage.session.set({ det: m }); }

let queue = Promise.resolve();           // serialize storage read-modify-write (several requests can land at once)
function remember(details, contentType) {
  queue = queue.then(() => rememberNow(details, contentType)).catch(() => {});
}
async function rememberNow(details, contentType) {
  if (details.tabId < 0) return;
  const m = await getMap();
  const list = m[details.tabId] || [];
  if (list.some(x => x.url === details.url)) return;
  let pageUrl = "";
  try { pageUrl = (await chrome.tabs.get(details.tabId)).url || ""; } catch (e) {}
  list.unshift({ url: details.url, page: pageUrl, ct: contentType || "", t: Date.now() });
  m[details.tabId] = list.slice(0, MAX_PER_TAB);
  await setMap(m);
  chrome.action.setBadgeText({ tabId: details.tabId, text: String(m[details.tabId].length) });
}

function onHeaders(details) {
  const h = (details.responseHeaders || []).find(x => x.name.toLowerCase() === "content-type");
  const ct = h ? h.value : "";
  if (MEDIA_RE.test(details.url) || MEDIA_CT.test(ct)) remember(details, ct);
}

// Registered synchronously at top level so Chrome can wake the service worker for matching requests.
// Without the optional host permission Chrome simply delivers no events, so this stays inert until you enable detection.
chrome.webRequest.onHeadersReceived.addListener(onHeaders, { urls: ["http://*/*", "https://*/*"] }, ["responseHeaders"]);

chrome.tabs.onRemoved.addListener(async (tabId) => { const m = await getMap(); delete m[tabId]; await setMap(m); });
chrome.tabs.onUpdated.addListener(async (tabId, info) => {
  if (info.status === "loading" && info.url) { const m = await getMap(); delete m[tabId]; await setMap(m); chrome.action.setBadgeText({ tabId, text: "" }); }
});
