# FRIDAY guide: setup, headphones, plugins, and how it decides to speak

## Setup (one device)

You need headphones or earbuds with a mic, paired with the device, and a brain: a Claude API key, a
Claude sign-in (`ant auth login`), or any OpenAI-compatible API.

```bash
curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/friday/main/install.sh | sh
```

This installs uv (which brings its own Python) and FRIDAY into `~/.local`; nothing needs sudo. Then
`friday setup` asks your name and your brain, downloads FRIDAY's ears and voice (about 300 MB, once), and
runs `friday doctor`. From a clone instead: `FRIDAY_SRC=./server sh install.sh`, or `pip install ./server`.

```bash
friday
```

You see a live transcript, FRIDAY's voice comes through your headphones, and you can type at the same time.

It uses the device's default mic and speaker; with Bluetooth headphones connected, those are usually the
headphones. `friday devices` lists everything, and `friday --mic 3 --speaker 5` picks specific ones. On
Linux without PortAudio, FRIDAY uses the system's own recorder and player (`parec`/`paplay` or
`arecord`/`aplay`) and the devices chosen in your sound settings. Bluetooth headphones only have a mic in
"headset" mode there. Pick it in your sound settings, or:

```bash
pactl set-card-profile bluez_card.<MAC_with_underscores> headset-head-unit
```

Everything FRIDAY keeps is in `~/.friday`: memory (`friday.db`), its skills, the speech models, settings
(`.env`), and this device's login.

### Choosing a brain

`friday setup` asks, or set it any time:

| Brain | Settings |
|---|---|
| Claude (default) | `friday config set ANTHROPIC_API_KEY sk-ant-…`, or `ant auth login` |
| Ollama on this machine | `friday config set FRIDAY_LLM_URL http://localhost:11434/v1`, `friday config set FRIDAY_MODEL llama3.3` |
| OpenRouter | `FRIDAY_LLM_URL https://openrouter.ai/api/v1`, `FRIDAY_LLM_KEY sk-or-…`, `FRIDAY_MODEL anthropic/claude-opus-5.5` |
| LM Studio, llama.cpp, vLLM, OpenAI, Hermes… | their `/v1` URL, key if any, and model name |

The model needs tool calling. Where the server doesn't support structured output, FRIDAY asks for plain
JSON and checks it. `friday config unset FRIDAY_LLM_URL` goes back to Claude. `friday doctor` checks the brain.

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
| `friday graph [thing] [--format text\|mermaid\|dot\|json]` | The map of your world, or what's around one thing |
| `friday export [file]` / `friday import <file>` | Save everything to JSON / merge an export, or learn from any notes file |
| `friday setup` / `friday config [set\|unset NAME VALUE]` / `friday doctor` | First run / settings / health check |
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

## The graph

FRIDAY keeps a map of how the things in your life connect: you work on a project, the project uses a tool,
a topic matters for it, a place you go, an org you work with. It adds links when you mention them, and
from the day's conversation every night. People appear only in how they relate to your work and interests;
it never maps health, money, family or relationships. Ask "what's connected to FRIDAY?" or:

```bash
friday graph FRIDAY --depth 2
```

```bash
friday graph --format mermaid > world.mmd
```

## Your data

```bash
friday export
```

This writes everything FRIDAY knows (facts, day summaries, reminders, routines, the graph, its skills, what
it learned about interrupting you) to `friday-export-DATE.json`, readable only by you. Add `--transcripts`
to include kept raw speech.

```bash
friday import friday-export-20261103.json
```

This merges an export into this FRIDAY (another machine, a new install). Nothing already here is
overwritten, except skills with the same name. Give `import` any other text file (notes, a bio, an
exported chat history) and FRIDAY reads it and keeps the facts, reminders and links that matter.

## What it keeps

- **Raw speech:** deleted after the nightly pass (3:00) unless it scored as worth keeping (commitments,
  dates, preferences). Other people's words are always deleted then. Audio is never stored.
- **Facts about you:** kept, shown in `/api/memory`. Say "Friday, forget that I …" to delete one.
- **Skills:** `~/.friday/skills/`, plain Markdown you can read and edit.
- **How you sounded:** when SenseVoice hears a tone (frustrated, happy…) or a laugh, it's noted with that
  line, so the nightly pass can notice patterns. Audio itself is never kept.

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
| `GET /api/graph?entity=&depth=` | The graph |
| `GET /api/export`, `POST /api/import`, `POST /api/learn` | Export, merge an export, learn from text |
| `GET /api/doctor` | Brain, speech and plugin health |

Keep port 8080 on your LAN or Tailscale. It holds your life.
