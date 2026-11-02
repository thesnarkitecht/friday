# FRIDAY guide: hardware, presence, and how it learns

FRIDAY is now just the engine: memory, the agent loop, the proactive loop, presence and the learning loop.
Anything that can send text and play speech is a client (see "Talking to it" below).

## Shopping list

Prices are approximate. Check before buying.

### The pocket unit

| Item | Why | Cost |
|---|---|---|
| Raspberry Pi 5, 8 GB (16 GB if you want a 4B model plus headroom) | Runs the engine and a small model | about $80 / $120 |
| Official Active Cooler | A Pi 5 doing inference in a pocket throttles without it | about $5 |
| Slim case that fits the cooler | Pocket | about $10 |
| 64 GB+ A2 microSD, or an NVMe HAT + small SSD | Model files load much faster from NVMe | $12–40 |
| USB-C power bank, 20,000 mAh, 5 V / 3 A output | 5–8 h of mixed use; the Pi 5 draws 8–10 W while generating | about $40 |
| USB-C lavalier mic, or a small USB speakerphone | Ears | $15–60 |
| Bluetooth earbuds | Voice | — |
| *Optional:* Raspberry Pi AI HAT+ 2 (Hailo-10H, 8 GB on board) | Moves a 1–1.5B model off the CPU. Check its current model list first | about $130 |

What runs on a Pi 5: a 1.5B–4B instruct model with tool calling (Qwen3-4B or Llama 3.2 3B, Q4) under
`llama-server`. Expect a few words per second out and tens of tokens per second in. That's why prompt
size matters so much (next section). Or keep the Blackhole box at home as the brain: point
`FRIDAY_LLM_BASE_URL` at it over Tailscale and the Pi only runs the engine.

### Presence sensing (one per room)

| Item | Why | Cost |
|---|---|---|
| HLK-LD2450 24 GHz mmWave radar | Counts up to 3 people in the room, through clothes and in the dark. No camera | about $15 |
| Seeed XIAO ESP32-C3 (or any ESP32 board) | Runs ESPHome and tells FRIDAY the count | about $5 |
| USB-C phone charger + cable | Power | — |
| *Optional:* HLK-LD2410C | The LD2450 can lose someone who sits perfectly still; the LD2410C catches still presence | about $5 |

Start with one in the room you spend the most time in. Four wires: 5V, GND, TX→RX, RX→TX.

ESPHome config, so the sensor reports changes to FRIDAY (check field names against the ESPHome
`ld2450` and `http_request` docs for your version):

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

### Who's around you (anywhere, not just at home)

The question FRIDAY needs answered is "did the owner say that, and was anyone else talking?" Voice
answers it better than any body-worn sensor. Radar on your body sees your own limbs move, not people.

| Item | Why | Cost |
|---|---|---|
| The lavalier mic from the pocket kit | Your voice is much louder on it than anyone else's | — |
| Voice ID on the Pi ([sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) speaker identification) | Enroll 30 s of your voice. Each utterance is tagged `owner` or `other` | free, runs offline |
| *Optional:* throat mic, plus a 2-input USB audio adapter | It hears only you, even in a loud bar. Speech on the room mic but not the throat mic is someone else | about $25 + $15 |

The client sends that tag as `speaker` with each utterance. The engine then:
- never takes commands from another voice, even if a guest says "Friday";
- counts you as "with company" for 2 minutes after another voice. Follow-ups without the wake word go
  to the model to decide, and non-urgent interruptions wait;
- forgets other people's words after the nightly pass, as before.

### Optional

| Item | Why | Cost |
|---|---|---|
| Tailscale | Pocket unit reaches the home box from anywhere | free personal plan |
| Twilio number + usage | Calls FRIDAY makes for you | about $1–2/month, 1–2¢/min |

## Why big prompts were killing local models, and what changed

Prompt processing is the slow part on small hardware. The old prompt put the clock and the recalled
memories inside the system prompt, so every turn started from token zero. With the tool list, that
was 2,000+ tokens re-read on every question.

Now:
- The system prompt and the tool list never change. `llama-server` and vLLM keep that prefix in the
  KV cache, so a new turn only processes the new message.
- The per-turn part (time, place, situation, 8 facts, 4 memories, each capped at 160 characters) goes
  in a small `[context]` block on the latest message only. Earlier turns stay as plain text, so they
  stay cached too.
- History is 12 messages, and tool results are capped at 3,000 characters.
- The nightly memory pass digests the day in ~6,000-character pieces, so it fits a 4k context window.

Run `llama-server` with `--jinja` (tool calling) and `-c 4096` or more. Prompt caching is on by default.

## How FRIDAY decides when to speak up

- **Always, right away:** reminders you set, severe weather, nearby quakes, results of calls it made.
- **Everything else** (GitHub today, any feed you add later) goes through two checks:
  1. **Company.** If the presence sensors count more than one person, it waits. When they leave, you
     get one line: "Catching you up: …".
  2. **What it has learned.** When you push back within 90 seconds of an interruption ("stop", "not now",
     "I know", "who cares"…), that kind of interruption scores lower. When you engage, it scores higher.
     Below 0.4, that kind stops interrupting and waits for the next good moment instead: the next time
     you talk to FRIDAY, or company leaving. Your reaction to a catch-up line tunes every kind in it, so
     a kind you start caring about again works its way back to interrupting.

`GET /api/hud` shows the presence count, the situation FRIDAY sees, and the learned scores.

## Run it

```bash
cd server
pip install -r requirements.txt
cp .env.example .env   # fill it in
set -a; . ./.env; set +a
uvicorn friday.app:app --host 0.0.0.0 --port 8080
```

Check it:
```bash
curl -H "Authorization: Bearer $FRIDAY_TOKEN" -H "Content-Type: application/json" -d '{"text":"What do you know about me?"}' localhost:8080/api/ask
```

Fake a sensor:
```bash
curl -H "Authorization: Bearer $FRIDAY_TOKEN" -H "Content-Type: application/json" -d '{"room":"office","people":2}' localhost:8080/api/presence
```

## Talking to it

| Endpoint | Use |
|---|---|
| `WS /ws?token=…` | Send `{"type":"utterance","text":…}` or `{"type":"presence","room":…,"people":…}`. Receive `heard`, `say`, `notify`, `memory` |
| `POST /api/utterance` | Everything you hear. FRIDAY decides whether it was addressed |
| `POST /api/ask` | Push-to-talk: always addressed |
| `POST /api/presence` | Room sensors |
| `GET /api/hud` | State, recent speech, notifications, learned scores |

Not built yet: the Pi's own voice loop (whisper.cpp in, voice ID tag, Piper out, talking to `/ws`).
