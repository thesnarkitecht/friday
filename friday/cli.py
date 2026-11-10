"""The `friday` command."""

import argparse
import json
import os
import sys
import time
import urllib.request

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from . import coin, config, skills, stack
from .agent import Agent
from .bus import Bus
from .memory import honcho_url
from .models import TIERS, chat, spec

ACCENT, BOLT, DIM = "rgb(142,162,255)", "rgb(245,180,84)", "grey50"
console = Console(highlight=False)

COMMANDS = {
    "/help": "show this",
    "/tier": "pin a model tier: /tier fast|main|deep, or /tier auto to let System 1 choose",
    "/skills": "list skills and how they have performed",
    "/stats": "how much of this session System 1 handled",
    "/yolo": "toggle approval prompts",
    "/clear": "start a fresh conversation (memory is kept)",
    "/exit": "leave",
}


# -- first run ----------------------------------------------------------------------------

PROVIDERS = [  # (name, base_url, key env var, suggested fast/main/deep models)
    ("Ollama (local)", "http://localhost:11434/v1", "", None),
    ("OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", None),
    ("OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY", None),
    ("Anthropic", "https://api.anthropic.com/v1/", "ANTHROPIC_API_KEY",
     ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")),
    ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", None),
    ("Another OpenAI-compatible endpoint", "", "", None),
]


def _ollama_models() -> list[str]:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=0.5) as r:
            return [m["name"] for m in json.load(r)["models"]]
    except (OSError, ValueError, KeyError):
        return []


def init() -> None:
    """Ask the three things Friday cannot guess and write ~/.friday/config.toml."""
    local = _ollama_models()
    console.print("\n[bold]Where should Friday think?[/]  (System 1 runs on this machine regardless)\n")
    for i, (name, _, env, _) in enumerate(PROVIDERS, 1):
        found = (name.startswith("Ollama") and local) or (env and os.environ.get(env))
        console.print(f"  [{ACCENT}]{i}[/]  {name}" + (f"  [green]detected[/]" if found else ""))
    pick = console.input(f"\n[{ACCENT}]›[/] number: ").strip()
    name, base, env, suggested = PROVIDERS[int(pick) - 1 if pick.isdigit() and 0 < int(pick) <= len(PROVIDERS) else 0]
    if not base:
        base = console.input("  base URL: ").strip()
        env = console.input("  env var holding the API key (blank if none): ").strip()
    if name.startswith("Ollama") and local:
        console.print(f"  [{DIM}]installed: {', '.join(local)}[/]")
    hint = suggested or ("", "", "")
    ask = lambda label, default: console.input(f"  {label} model" + (f" [{default}]" if default else "") + ": ").strip() or default
    main = ask("main", hint[1])
    fast = ask("fast (small and cheap; blank = same as main)", hint[0]) or main
    deep = ask("deep (strongest; blank = same as main)", hint[2]) or main
    lines = [f"user = {json.dumps(os.environ.get('USER', 'user'))}", ""]
    for tier, model in zip(TIERS, (fast, main, deep)):
        lines += [f"[models.{tier}]", f"model = {json.dumps(model)}", f"base_url = {json.dumps(base)}"]
        lines += ([f"api_key_env = {json.dumps(env)}"] if env else []) + [""]
    config.HOME.mkdir(parents=True, exist_ok=True)
    path = config.HOME / "config.toml"
    path.write_text("\n".join(lines))
    console.print(f"\n  wrote [bold]{path}[/]")
    if env and not os.environ.get(env):
        console.print(f"  [yellow]set {env} in your shell before chatting[/]")
    if not os.environ.get("TYPESAFE_API_KEY"):
        console.print(f"  [{DIM}]set TYPESAFE_API_KEY to let Jev do routing, skill choice and verification[/]")
    console.print(f"  [{DIM}]`friday up` starts the bundled Honcho; `friday doctor` checks everything[/]\n")


# -- the conversation ---------------------------------------------------------------------


def _tick(ok: bool) -> str:
    return "[green]●[/]" if ok else f"[{DIM}]○[/]"


def banner(cfg: dict, agent: Agent) -> Table:
    models = " · ".join(dict.fromkeys(spec(cfg, t)["model"] for t in TIERS))
    info = Table.grid(padding=(0, 2))
    info.add_row("")
    info.add_row(Text("F R I D A Y", style=f"bold {ACCENT}"))
    info.add_row(Text("fast decisions first, deep thought when it counts", style=DIM))
    info.add_row("")
    rows = Table.grid(padding=(0, 2))
    rows.add_row(Text("system 1", style=BOLT), f"{_tick(agent.s1.has_jev)} jev   [green]●[/] needle   [green]●[/] mirage")
    rows.add_row(Text("system 2", style=ACCENT), models)
    rows.add_row(Text("memory", style=DIM), f"[green]●[/] mempalace   {_tick(agent.memory.honcho_on)} honcho")
    rows.add_row(Text("skills", style=DIM), str(len(skills.load())))
    info.add_row(rows)
    info.add_row("")
    info.add_row(Text("/help for commands · ctrl-d to leave", style=DIM))
    return info


class Screen:
    """Renders one agent's activity: status lines, streamed Markdown, approvals."""

    def __init__(self, agent: Agent):
        self.agent, self.live, self.buffer, self.shown = agent, None, "", ""
        self.status = console.status(Text("thinking", style=DIM), spinner="dots", spinner_style=BOLT)
        agent.out, agent.stream, agent.ask = self.line, self.token, self.ask

    def line(self, text: str) -> None:
        console.print(Text(text, style=BOLT if "⚡" in text or text.lstrip().startswith("jev") else DIM))

    def token(self, delta: str | None) -> None:
        if delta is None:  # the message is complete
            if self.live:
                self.live.stop()
                self.live, self.shown, self.buffer = None, self.buffer, ""
                self.status.start()
            return
        self.buffer += delta
        if not self.live:
            self.status.stop()
            self.live = Live(console=console, refresh_per_second=12, vertical_overflow="visible")
            self.live.start()
        self.live.update(Markdown(self.buffer))

    def ask(self, prompt: str) -> str:
        self.status.stop()
        try:
            return console.input(Text(prompt, style="yellow"))
        finally:
            self.status.start()

    def turn(self, text: str) -> None:
        a, before, started = self.agent, dict(self.agent.stats), time.monotonic()
        self.shown = ""
        self.status.start()
        try:
            reply = a.turn(text)
            if a._learner:  # let any "learned skill" line land before the prompt returns
                self.status.update(Text("learning", style=DIM))
                a._learner.join()
                self.status.update(Text("thinking", style=DIM))
        finally:
            self.status.stop()
        if reply.strip() and reply != self.shown:  # reflex results and errors are not streamed
            console.print(Markdown(reply) if a.last_tier != "reflex" else Text(reply))
        s2 = a.stats["s2"] - before.get("s2", 0)
        s1 = sum(a.stats[k] - before.get(k, 0) for k in ("jev", "needle", "mirage"))
        console.print(
            Text(
                f"  {a.last_tier} · system 1 ×{s1} · system 2 ×{s2} · "
                f"context {a.context_used:.0%} · {time.monotonic() - started:.1f}s",
                style=DIM,
            )
        )
        console.print()


def show_skills() -> None:
    marks, table = skills.scores(), Table(box=None, padding=(0, 2), header_style=DIM)
    for column in ("skill", "used", "won", "", "when"):
        table.add_column(column)
    for name, (desc, _) in sorted(skills.load().items()):
        uses, wins = marks.get(name, [0, 0])
        state = "" if skills.healthy(marks.get(name)) else "[red]retired[/]"
        table.add_row(Text(name, style=ACCENT), str(uses), str(wins), state, Text(desc[:70], style=DIM))
    console.print(table if table.row_count else Text("  no skills yet; Friday writes them as it works", style=DIM))


def show_stats(agent: Agent) -> None:
    s = agent.stats
    s1 = s["jev"] + s["needle"] + s["mirage"]
    console.print(
        f"  system 1: {s1} calls (jev {s['jev']}, needle {s['needle']}, mirage {s['mirage']})   "
        f"system 2: {s['s2']} calls   browser steps: {s['operate_s1']} fast / {s['operate_s2']} slow"
    )
    for backend, error in agent.memory.errors.items():
        console.print(f"  [yellow]{backend}[/]: {error[:200]}")
    if agent.s1.last_error:
        console.print(f"  [yellow]system 1[/]: {agent.s1.last_error[:200]}")


def chat_loop(agent: Agent, cfg: dict, animate: bool) -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory

    screen = Screen(agent)
    coin.spin(console, banner(cfg, agent), seconds=1.7 if animate else 0)
    console.print()
    session = PromptSession(
        history=FileHistory(str(config.HOME / "history")),
        completer=WordCompleter(list(COMMANDS), sentence=True),
    )
    toolbar = lambda: HTML(
        f" tier <b>{agent.force_tier or 'auto'}</b> · approvals <b>{'off' if agent.yolo else 'on'}</b>"
        f" · context <b>{agent.context_used:.0%}</b>"
    )
    while True:
        try:
            text = session.prompt(HTML('<style fg="#8ea2ff"><b>› </b></style>'), bottom_toolbar=toolbar).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if not text:
            continue
        command, _, arg = text.partition(" ")
        if command == "/exit":
            break
        elif command == "/help":
            for name, what in COMMANDS.items():
                console.print(f"  [{ACCENT}]{name:8}[/] [{DIM}]{what}[/]")
        elif command == "/tier":
            agent.force_tier = arg if arg in TIERS else None
        elif command == "/skills":
            show_skills()
        elif command == "/stats":
            show_stats(agent)
        elif command == "/yolo":
            agent.yolo = not agent.yolo
        elif command == "/clear":
            agent.messages.clear()
        elif command.startswith("/"):
            console.print(f"  [{DIM}]unknown command; /help lists them[/]")
        else:
            try:
                screen.turn(text)
            except KeyboardInterrupt:
                screen.token(None)
                screen.status.stop()
                console.print(Text("  interrupted", style=DIM))
    agent.memory.flush()


# -- doctor -------------------------------------------------------------------------------


def doctor(cfg: dict) -> None:
    """Exercise every part for real and say what is wrong in plain words."""

    def check(label: str, fn) -> None:
        started = time.monotonic()
        try:
            detail, ok = fn(), True
        except Exception as e:
            detail, ok = f"{type(e).__name__}: {e}"[:160], False
        mark = "[green]ok  [/]" if ok else "[red]fail[/]"
        console.print(f"  {mark} {label:22} [{DIM}]{detail}  ({(time.monotonic() - started) * 1000:.0f} ms)[/]")

    from .system1 import S1

    s1 = S1(cfg)
    for tier in TIERS:
        check(f"system 2 · {tier}", lambda t=tier: f"{spec(cfg, t)['model']} said {chat(cfg, t, [{'role': 'user', 'content': 'Say ok.'}])[0].content!r:.40}")

    def jev():
        if not s1.has_jev:
            raise RuntimeError("TYPESAFE_API_KEY is not set; decisions fall back to the fast model")
        a = s1._decide_jev("The sky is blue.", {"q": {"type": "noul", "instructions": "The text mentions a colour."}})["q"]
        return f"answered {a.value} at confidence {a.confidence:.2f}"

    def needle():
        tool = {"name": "read_file", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
        calls, conf = s1.act("read the file notes.txt", [tool])
        if not calls:
            raise RuntimeError(s1.last_error or "no call returned")
        return f"{calls[0]['name']}({calls[0]['arguments']}) at confidence {conf:.2f}"

    def mirage():
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (320, 120), "white")
        ImageDraw.Draw(image).text((40, 50), "Friday", fill="black")
        return f"read {[t['string'] for t in s1.see(image).get('text', [])]}"

    def mempalace():
        from mempalace.mcp_server import tool_search

        r = tool_search(query="friday doctor", limit=1)
        if r.get("error"):
            raise RuntimeError(r["error"])
        return "search works"

    def honcho():
        url = honcho_url(cfg)
        if not url:
            raise RuntimeError("not running; `friday up` starts the bundled one")
        urllib.request.urlopen(f"{url}/health", timeout=3)
        return url

    def importable(module: str, fix: str):
        def run():
            try:
                __import__(module)
            except ImportError:
                raise RuntimeError(fix) from None
            return "installed"

        return run

    check("system 1 · jev", jev)
    check("system 1 · needle", needle)
    check("system 1 · mirage", mirage)
    check("memory · mempalace", mempalace)
    check("memory · honcho", honcho)
    check("browser", importable("playwright", "uv sync --extra browser && playwright install chromium"))
    check("desktop control", importable("pyautogui", "uv sync --extra desktop"))


# -- entry --------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--yolo", action="store_true", help="never ask before writing or running")
    common.add_argument("--as", dest="name", default="friday", help="this agent's name on the bus")
    common.add_argument("--tier", choices=TIERS, help="pin a model tier instead of letting System 1 choose")
    common.add_argument("--no-anim", action="store_true", help="skip the opening animation")
    parser = argparse.ArgumentParser(prog="friday", description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", parents=[common], help="do one task and exit").add_argument("task")
    sub.add_parser("watch", parents=[common], help="stay running and act on events (see [[watch]] in the config)")
    sub.add_parser("up", help="start the bundled Honcho")
    sub.add_parser("down", help="stop the bundled Honcho")
    sub.add_parser("init", help="choose models")
    sub.add_parser("doctor", help="test every part")
    sub.add_parser("skills", help="list skills and their record")
    sub.add_parser("train", help="show the Needle training data Friday has collected")
    send = sub.add_parser("send", parents=[common], help="message another agent")
    send.add_argument("to")
    send.add_argument("text")
    args = parser.parse_args(argv)

    if args.cmd == "init":
        return init()
    if args.cmd == "skills":
        return show_skills()
    if args.cmd == "down":
        sys.exit(0 if stack.down() else 1)
    if args.cmd == "send":
        Bus().send(args.name, args.to, args.text)
        return console.print(f"  sent to {args.to}")
    if args.cmd == "train":
        path = config.HOME / "train" / "needle.jsonl"
        count = sum(1 for _ in path.open()) if path.exists() else 0
        console.print(f"  {count} examples in {path}")
        console.print(f"  [{DIM}]cactus finetune {path} --out needle-friday.safetensors[/]")
        console.print(f"  [{DIM}]cactus build --lora needle-friday.safetensors --out needle-friday.cact[/]")
        return console.print(f"  [{DIM}]then set [s1] needle_weights = \"needle-friday.cact\"[/]")

    cfg = config.load()
    piped = None if sys.stdin.isatty() else sys.stdin.read().strip()
    if not cfg["models"]:
        if args.cmd or piped:
            sys.exit("No model configured. Run `friday init`.")
        coin.spin(console, Text("\n\n\n  F R I D A Y\n\n  Let's pick your models.", style=f"bold {ACCENT}"), seconds=0 if args.no_anim else 1.7)
        init()
        cfg = config.load()
    if args.cmd == "up":
        sys.exit(0 if stack.up(cfg, console.print) else 1)
    if args.cmd == "doctor":
        return doctor(cfg)

    interactive = args.cmd is None and piped is None
    agent = Agent(cfg, name=args.name, interactive=interactive, yolo=args.yolo)
    agent.force_tier = args.tier
    if args.cmd == "watch":
        from .proactive import watch

        agent.out = lambda line: console.print(Text(line, style=DIM))
        console.print(Text(f"  {agent.name} is watching {len(cfg['watch'])} sources and its inbox. ctrl-c to stop.", style=DIM))
        try:
            watch(agent)
        except KeyboardInterrupt:
            pass
    elif interactive:
        chat_loop(agent, cfg, animate=not args.no_anim)
    else:
        agent.out = lambda line: print(line, file=sys.stderr)  # stdout carries only the answer
        print(agent.turn(args.task if args.cmd == "run" else piped))
    agent.memory.flush()


if __name__ == "__main__":
    main()
