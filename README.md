# FRIDAY

A proactive assistant that lives in your headphones. Pair them with any laptop, desktop or Pi, run
`friday`, and talk. It answers in a voice, starts talking within a moment of you finishing, stops when you
talk over it, follows the conversation, greets you when you're back, checks in on things you mentioned,
and speaks up when something you care about happens. It assists; it doesn't automate.

```bash
curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/friday/main/install.sh | sh
```

No sudo, no Docker, no server: it installs into `~/.local` (bringing its own Python via
[uv](https://docs.astral.sh/uv/)), then `friday setup` asks your name and which brain to use. After
that, put your headphones on and run `friday`. Everything FRIDAY knows lives in `~/.friday`.

(The one-liner needs this repository to be public; until then, clone it and run
`FRIDAY_SRC=./server sh install.sh`.)

## What it's like

- **Instant.** FRIDAY speaks each sentence the moment it's written, so you hear the start of the answer
  while the rest is still being thought. Talk over it and it stops at once and listens.
- **Hears how you said it.** Its ears are [SenseVoice](https://github.com/FunAudioLLM/SenseVoice): punctuated
  text in about 0.2 s, plus your tone and sounds like laughter. Sound frustrated and FRIDAY knows.
- **Any brain.** Claude by default, or any OpenAI-compatible API: OpenRouter, Ollama, LM Studio,
  llama.cpp, vLLM, OpenAI. Tools, streaming and structured decisions work with all of them.
- **Knows what you're talking about.** Say "Friday, …", follow up without its name, or, when you're alone,
  just ask ("remind me to…", "look that up"). It hears the last few minutes of conversation, so "what do
  you think?" means what you were just discussing.
- **Has a personality.** Quick, warm, a little cheeky, with opinions when you ask. It remembers running
  jokes and how you like to be talked to.
- **Starts things.**
  - Greets you when you put your headphones on after a while away, then catches you up.
  - Checks in on things it heard you planning ("How did the interview go?").
  - Mentions one pattern it noticed overnight ("You've asked about GPUs all week. Want me to watch prices?").
  - Asks about your tastes now and then ("What news are you most interested in?"), never about your
    relationships, family, health or money.
- **Watches for you, for free.** Every 10 minutes, with plain web requests (no AI tokens), it checks for
  new model releases from the big labs and for Hacker News stories about your interests.
- **Knows when not to interrupt.** Reminders always speak. Everything else waits while you have company
  or during quiet hours (22:00–08:00 by default). It learns from "not now" and from what you engage with.
- **Maps your world.** FRIDAY builds a graph of the projects, tools, topics, places and people in your
  life and how they connect, from conversation and every night. `friday graph` shows it (or exports
  Mermaid / Graphviz / JSON).
- **Your data is yours.** `friday export` saves everything to one JSON file; `friday import` merges it
  into another FRIDAY, or learns from any notes or text file you give it.
- **Remembers what matters, forgets the rest.** Each night it keeps facts, a summary and reminders, deletes
  raw speech that didn't matter, and rewrites its own small how-to skills from what it did that day. No
  audio is ever stored.
- **Hands off heavy work.** Research and coding go to [Hermes Agent](https://github.com/NousResearch/hermes-agent);
  FRIDAY tells you when the answer is back.

## One FRIDAY across devices

Run `friday serve` on an always-on machine (or `docker compose up -d`, which does the same), then on each
device:

```bash
friday login http://<that machine>:8080 <token>
```

Now every device shares one memory and one personality. `friday logout` goes back to a FRIDAY of its own.

## Also

- **Everything from the CLI:** `friday setup`, `friday config set NAME VALUE`, `friday doctor`, `friday ask "…"`,
  `friday chat`, `friday status`, `friday memory`, `friday forget "…"`, `friday graph`, `friday export`,
  `friday import`. Type while talking, too.
- **OpenAI-compatible:** point Open WebUI, LibreChat or any OpenAI client at `http://<hub>:8080/v1`, with
  your token as the API key and `friday` as the model.
- **Plugins:** a folder with a `plugin.py` in `~/.friday/plugins/` adds tools, zero-token watchers and skills.
  See `server/friday/plugins.py` and `server/friday/builtin_plugins/models/plugin.py` (30 lines).

**More:** [docs/GUIDE.md](docs/GUIDE.md) (every setting and command) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (diagrams).
Tests: `pip install -e "./server[test]"`, then `cd server && python -m pytest -q`.
Linux without PortAudio uses the system's own `parec`/`paplay` or `arecord`/`aplay`, so nothing needs sudo.

Skills and progressive disclosure are adapted from Hermes Agent by Nous Research (MIT). Speech runs on
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx): SenseVoice for hearing, a Piper voice for speaking.
