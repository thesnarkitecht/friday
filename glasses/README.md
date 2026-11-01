# FRIDAY for Meta Ray-Ban Display

A web app that runs on the glasses, built with Meta's
[UI Toolkit for Meta Ray-Ban Display](https://github.com/meta/meta-ray-ban-display-ui-toolkit-web)
(`@wearables-ui-toolkit/mrbd`).

It is the glasses half of FRIDAY. The iPhone app is the ears, voice and camera; the glasses
are its screen. Both connect to your FRIDAY home server and share one live conversation.

**On the glasses:** Talk (conversation, Ask, Look, suggested questions), Memory, Alerts
(reminders, weather, quakes and GitHub also pop up as toasts), and iPhone screen sharing.

## Build
```bash
npm install --legacy-peer-deps
npm run build
```
The FRIDAY server serves `dist/` at `/glasses/` automatically.

## Pair
1. On the iPhone: FRIDAY › Settings › Pair glasses. A QR code appears.
2. Scan it with the glasses to add the web app. It opens `/glasses/?pair=<code>`, trades the
   one-time code for access, and joins the conversation.

The code works once and expires after 10 minutes. The glasses must be able to reach your server
(the same address the phone uses).

## Preview without glasses
- **Meta's simulator:** install the [Meta Ray-Ban Display Simulator](https://chromewebstore.google.com/detail/meta-ray-ban-display-simu/jpjlmmodokemlepklkdbimceggpbjcll)
  Chrome extension and open `npm run dev` in it.
- **FRIDAY preview:** `preview/friday-glasses.html` runs this exact build in a first-person view,
  paired with a simulated iPhone.
