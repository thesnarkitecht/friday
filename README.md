# FRIDAY

A voice-first personal assistant that runs on your own hardware. It listens, remembers what matters,
and speaks up on its own when something needs your attention.

```
clients (pocket Pi, room sensors, anything) ──(Tailscale, WebSocket/HTTP)──► engine ──► model server
  speech as text, presence counts                memory (SQLite + full-text search)     llama.cpp on the Pi, or
                                                 agent loop + tools                     Tenstorrent Blackhole at home
                                                 proactive loop + learning loop
                                                 Twilio calls on your behalf
```

## What it does

- **Hears everything you send it**, and knows when you're talking to it: the wake word ("Friday…", but not
  "see you Friday"), a 10-second follow-up window after it speaks, and the model breaking ties when
  presence sensors say other people are around.
- **Remembers**: every utterance gets an importance score. At 3am a "sleep" pass turns the day into a
  summary, durable facts about you, and reminders. Low-value raw text expires after `FRIDAY_RETAIN_DAYS`.
  Words from anyone who isn't you are deleted after that pass unless `FRIDAY_RECORD_OTHERS=1`.
- **Speaks up**: due reminders, severe NWS weather alerts, magnitude 4+ quakes within 200 km, and GitHub
  review requests and mentions. Each one is said once.
- **Reads the room**: mmWave sensors report how many people are in each room. Non-urgent things wait
  while you have company and arrive as one catch-up line when they leave.
- **Learns what's worth interrupting you for**: pushing back ("not now", "I know") or engaging tunes each
  kind of interruption. Kinds you don't want live wait for the next good moment instead.
- **Learns how to do things**: skills are `SKILL.md` how-tos (the Hermes Agent / agentskills.io format).
  Only a one-line index sits in the prompt; the steps load when needed. Each night FRIDAY reviews what
  it did with tools and writes or fixes skills.
- **Routines**: "every weekday at 7, brief me." FRIDAY does the task on schedule and tells you.
- **Small-model friendly**: the system prompt and tools never change between turns, so local servers
  reuse their prompt cache; only a short context block is new each time.
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

Put the pocket unit, the home box and the room sensors on [Tailscale](https://tailscale.com) (sensors can
stay on the home LAN). Twilio has to reach `/twilio/turn` from the internet: expose only that path (e.g.
Tailscale Funnel) and set `FRIDAY_PUBLIC_URL`. Twilio requests are checked against their signature.

## Known limits

- No voice loop on the Pi yet: speech must arrive as text (`/ws`, `/api/utterance`, `/api/ask`).
- Voices aren't told apart: speech addressed to FRIDAY is treated as yours, everything else as "unknown".

Skills and progressive disclosure are adapted from [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research (MIT).
Traffic camera catalogs are ported from [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view) (MIT).

**How it works:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (diagrams).

**Start here:** [docs/GUIDE.md](docs/GUIDE.md) has the shopping list (pocket Pi and presence sensors),
why prompts are shaped the way they are, and how the learning loop decides when to speak.
