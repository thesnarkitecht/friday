# FRIDAY

A voice-first personal assistant that runs on your own hardware. It listens, remembers what matters,
and speaks up on its own when something needs your attention.

```
iPhone app ──(Tailscale, WebSocket)──► home server ──► model server (Tenstorrent Blackhole / any OpenAI-compatible)
  on-device speech-to-text               memory (SQLite + full-text search)
  speech, Siri / Action button           brain + tools
  Shortcuts, tel:, sms: hand-offs         proactive loop: reminders, alerts, quakes, GitHub
                                          Twilio calls on your behalf
                                          traffic cameras (from gods-eye-view)
```

## What it does

- **Listens all day** on the iPhone. Speech is transcribed on the phone; only text goes to your home server.
- **Knows when you're talking to it**: the wake word ("Friday…", but not "see you Friday"), a 10-second
  follow-up window after it speaks, and the model breaking ties when other people are around.
- **Remembers**: every utterance gets an importance score. At 3am a "sleep" pass turns the day into a
  summary, durable facts about you, and reminders. Low-value raw text expires after `FRIDAY_RETAIN_DAYS`.
  Words from anyone who isn't you are deleted after that pass unless `FRIDAY_RECORD_OTHERS=1`.
- **Speaks up**: due reminders, severe NWS weather alerts, magnitude 4+ quakes within 200 km, and GitHub
  review requests and mentions. Each one is said once.
- **Uses your iPhone the Apple-supported way**: runs your Shortcuts (music, lights, Focus, alarms…),
  starts calls (`tel:`) and pre-fills texts (`sms:`). iOS asks you to confirm calls and texts.
- **Calls people for you** through Twilio: it says it's an AI assistant, works toward the goal you gave it,
  and reports back.
- **Looks at public traffic cameras** (NYC, London, California) and describes them with a vision model.

## Run the server

```bash
cd server
pip install -r requirements.txt
cp .env.example .env   # fill it in
set -a; . ./.env; set +a
uvicorn friday.app:app --host 0.0.0.0 --port 8080
```

Tests: `cd server && python -m pytest -q`

### Model server on the Blackhole cards

Run Tenstorrent's [tt-inference-server](https://github.com/tenstorrent/tt-inference-server) and point
`FRIDAY_LLM_BASE_URL` at it. Check its model support table for what currently runs on Blackhole; the
defaults in `.env.example` are placeholders. The model needs tool calling. Camera descriptions need a
vision model. You can point either setting at another server, including a cloud one.

### Reaching it from anywhere

Put the home box and the iPhone on [Tailscale](https://tailscale.com). Twilio has to reach
`/twilio/turn` from the internet: expose only that path (e.g. Tailscale Funnel) and set `FRIDAY_PUBLIC_URL`.
Twilio requests are checked against their signature.

## Build the iPhone app

You need a Mac with Xcode 15+ and an Apple developer account.

```bash
cd ios
brew install xcodegen
xcodegen
open Friday.xcodeproj
```

Set your signing team, then run it on your phone. In the app's settings, enter the server URL and `FRIDAY_TOKEN`.
"Hey Siri, ask Friday…" and the Action button (set it to the **Ask FRIDAY** shortcut) work without opening the app.

Create Shortcuts with clear names ("Lights Off", "Focus Work", "Play Focus Music") and tell FRIDAY about them.
It runs them by name.

## Known limits

- iOS limits background listening. It works best with the app open, or in the background with an
  active audio session (it plays and records, so iOS keeps it alive). Expect iOS to stop it sometimes.
- iOS only opens Shortcuts, `tel:` and `sms:` links while the app is in the foreground. In the background
  FRIDAY posts a notification instead.
- The phone can't tell voices apart: speech addressed to FRIDAY is treated as yours, everything else as "unknown".
- The iOS app hasn't been compiled yet: it was written without Xcode.

Traffic camera catalogs are ported from [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view) (MIT).
