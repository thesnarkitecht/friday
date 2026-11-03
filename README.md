# FRIDAY

A proactive voice assistant you talk to through whatever headphones you're wearing. It listens all day,
follows the conversation, keeps what matters and drops the rest, learns your tastes by asking, and
speaks up when something you care about happens. It assists; it doesn't automate.

```
any device + paired headphones                    one Docker container
┌──────────────────────────┐   audio / text   ┌────────────────────────────────────────────┐
│ friday                   │ ───────────────► │ Whisper → "meant for me?" → Claude + tools │
│  mic → speech detection  │                  │ memory · nightly skills · questions        │
│  speaker ◄─ FRIDAY voice │ ◄─────────────── │ Piper voice · plugins (models, HN, Hermes) │
└──────────────────────────┘   speech / text  └────────────────────────────────────────────┘
```

## What it does

- **One command, any device.** Pair your headphones with a laptop, desktop or Pi, run `friday`, and
  talk. Speech is transcribed on your own server; nothing goes anywhere else except the words FRIDAY
  sends to Claude.
- **Knows what you're talking about.** It answers "Friday, …", follow-ups, and, when you're alone, plain
  requests like "remind me to…" or "look that up". It sees the last few minutes of conversation, so "what
  do you think?" means what you were just discussing.
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
  while you have company or during quiet hours (22:00–08:00 by default), and learns from your reactions:
  say "not now" and that kind of alert starts waiting for a good moment instead.
- **Also typed.** Type while talking, or use `friday ask`, `friday chat`, `friday status`,
  `friday memory` and `friday forget`.
- **Works with any OpenAI-compatible client.** Point Open WebUI, LibreChat, a script, or anything that
  speaks the OpenAI API at `http://<box>:8080/v1`, with your `FRIDAY_TOKEN` as the API key and `friday`
  as the model. Streaming works.

## Run it

On the always-on box:

```bash
cp .env.example .env      # set FRIDAY_TOKEN, your name, and plugin credentials
ant auth login            # Claude sign-in (or set ANTHROPIC_API_KEY in .env)
docker compose up -d --build
```

On each device you'll talk from (headphones paired):

```bash
pip install ./server
```

```bash
friday login http://<box>:8080 <FRIDAY_TOKEN>
```

```bash
friday
```

Any OpenAI client works too:

```python
from openai import OpenAI
friday = OpenAI(base_url="http://<box>:8080/v1", api_key="<FRIDAY_TOKEN>")
print(friday.chat.completions.create(model="friday", messages=[{"role": "user", "content": "Anything new?"}]).choices[0].message.content)
```

Setup details, headphone tips and every command: [docs/GUIDE.md](docs/GUIDE.md).

Tests:
`cd server && pip install -r requirements.txt && python -m pytest -q`

## Plugins

A plugin is a folder in `server/plugins/` with a `plugin.py`. It can add tools for Claude, zero-token feeds
for the proactive loop, and skills. See `server/friday/plugins.py` for the whole interface, and
`server/plugins/models/plugin.py` for a 30-line example. Credentials go in `.env`.

**How it works:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (diagrams).

Skills and progressive disclosure are adapted from [Hermes Agent](https://github.com/NousResearch/hermes-agent)
by Nous Research (MIT).
