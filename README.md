# FRIDAY

A proactive voice assistant that lives in your earbuds. It listens all day, keeps what matters and drops
the rest, learns your tastes by asking, and speaks up when something you care about happens. It assists;
it doesn't automate.

```
Bluetooth earbuds ──► ears (speech detection + Whisper, on your box) ──► FRIDAY server ──► Claude
        ▲                                                                  │   │   │
        └──────────── Piper voice ◄──── replies, alerts, questions ◄───────┘   │   └── plugins: new models,
                                                                               │       Hacker News, Hermes…
                                                       memory + skills (SQLite, SKILL.md)
```

Everything runs in **one Docker container**: the server, the voice loop, every plugin, and every
credential (one `.env` file). No MCP servers.

## What it does

- **Hears you through paired Bluetooth earbuds.** Speech is transcribed on your machine. It answers when
  you say "Friday…", for 10 seconds after it speaks, or when Claude decides an ambiguous line was meant
  for it. Voices it's told aren't yours can never command it.
- **Answers with Claude.** Sign in once with `ant auth login` (or use an API key). Quick decisions
  (was that for me? what's worth remembering?) come back as type-checked structured output.
- **Keeps what's needed, throws away the rest.** Every line gets an importance score. Each night it turns
  the day into a summary, facts about you, and reminders, then deletes raw speech that didn't matter.
  No audio is ever stored.
- **Rebuilds micro skills every night.** It reviews what it did with tools that day and writes or fixes
  small how-tos (`SKILL.md`, the Hermes Agent / agentskills.io format). Only a one-line index is in the
  prompt; the steps load when needed.
- **Gets to know you.** About once a day, at a quiet moment, it asks one question about your
  preferences ("What news are you most interested in?"). It never asks about relationships, family,
  health or money.
- **Speaks up on its own, without spending tokens.** Plugins check sources with plain HTTP:
  - **New models:** every 10 minutes it checks OpenRouter's public model list and tells you when a
    major lab ships one.
  - **Hacker News:** every 10 minutes it checks the front page for stories about your interests.
- **Hands heavy work to Hermes Agent.** Research and coding go to Hermes over its OpenAI-compatible
  API. FRIDAY tells you when the answer is back.
- **Knows when to interrupt.** Reminders and things you asked for always speak. Everything else waits
  while you have company, and learns from your reactions: say "not now" and that kind of alert starts
  waiting for a good moment instead.

## Run it

```bash
cp .env.example .env      # set FRIDAY_TOKEN, your name, and plugin credentials
ant auth login            # Claude sign-in (or set ANTHROPIC_API_KEY in .env)
docker compose up -d --build
```

Pair your earbuds with the host first (see [docs/GUIDE.md](docs/GUIDE.md)). Tests:
`cd server && pip install -r requirements.txt && python -m pytest -q`

## Plugins

A plugin is a folder in `server/plugins/` with a `plugin.py`. It can add tools for Claude, zero-token feeds
for the proactive loop, and skills. See `server/friday/plugins.py` for the whole interface, and
`server/plugins/models/plugin.py` for a 30-line example. Credentials go in `.env`.

**How it works:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (diagrams).
**Setup, hardware and earbuds:** [docs/GUIDE.md](docs/GUIDE.md).

Skills and progressive disclosure are adapted from [Hermes Agent](https://github.com/NousResearch/hermes-agent)
by Nous Research (MIT).
