# Friday

An agent harness for coding and automation that thinks fast first. A typed decision model and
two small on-device models handle routing, tool choice, skill choice, vision and verification in
milliseconds; the big model you choose is called only for the thinking that needs it.

```bash
curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/friday/main/install.sh | sh
friday
```

One binary in `~/.local/bin`. Nothing installed system-wide: when no prebuilt binary fits your
machine, the installer builds with a private Rust toolchain in a temporary folder and deletes it.
Memory is built in and local. Any OpenAI-compatible model: Ollama, OpenRouter, OpenAI, Anthropic,
Gemini, vLLM, LM Studio.

## System 1 and System 2

| | Model | Runs | Does |
|---|---|---|---|
| **decide** | [Jev](https://docs.typesafe.ai) (TypeSafe) | API, ms | for every request: **Needle alone or an LLM, and which LLM**; which tool and tool groups; which MCP servers; which skills, ranked. Also checks Needle's call before it runs, judges whether a reply resolved the request, picks what is worth remembering, and decides when work becomes a skill |
| **act** | [Needle 3](https://cactuscompute.com/needle) (Cactus) | on-device | fills in the tool call when Jev says one call does it: "read notes.txt", "run cargo test", "fetch https://…" never reach an LLM |
| **see** | [Mirage](https://cactuscompute.com/blog/mirage) (Cactus) | on-device | reads the screen (OCR, objects, boxes) for desktop control |
| **think** | yours: `fast`, `main`, `deep` | anywhere | everything that needs reasoning |

You see the decision on every turn:

```
› read notes.txt
  jev → needle only · no LLM
  ⚡ read_file  notes.txt
    ⎿ remember the milk
  ⚡ needle · system 1 ×4 · system 2 ×0 · 0.6s

› the upload times out, fix the config
  jev → llm: main (claude-sonnet-5-5)
  ⏺ shell  grep -n timeout config.toml
  ⏺ edit_file  config.toml
```

A Needle call runs only if it passes every check: Jev chose the tool, every argument appears in
your own words, and Jev confirms the finished call does exactly what you asked. Otherwise the
request goes to the `fast` model, and the line says why. On the real model, 5 of 8 everyday
requests are handled with no LLM, and none of the rest is ever a wrong action
(`tests/cactus_live.rs`).

An LLM turn that fails, or that Jev judges unresolved, climbs to the next tier. Without a
`TYPESAFE_API_KEY` your `fast` model stands in for Jev.

## Skills

`SKILL.md` folders, the format Claude Code and Hermes use; `~/.claude/skills` and
`~/.hermes/skills` work as they are.

- **Chosen per request**: local embeddings shortlist the 24 closest, Jev ranks them and the best
  (up to 3) go into the prompt. No skill index sits in the context, at any library size.
- **Progressive**: a skill's scripts and references are listed by path and read only when a step
  needs them.
- **Scored**: every use is a win or a loss. A skill that keeps losing is retired, then rewritten
  from the failure and offered again.
- **Learned**: after multi-step work Jev decides whether it becomes a new skill or a rewrite of
  the one followed. The agent can also `skill_view` and `skill_save` mid-task. Every rewrite keeps
  the old version in `history/`.
- **Yours to direct**: `/deploy` (or `--skill deploy`) runs a turn with that skill;
  `friday skills add <git-url>` installs a pack; `friday skills` shows each one's record.
- **Safe by default**: skills shipped inside a project folder load only after you trust it.

## Memory

One SQLite file in `~/.friday`; nothing leaves the machine.

- **The palace** (after [MemPalace](https://github.com/MemPalace/mempalace)): every word
  verbatim, filed by who said it and when, append-only, never summarised in place of the original.
  Full-text ranking fused with local MiniLM embeddings.
- **Peers** (after [Honcho](https://github.com/plastic-labs/honcho)): a background deriver turns
  what you say into conclusions about you (Jev decides which messages say anything lasting), a
  dream pass consolidates them into a profile, and the agent can `ask_about_user`.

Relevant memories and what is known about you are injected into the system prompt each turn, not
piled into the history. When the context fills, old turns are filed verbatim and summarised.

## The harness

- **MCP servers** (`[[mcp]]` in the config). Each server is a yes/no for Jev, so a dozen
  connected servers cost no context until a request needs one.
- **Checkpoints**: before the first file change of a turn, the folder is snapshotted into a
  shadow git store; `/undo` or `friday undo` puts it back. Your own git history is untouched.
- **Project instructions** from `AGENTS.md`, `CLAUDE.md` or `.friday/FRIDAY.md`.
- **Tools**: files, shell (300 s timeout), memory, todo list, ask the user, fetch a URL, a real
  browser (fetches its own Chromium if you have none), desktop control, skills.
- **Agents**: parallel `delegate`, and agents that message each other by name across processes.
- **Proactive**: `friday watch` acts on timers, on a command whose output changed, and on mail
  from other agents; Jev drops the noise before any LLM sees it.
- **Ctrl-C** stops the running turn; a second one quits. `--continue` picks a session back up.
- **Self-training**: every call the LLM made that Needle could have made is saved for
  fine-tuning Needle (`friday train`).

## Use

```
friday                           talk (slash commands: /help)
friday run "fix the flaky test"  one task; only the answer on stdout
echo "summarise this" | friday   same, from a pipe
friday --continue                pick up the last conversation
friday watch                     act on events, proactively
friday --as scout watch          a second agent; message it with `friday send scout "…"`
friday skills [add <git-url>]    skills and their record; install a pack
friday undo                      revert Friday's last change in this folder
friday logo path/to/logo.png     your logo on the opening screen (`--reset` for the coin)
friday doctor                    test every model and part, for real
friday train                     the Needle fine-tuning data collected so far
```

Writes and commands that could change anything ask first (`--yolo` to stop asking).

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

[[mcp]]
name = "github"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env = { GITHUB_PERSONAL_ACCESS_TOKEN = "…" }
trust = false             # true runs its tools without asking

[[watch]]
name = "repo"
every = 300
cmd = "git -C ~/code/app status --porcelain"
prompt = "The working tree changed. Tell me if anything needs attention."
```

`friday` with no config walks you through it. Set `TYPESAFE_API_KEY` for Jev.

## Build

```bash
cargo build --release
cargo test        # the live-model tests download Needle, Mirage and MiniLM, or skip
```

Desktop control needs a display and is a feature: `cargo build --release --features desktop`
(on Linux: `apt install libdbus-1-dev libxcb1-dev libxrandr-dev libpipewire-0.3-dev libgbm-dev libxdo-dev`).
