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
