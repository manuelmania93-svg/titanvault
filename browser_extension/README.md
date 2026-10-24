# TitanVault Handoff (browser extension, Chrome / Edge / Brave, Manifest V3)

Sends a video stream URL you pick from the current tab to your own TitanVault bot.

1. Run TitanVault (it listens on 127.0.0.1:8765 only; set `BRIDGE_ENABLE=0` to turn it off).
2. Chrome: `chrome://extensions` > Developer mode > Load unpacked > this folder.
3. In Telegram, as the owner, send `/pair`. Enter the code in the extension popup. The code is single use and expires in 5 minutes.
4. Click the extension > "Enable detection" (Chrome asks for permission to see requests on websites).
5. Play the video, open the popup, press "Send to TitanVault".

What it does and does not do
- Detection is off until you grant the permission. It only keeps stream URLs in session memory, per tab, and clears them on navigation and when the browser closes.
- Nothing is sent anywhere except when you press Send, and only to 127.0.0.1:8765. Only the stream URL, the page URL (used as Referer) and the page title are sent.
- No cookies, no request headers, no page content are read or sent. Links that need your login/cookies to play will fail to download. That is a known limit.
- `/unpair` in Telegram revokes every paired browser. "Forget pairing" in the popup only forgets it in this browser.
- Optional hardening: set `BRIDGE_EXT_ID` to your extension's ID so only that extension is accepted.

## Using cat-catch instead (or as well)
[cat-catch](https://github.com/xifangczy/cat-catch) is a more mature media detector (network sniffing, page search, MediaSource capture). The bot's receiver speaks the aria2 JSON-RPC subset, so cat-catch can hand a detected stream straight to TitanVault:

1. In Telegram, as the owner, send `/catcatch`. The bot replies with a one-time token (also revoked by `/unpair`).
2. cat-catch Settings → Aria2 RPC → enable. RPC address: `http://127.0.0.1:8765/jsonrpc`, token: the one from `/catcatch`.
3. Play the video, click the cat-catch aria2 icon on the detected stream. The bot picks it up like a browser handoff.

Limits, same as the built-in extension:
- It is manual: you press the button in your browser. The bot cannot trigger it.
- Only the URL and its Referer are accepted. `dir` is ignored, `out` is used only as a display title, and Cookie/Authorization headers are always dropped, so links that need your login still fail.
- cat-catch's MediaSource capture can grab streams that have no reusable URL at all; those cannot be handed to any downloader.
- The `BRIDGE_EXT_ID` pin only applies to the TitanVault extension endpoints; `/jsonrpc` accepts any browser-extension origin because the token is the secret.

## Permissions and limits (read before enabling)
- Shipped permissions: `webRequest`, `storage`, `tabs`, and the bot receiver at `http://127.0.0.1:8765/*`.
- Seeing video requests on other sites needs access to all http/https sites. It is optional: the popup button "Enable detection" asks Chrome for it, only when you click. Stream URLs are kept in memory only and sent to your bot only when you press Send. This prompt was NOT tested (a test build pre-granted it).
- No cookies are read or sent. Links that need login or cookies are not supported.
- Firefox and Edge, and the extension after the browser sleeps its service worker, are untested.
- Pair: send `/pair` to the bot (owner only), enter the 5-minute code in the popup. `/unpair` revokes all tokens.

## Bot settings that are still your decision
- `KEEP_CAP_GB` (default 20) was chosen by the assistant. At the cap new downloads are refused; nothing is deleted automatically.
- `JD_MODE` is off by default. `local` uses JDownloader's unofficial local API (tested); `myjd` uses a MyJDownloader account (untested).
- `STREAMLINK_ENABLE` (default on) adds Streamlink's plugin set as an automatic resolver between the custom scrapers and the browser snatch. Needs `pip install streamlink`; without the package the step is skipped. Set `STREAMLINK_ENABLE=0` to turn it off. Its plugins focus on broadcasters/live services; it is not a resolver for hoster sites like VOE/Filemoon/Doodstream.
- `BRIDGE_ENABLE=0` disables the receiver.
- Not tested: real Telegram upload and retry, MyJDownloader, the aria2c path of `/stream`.
