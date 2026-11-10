# Friday

An agent harness for coding and automation that thinks fast first. Small on-device models and a
typed decision model handle routing, skill choice, tool calls, vision and verification in
milliseconds; the big model you choose is called only for the thinking that needs it.

```bash
curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/friday/main/install.sh | sh
friday
```

One Rust binary. Memory is built in and local. Any OpenAI-compatible model: Ollama, OpenRouter,
OpenAI, Anthropic, Gemini, vLLM, LM Studio.

## System 1 and System 2

| | Model | Runs | Does |
|---|---|---|---|
| **decide** | [Jev](https://docs.typesafe.ai) (TypeSafe) | API, ~ms | picks the model tier, the tool groups and the best skills for each request; judges whether a reply resolved it; decides what memory is worth keeping and which skills to write |
| **act** | [Needle 3](https://cactuscompute.com/needle) (Cactus) | on-device | makes single tool calls with no big model at all ("read notes.txt" never leaves your machine); types into fields during browser work |
| **see** | [Mirage](https://cactuscompute.com/blog/mirage) (Cactus) | on-device | reads the screen (OCR, objects, boxes) so desktop control can click |
| **think** | yours: `fast`, `main`, `deep` tiers | anywhere | the work that needs reasoning |

Every System 1 answer carries a confidence, and every caller gates on it: below the threshold the
decision goes up to System 2. Without a `TYPESAFE_API_KEY` your `fast` model stands in for Jev.

A turn:

1. **Plan** (Jev, one call): `reflex`, `fast`, `main` or `deep`; which tool groups (web, desktop,
   agents) to load at all; which saved skills apply, ranked.
2. **Reflex**: if Jev says one tool call does it, Needle makes it on-device. System 2 isn't called.
3. **Think**: the chosen tier runs the tool loop, streaming. A failure or a reply Jev judges
   unresolved climbs to the next tier.
4. **Learn** (background): every skill used is scored. Jev decides whether multi-step work becomes
   a new skill, or a rewrite of the skill that was followed; a skill that keeps losing is retired.
   Every tool call System 2 made that Needle could have becomes fine-tuning data (`friday train`).

## Memory

Built in, one SQLite file in `~/.friday`, nothing leaves the machine:

- **The palace** (after [MemPalace](https://github.com/MemPalace/mempalace)): every word verbatim,
  filed by who said it and when, append-only, never summarised in place of the original. Search
  fuses full-text ranking with local MiniLM embeddings.
- **Peers** (after [Honcho](https://github.com/plastic-labs/honcho)): a background deriver turns
  what you say into conclusions about you (Jev decides which messages say anything lasting), a
  dream pass consolidates them into a profile, and the agent can `ask_about_user`.

Each turn, relevant memories and what is known about you are injected into the system prompt, not
piled into the history. When the context fills, old turns are filed verbatim and replaced by a
summary.

## Use

```
friday                      talk (slash commands: /help)
friday run "fix the flaky test in tests/api"     one task; only the answer on stdout
echo "summarise this" | friday                   same, from a pipe
friday watch                act on [[watch]] events and agent mail, proactively
friday --as scout watch     a second agent; agents message each other by name
friday send scout "check the deploy"
friday skills               skills and their win record
friday doctor               test every model and part, for real
friday train                the Needle fine-tuning data collected so far
```

Writes and non-read-only commands ask first (`--yolo` to stop asking). Skills are `SKILL.md`
folders, the same format as Claude Code and Hermes, and Friday reads `~/.claude/skills` and
`~/.hermes/skills` as they are.

### Config: `~/.friday/config.toml`

```toml
[models.fast]
model = "llama3.2"
base_url = "http://localhost:11434/v1"

[models.main]
model = "claude-sonnet-5-5"
base_url = "https://api.anthropic.com/v1"
api_key_env = "ANTHROPIC_API_KEY"   # the variable's name; keys stay in your environment

[s1]
confidence = 0.6          # Jev answers below this fall back to the safe default
needle_confidence = 0.5   # Needle calls below this go to System 2

[[watch]]
name = "repo"
every = 300
cmd = "git -C ~/code/app status --porcelain"
prompt = "The working tree changed. Tell me if anything needs attention."
```

`friday` with no config walks you through this.

## Build

```bash
cargo build --release
cargo test        # the live-model tests download Needle, Mirage and MiniLM, or skip
```

Desktop control needs a display and is a feature: `cargo build --release --features desktop`
(on Linux: `apt install libdbus-1-dev libxcb1-dev libxrandr-dev libpipewire-0.3-dev libgbm-dev libxdo-dev`).
The browser tool uses an installed Chrome or Chromium (`FRIDAY_CHROMIUM` to point at one).
