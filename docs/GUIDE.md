# FRIDAY guide: shopping list, features, testing on iPhone

## Shopping list

Prices are approximate. Check before buying.

| Item | Needed for | Cost |
|---|---|---|
| Home box with the Tenstorrent Blackhole cards (you have this) | Models, memory, the server | — |
| iPhone on iOS 17.2+ | The app | — |
| Mac with Xcode 26.4+ | Building the app (the glasses SDK requires this Xcode) | — |
| Apple Developer Program | Installing on your phone for more than 7 days, Siri intents | $99/yr (a free Apple ID works, but apps expire after 7 days) |
| Meta Ray-Ban Display + Neural Band | Replies on the lens, glasses camera, mic and speakers | about $799 |
| *or* Ray-Ban Meta (Gen 2) | Glasses camera, mic and speakers; no display | about $379 |
| Tailscale | Phone reaches the home box from anywhere | free personal plan |
| Twilio number + usage | Calls FRIDAY makes on your behalf | about $1–2/month for the number, about 1–2¢/min |
| AirPods (optional) | Hands-free listening when you're not wearing glasses | — |

You don't need glasses to start: the app has a built-in glasses simulator (below).

## Features

**Voice and memory**
- Listens all day. Speech is transcribed on the iPhone; only text goes to your home box.
- Knows when you're talking to it: wake word ("Friday…", not "see you Friday"), follow-ups for 10 s
  after it speaks, and the model breaking ties when others are around.
- Remembers: scores every sentence for importance, and each night turns the day into a summary, durable
  facts about you, and reminders. Other people's words are deleted after that pass by default.
- Ask it anything about your past: "What did I promise Mike last week?"

**Proactive**
- Speaks due reminders, severe weather alerts, nearby M4+ earthquakes, and GitHub review requests and
  mentions, once each.

**Acting for you**
- Runs your Apple Shortcuts by name (lights, music, Focus, alarms, anything you build).
- Starts calls and pre-fills texts on your iPhone; iOS asks you to confirm each one.
- Phones businesses for you through Twilio: says it's an AI assistant, reaches the goal, reports back.

**Seeing**
- Public traffic cameras (NYC, London, California), described by a vision model.
- Meta glasses camera: "Friday, what does that sign say?" grabs one frame and answers. It won't identify
  people by their face.

**Meta glasses**
- Audio: the glasses act as a Bluetooth headset, so you talk to and hear FRIDAY through them.
- Ray-Ban Display: every reply and alert also shows as a card on the lens.

## Test it on your iPhone

### 1. Start the server
```bash
cd server
pip install -r requirements.txt
cp .env.example .env
set -a; . ./.env; set +a
uvicorn friday.app:app --host 0.0.0.0 --port 8080
```
Set `FRIDAY_TOKEN` (long and random), `FRIDAY_OWNER_NAME`, home coordinates, and the model server URL.
Check it from the box:
```bash
curl -H "Authorization: Bearer $FRIDAY_TOKEN" localhost:8080/api/hud
```
Ask it something through the API before involving the phone:
```bash
curl -H "Authorization: Bearer $FRIDAY_TOKEN" -H "Content-Type: application/json" -d '{"text":"What do you know about me?"}' localhost:8080/api/ask
```

### 2. Connect the phone
Install Tailscale on the box and the iPhone and sign in to both with the same account. Your server URL
is `http://<box-name>:8080` (the name Tailscale shows for the box).

### 3. Build and install the app
```bash
cd ios
brew install xcodegen
xcodegen
open Friday.xcodeproj
```
In Xcode: select the Friday target, then Signing & Capabilities, and pick your team. Plug in the iPhone,
choose it as the run destination, and press Run. On the phone, enable Developer Mode if iOS asks
(Settings > Privacy & Security > Developer Mode).

### 4. First run
Allow microphone, speech recognition, location and notifications. Tap the gear, then enter the server URL
and token. The dot next to FRIDAY turns green when it's connected.

### 5. Things to try
| Say | Expect |
|---|---|
| "Friday, remember I'm allergic to shellfish." | It confirms. Later: "Friday, what am I allergic to?" |
| "Friday, remind me to stretch in two minutes." | It speaks the reminder two minutes later |
| "Friday, any alerts near me?" | Weather and quake check for your location |
| "Friday, run Lights Off." (make a Shortcut with that name first) | The Shortcuts app runs it |
| "Friday, text Mom I'm running late." | It reads it back; say yes and Messages opens pre-filled |
| "Friday, what's traffic like near Times Square?" | Nearby NYC camera, described |
| A conversation without saying "Friday" | Nothing spoken back, but `/api/hud` shows it was heard |

Siri: "Hey Siri, ask Friday what's on my mind." To use the Action button, go to Settings > Action Button >
Shortcut > Ask FRIDAY.

### 6. Glasses without glasses (simulator)
Tap the gear, then **Simulate Ray-Ban Display**. The app pairs simulated glasses using Meta's
MockDeviceKit: the iPhone's back camera becomes the glasses camera, and a lens preview appears on
the main screen. Point the phone at a sign and say "Friday, what does that sign say?". FRIDAY's
answer should appear in the lens preview.

### 7. Real glasses
1. Pair the glasses in the Meta AI app.
2. In Meta AI, go to Settings > your glasses and turn on Developer Mode.
3. In FRIDAY, tap the gear, then **Connect glasses**. Meta AI opens; approve FRIDAY there.
4. Choose the glasses as the iPhone's audio device. Ask about something you're looking at. The first
   time, Meta AI asks for camera permission.

Developer Mode works for you and up to 100 testers through a release channel. Publishing to the public
needs Meta's approval.

## FRIDAY on the glasses (web app)

`glasses/` is a web app that runs directly on Ray-Ban Display, built with Meta's own UI Toolkit.
The phone stays the ears, voice and camera; the glasses show the conversation, memory, alerts and
your phone's screen. See [glasses/README.md](../glasses/README.md) to build and pair it.

No glasses? Open the [FRIDAY Lens preview](https://claude.ai/artifact/Lsa8d5RBMuLDfZxiGtzPGR):
it runs the same build in a first-person view, paired with a simulated iPhone.

## Not included
Body-worn ESP32 sensing of the people around you is not part of this build.
Full-screen sharing from a real iPhone needs a ReplayKit broadcast extension (not built yet); the
server and glasses already relay and show the frames.
