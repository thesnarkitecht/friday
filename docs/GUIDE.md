# FRIDAY guide: setup, earbuds, plugins, and how it decides to speak

## What you need

| Item | Why | Cost |
|---|---|---|
| A Linux box that's always on: a Raspberry Pi 5 (8 GB), a mini PC, or your home server | Runs the container. Whisper `base.en` transcribes comfortably on a Pi 5; use `tiny.en` if it lags | about $80+ |
| Bluetooth earbuds with a mic | Ears and voice. Any pair that works as a headset | — |
| A Claude sign-in or API key | The brain | API usage |
| *Optional:* room radars (below) | Lets FRIDAY know when you have company at home | about $20/room |
| *Optional:* Hermes Agent running somewhere | Heavy research and coding | free (MIT) |

Bluetooth reaches about 10 m, so FRIDAY hears you around the house, not across town. To take it with you,
run the same container on a Pi 5 in your pocket (official Active Cooler plus a 20,000 mAh 5 V/3 A power bank).

## Setup

### 1. Pair the earbuds with the host

```bash
bluetoothctl
```

Then, inside `bluetoothctl`: `power on`, `scan on`, `pair <MAC>`, `trust <MAC>`, `connect <MAC>`.

Switch them to the headset profile, which turns the mic on. A2DP is music-only and has no mic:

```bash
pactl list cards short
```

```bash
pactl set-card-profile bluez_card.<MAC_with_underscores> headset-head-unit
```

Make them the default input and output (`pactl set-default-source …` / `pactl set-default-sink …`), or use
your desktop's sound settings. FRIDAY uses whatever the defaults are.

Headset mode is lower-fidelity audio (mSBC, 16 kHz), which is fine for speech and exactly the rate Whisper wants.

### 2. Credentials

```bash
cp .env.example .env
```

Set `FRIDAY_TOKEN` (long and random), `FRIDAY_OWNER_NAME`, and any plugin settings. For Claude, either:
- **Sign in:** install the `ant` CLI on the host and run `ant auth login`. Compose mounts that profile into
  the container read-only, and the Anthropic SDK picks it up.
- **API key:** set `ANTHROPIC_API_KEY` in `.env`.

A Claude Pro/Max chat subscription can't power a third-party server like this; it bills as API usage.

### 3. Start

```bash
docker compose up -d --build
```

```bash
docker compose logs -f
```

The compose file mounts `/run/user/1000/pulse` for audio. If your user ID isn't 1000 (`id -u`), change
both paths in `docker-compose.yml`.

### 4. Check it

```bash
curl -H "Authorization: Bearer $FRIDAY_TOKEN" -H "Content-Type: application/json" -d '{"text":"What do you know about me?"}' localhost:8080/api/ask
```

`GET /api/hud` shows loaded plugins (and any that failed), the situation FRIDAY sees, learned
interruption scores, and recent speech.

## Driving it

### From a terminal

Inside the container:

```bash
docker compose exec friday friday chat
```

From another machine (needs Python 3.10+):

```bash
pip install ./server
```

```bash
export FRIDAY_URL=http://<box>:8080 FRIDAY_TOKEN=...
```

| Command | What it does |
|---|---|
| `friday ask "…"` | One question, one answer (plus a catch-up line if things were held for you) |
| `friday chat` | Back-and-forth until Ctrl-D |
| `friday heard "…"` | As if overheard; FRIDAY decides whether it was meant for it |
| `friday watch` | Live: what FRIDAY hears, says, and alerts |
| `friday status` | Plugins, situation, what it learned about interrupting you, skills, routines, recent alerts |
| `friday memory [query]` | What it knows about you, or a search |
| `friday forget "…"` | Delete facts containing that text |

Typed and spoken conversations are the same conversation: what you type counts as you talking to FRIDAY.

### From OpenAI-compatible clients

Base URL `http://<box>:8080/v1`, API key = `FRIDAY_TOKEN`, model `friday`. `/v1/models` and
`/v1/chat/completions` (with or without streaming) are supported. FRIDAY keeps its own memory and
history, so it only reads the latest user message; the client's system prompt and earlier turns are ignored.

## How FRIDAY decides to speak

- **Always, right away:** reminders you set, routines you scheduled, Hermes reporting back.
- **Everything else** (new models, Hacker News, any plugin feed) goes through two checks:
  1. **Company and quiet hours.** If room radars count more than one person, another voice was heard in
     the last 2 minutes, or it's quiet hours (`FRIDAY_QUIET_HOURS`, 22–8 by default), it waits. When
     company leaves or you next talk to FRIDAY, you get one line: "Catching you up: …".
  2. **What it has learned.** Push back within 90 seconds ("not now", "I know", "stop") and that kind
     of alert scores lower; engage and it scores higher. Below 0.4 it stops interrupting and waits for
     the next good moment: the next time you talk to FRIDAY, or company leaving.
- **Feeds start quietly.** The first time a feed runs it only learns what already exists. After that,
  each new item is announced once, at most 3 per check.
- **Questions about you:** at most one every 20 hours, between 10:00 and 21:00, only when you've talked to
  FRIDAY in the last half hour but not in the last 5 minutes. Questions are about preferences only. Your
  answer is kept as a preference, and topics you name become interests for Hacker News. Push back on
  questions and it asks less.

## What it keeps

- **Raw speech:** deleted after the nightly pass (3:00) unless it scored as worth keeping (commitments,
  dates, preferences). Other people's words are always deleted then. Audio is never stored.
- **Facts about you:** kept, shown in `/api/memory`. Say "Friday, forget that I …" to delete one.
- **Skills:** `data/skills/`, plain Markdown you can read and edit.

## Plugins

Built in:

| Plugin | What it does | Setting |
|---|---|---|
| `models` | Every 10 min: new model ids on OpenRouter's public list from the labs you choose | `FRIDAY_MODEL_LABS` |
| `hackernews` | Every 10 min: front-page stories matching your interests. Also a search tool | — |
| `hermes` | `ask_hermes` hands research or coding to Hermes Agent and reports back | `HERMES_URL`, `HERMES_KEY` |

To connect Hermes, turn on its API server (`API_SERVER_ENABLED=true`, `API_SERVER_KEY=…` in Hermes'
`.env`, then `hermes gateway`). Then set `HERMES_URL=http://<host>:8642/v1` and `HERMES_KEY` to the same key.

Writing one: make `server/plugins/<name>/plugin.py` with `def setup(p):`. Use `@p.tool(...)` for things
Claude can call, `@p.feed(every_s=...)` for zero-token checks returning `{"key", "kind", "text"}` items,
`p.env("NAME")` for credentials, and an optional `skills/` folder. The plugins folder is mounted, so a
restart picks up changes without rebuilding.

## Room radars (optional)

| Item | Why | Cost |
|---|---|---|
| HLK-LD2450 24 GHz mmWave radar | Counts up to 3 people in a room. No camera | about $15 |
| Seeed XIAO ESP32-C3 | Runs ESPHome and reports the count | about $5 |
| *Optional:* HLK-LD2410C | Catches someone sitting perfectly still, which the LD2450 can miss | about $5 |

ESPHome config (check field names against the ESPHome `ld2450` and `http_request` docs for your version):

```yaml
uart:
  rx_pin: GPIO20
  tx_pin: GPIO21
  baud_rate: 256000
ld2450:
http_request:
sensor:
  - platform: ld2450
    target_count:
      name: People
      on_value:
        - http_request.post:
            url: http://friday:8080/api/presence
            request_headers:
              Authorization: !secret friday_bearer   # "Bearer <FRIDAY_TOKEN>"
              Content-Type: application/json
            json:
              room: office
              people: !lambda 'return to_string((int) x);'
```

## API

| Endpoint | Use |
|---|---|
| `WS /ws?token=…` | Send `{"type":"utterance","text":…}` or `{"type":"presence","room":…,"people":…}`. Receive `heard`, `say`, `notify`, `memory` |
| `POST /api/utterance` | Everything heard; FRIDAY decides whether it was addressed |
| `POST /api/ask` | CLI / push-to-talk: always addressed. Returns `reply` and `catch_up` |
| `POST /api/forget` | Delete facts containing `text` |
| `GET /v1/models`, `POST /v1/chat/completions` | OpenAI-compatible |
| `POST /api/presence` | Room radars |
| `GET /api/hud` | Plugins, situation, learned scores, skills, routines, recent speech |
| `GET /api/memory?q=` | What FRIDAY knows about you |

Keep port 8080 on your LAN or Tailscale. It holds your life.
