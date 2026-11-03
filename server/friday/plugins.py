"""Plugins: one folder each with plugin.py defining setup(p). No MCP servers. Built-ins ship in
friday/builtin_plugins; add your own in ~/.friday/plugins/<name>/plugin.py.

A plugin can add:
  - tools the brain calls:     @p.tool("what it does", arg=("string", "meaning"))
  - feeds the proactive loop polls with no model in the loop (zero tokens):
        @p.feed(every_s=600)  async def check(): return [{"key": "...", "kind": "...", "text": "..."}]
    Each key is announced once; return None when the feed isn't set up yet. The first look only learns
    what already exists, and a poll says at most 3 things. The kind goes through the interruption rules
    and the learning loop.
  - skills: a skills/ folder of SKILL.md how-tos next to plugin.py.
Credentials come from the container's environment: p.env("NAME")."""
import importlib.util
import os
import time
from pathlib import Path


MAX_PER_POLL = 3


class Plugin:
    def __init__(self, name, state, tools):
        self.name, self.state, self.tools = name, state, tools
        self.memory, self.http = state.memory, state.http
        self.feeds = []

    def env(self, key, default=""):
        return os.environ.get(key, default)

    def tool(self, doc, **params):
        return self.tools.tool(doc, **params)

    def feed(self, every_s=600):
        def wrap(fn):
            self.feeds.append({"fn": fn, "every": every_s, "next": 0.0, "name": f"{self.name}.{fn.__name__}"})
            return fn
        return wrap

    async def notify(self, kind, text, urgent=False):
        await self.state.notify(kind, text, urgent=urgent)


def load(roots, state, tools):
    """Import every <root>/<name>/plugin.py. A broken plugin is reported and skipped, never fatal."""
    plugins, errors = [], []
    for f in [f for r in roots for f in sorted(Path(r).glob("*/plugin.py"))]:
        name = f.parent.name
        try:
            spec = importlib.util.spec_from_file_location(f"friday_plugin_{name}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            p = Plugin(name, state, tools)
            mod.setup(p)
            plugins.append(p)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
    return plugins, errors


async def poll(plugins, memory, notify, now=None):
    """Run the feeds that are due. Returns what was announced."""
    now = now or time.time()
    said = []
    for p in plugins:
        for f in p.feeds:
            if now < f["next"]:
                continue
            f["next"] = now + f["every"]
            try:
                items = await f["fn"]()
            except Exception:
                continue  # sources flake; next poll tries again
            if items is None:
                continue  # the feed isn't set up yet (e.g. no interests): don't use up its first look
            first_run = memory.first_time(f"{f['name']}:__seeded__")  # day one: learn what exists, say nothing
            new = [i for i in items if memory.first_time(f"{f['name']}:{i['key']}")]
            for item in [] if first_run else new[:MAX_PER_POLL]:  # a burst is noise: the rest are marked seen
                said.append((item["kind"], item["text"]))
                await notify(item["kind"], item["text"])
    return said
