# FRIDAY guide: setup, headphones, plugins, and how it decides to speak

## Setup (one device)

You need headphones or earbuds with a mic, paired with the device, and a Claude sign-in or API key.

```bash
pip install ./server
```

On Linux, also install PortAudio for the mic: `sudo apt install libportaudio2`. macOS and Windows need nothing extra.

```bash
ant auth login
```

Or put `ANTHROPIC_API_KEY=…` in `~/.friday/.env`. A Claude Pro/Max chat subscription can't power an app
like this; it bills as API usage.

```bash
friday
```

The first run downloads FRIDAY's voice (about 60 MB) and speech recognition model (about 150 MB), once.
After that you see a live transcript, FRIDAY's voice comes through your headphones, and you can type at
the same time.

It uses the device's default mic and speaker; with Bluetooth headphones connected, those are usually the
headphones. `friday devices` lists everything, and `friday --mic 3 --speaker 5` picks specific ones. On
Linux, Bluetooth headphones only have a mic in "headset" mode. Pick it in your sound settings, or:

```bash
pactl set-card-profile bluez_card.<MAC_with_underscores> headset-head-unit
```

Settings and plugin credentials go in `~/.friday/.env` (copy `.env.example`). Everything FRIDAY keeps is
in `~/.friday`: memory (`friday.db`), its skills, the voice, and this device's login.

**Laptop speakers instead of headphones?** Run `friday --no-barge-in`, or FRIDAY may hear itself and stop.

## Sharing one FRIDAY across devices (optional)

On an always-on machine (mini PC, home server, Pi 5), either:

```bash
FRIDAY_TOKEN=<long random string> friday serve
```

or, with Docker, put the same settings in `.env` next to `docker-compose.yml` and run:

```bash
docker compose up -d --build
```

The hub preloads the speech models, so the first answer is as quick as the rest. On every other device:

```bash
friday login http://<hub>:8080 <FRIDAY_TOKEN>
```

Now they share one memory, one set of skills, and one personality, and every connected device sees the
conversation. `friday logout` goes back to a FRIDAY of the device's own. Use Tailscale to reach the hub
from outside your home network; never expose port 8080 to the internet.

| Hardware for a hub | Notes |
|---|---|
| Raspberry Pi 5 (8 GB), mini PC, or home server | SenseVoice hears a sentence in well under a second even on a Pi 5 |
| *Optional:* room radars (below) | Tells FRIDAY when you have company at home |
| *Optional:* Hermes Agent | Heavy research and coding |

## Talking to it

- **"Friday, …"** always works.
- **Follow-ups** for 10 seconds after FRIDAY speaks need no name.
- **Alone, you can just ask.** Lines that sound like a request ("remind me to…", "what's…", "look that
  up", "what do you think") go to a quick check by Claude of whether you meant FRIDAY or were thinking out
  loud. With company, you need its name.
- **It knows what you were just talking about.** When you ask, FRIDAY also sees the last 5 minutes of
  what was said around you, so "Friday, what do you think?" or "look that up" works without repeating yourself.
- **Talk over it** to cut it off: about half a second of your voice stops FRIDAY mid-sentence, and what
  you said is heard as your next request.
- **It starts things too:** a greeting when you put your headphones on after 4+ hours away
  (`FRIDAY_GREET_AFTER_HOURS`), check-ins on plans it heard ("How did the interview go?"), one pattern it
  noticed overnight, and now and then a question about your tastes.

| Command | What it does |
|---|---|
| `friday` | Talk (voice + live transcript + typing) |
| `friday ask "…"` | One question, one answer (plus a catch-up line if things were held for you) |
| `friday chat` | Typed back-and-forth until Ctrl-D |
| `friday watch` | Live transcript only, no mic |
| `friday heard "…"` | As if overheard; FRIDAY decides whether it was meant for it |
| `friday status` | Plugins, situation, what it learned about interrupting you, skills, routines, recent alerts |
| `friday memory [query]` | What it knows about you, or a search |
| `friday forget "…"` | Delete facts containing that text |
| `friday devices` | List mics and speakers |
| `friday login <url> <token>` / `friday logout` | Use a shared hub / go back to this device's own FRIDAY |
| `friday serve` | Be the hub other devices log in to |

Inside the hub container the same commands work: `docker compose exec friday friday ask "…"`.

## OpenAI-compatible clients

Base URL `http://<hub>:8080/v1`, API key = `FRIDAY_TOKEN`, model `friday`. `/v1/models` and
`/v1/chat/completions` (with or without streaming) are supported. FRIDAY keeps its own memory and
history, so it only reads the latest user message; the client's system prompt and earlier turns are ignored.

## How FRIDAY decides to speak

- **Always, right away:** reminders you set, routines you scheduled, Hermes reporting back.
- **Everything else** (new models, Hacker News, check-ins, patterns, any plugin feed) goes through two checks:
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
- **Skills:** `~/.friday/skills/`, plain Markdown you can read and edit.

## Plugins

Built in:

| Plugin | What it does | Setting |
|---|---|---|
| `models` | Every 10 min: new model ids on OpenRouter's public list from the labs you choose | `FRIDAY_MODEL_LABS` |
| `hackernews` | Every 10 min: front-page stories matching your interests. Also a search tool | — |
| `hermes` | `ask_hermes` hands research or coding to Hermes Agent and reports back | `HERMES_URL`, `HERMES_KEY` |

To connect Hermes, turn on its API server (`API_SERVER_ENABLED=true`, `API_SERVER_KEY=…` in Hermes'
`.env`, then `hermes gateway`). Then set `HERMES_URL=http://<host>:8642/v1` and `HERMES_KEY` to the same key.

Writing one: make `~/.friday/plugins/<name>/plugin.py` (on the hub with Docker: `plugins/<name>/` next to
`docker-compose.yml`) with `def setup(p):`. Use `@p.tool(...)` for things Claude can call,
`@p.feed(every_s=...)` for zero-token checks returning `{"key", "kind", "text"}` items, `p.env("NAME")`
for credentials from `.env`, and an optional `skills/` folder. Restart FRIDAY to load it; `friday status`
lists plugins that failed to load and why.

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
| `WS /ws?token=…` | Send `utterance` (text heard), `ask` (typed: always for FRIDAY), `presence`, or `hello` `{"voice": true}` then `audio` (`pcm`: base64 16 kHz mono 16-bit). Receive `heard`, `say`, `notify`, `memory`; voice clients also get `audio` (base64 WAV) on `say`/`notify` |
| `POST /api/utterance` | Everything heard; FRIDAY decides whether it was addressed |
| `POST /api/ask` | CLI / push-to-talk: always addressed. Returns `reply` and `catch_up` |
| `POST /api/forget` | Delete facts containing `text` |
| `GET /v1/models`, `POST /v1/chat/completions` | OpenAI-compatible |
| `POST /api/presence` | Room radars |
| `GET /api/hud` | Plugins, situation, learned scores, skills, routines, recent speech |
| `GET /api/memory?q=` | What FRIDAY knows about you |

Keep port 8080 on your LAN or Tailscale. It holds your life.
