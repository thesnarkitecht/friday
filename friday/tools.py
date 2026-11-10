"""Tools. A tool is a typed function; its schema is read from the signature and docstring.

Groups let System 1 leave whole capabilities out of the context when a request doesn't need them.
"""

import inspect
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TOOLS: dict[str, tuple[dict, callable, str]] = {}  # name -> (schema, fn, group)
_JSON = {str: "string", int: "integer", float: "number", bool: "boolean", list[str]: "array"}
MAX_OUTPUT = 8000
DENIED = "denied: the user did not approve this (unattended runs need --yolo)"


def tool(group: str):
    def register(fn):
        params = list(inspect.signature(fn).parameters.values())[1:]  # first is the agent
        props = {}
        for p in params:
            props[p.name] = {"type": _JSON[p.annotation]}
            if p.annotation == list[str]:
                props[p.name]["items"] = {"type": "string"}
        schema = {
            "name": fn.__name__,
            "description": inspect.getdoc(fn),
            "parameters": {
                "type": "object",
                "properties": props,
                "required": [p.name for p in params if p.default is p.empty],
            },
        }
        TOOLS[fn.__name__] = (schema, fn, group)
        return fn

    return register


def schemas(groups: set[str]) -> list[dict]:
    return [s for s, _, g in TOOLS.values() if g in groups]


def clip(text: str) -> str:
    """Keep the head and tail of long output: errors usually sit at one end."""
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n... [{len(text) - MAX_OUTPUT} chars omitted] ...\n{text[-half:]}"


# -- core: files and shell ----------------------------------------------------------------

READ_ONLY = {"ls", "cat", "head", "tail", "wc", "pwd", "rg", "grep", "find", "which", "date", "tree"}


def _is_read_only(command: str) -> bool:
    words = command.split()
    return bool(words) and words[0] in READ_ONLY and not any(c in command for c in ";|&<>`$\n")


@tool("core")
def read_file(agent, path: str) -> str:
    """Read a text file."""
    return Path(path).expanduser().read_text(errors="replace")


@tool("core")
def write_file(agent, path: str, content: str) -> str:
    """Create or overwrite a file with the given content."""
    if not agent.approve(f"write {path}"):
        return DENIED
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} chars to {path}"


@tool("core")
def edit_file(agent, path: str, old: str, new: str) -> str:
    """Replace the single exact occurrence of `old` in a file with `new`."""
    p = Path(path).expanduser()
    text = p.read_text()
    if text.count(old) != 1:
        return f"error: `old` occurs {text.count(old)} times; it must occur exactly once"
    if not agent.approve(f"edit {path}"):
        return DENIED
    p.write_text(text.replace(old, new))
    return f"edited {path}"


@tool("core")
def shell(agent, command: str) -> str:
    """Run a shell command in the working directory and return its output."""
    if not _is_read_only(command) and not agent.approve(f"run `{command}`"):
        return DENIED
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return "error: timed out after 300s"
    return f"exit {r.returncode}\n{r.stdout}{r.stderr}"


# -- memory -------------------------------------------------------------------------------


@tool("core")
def recall(agent, query: str) -> str:
    """Search long-term memory for what was said or done before. Returns the exact words."""
    return agent.memory.recall(query, limit=8) or "nothing found"


@tool("honcho")
def ask_about_user(agent, question: str) -> str:
    """Ask what is known about the user: preferences, habits, goals, how they like things done.
    Use it before guessing what they would want."""
    return agent.memory.ask(question)


# -- agents -------------------------------------------------------------------------------


@tool("agents")
def delegate(agent, tasks: list[str], role: str = "") -> str:
    """Run independent tasks in parallel, each in a fresh sub-agent, and return their reports.
    Give each task everything it needs: sub-agents do not see this conversation."""
    if agent.depth >= 2:
        return "error: sub-agents may not delegate further"
    subs = [agent.spawn(f"{agent.name}.{i + 1}", role) for i in range(len(tasks))]
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(lambda pair: pair[0].turn(pair[1]), zip(subs, tasks)))
    return "\n\n".join(f"## {s.name}\n{r}" for s, r in zip(subs, reports))


@tool("agents")
def send_message(agent, to: str, text: str) -> str:
    """Send a message to another agent by name. They read it on their next inbox check."""
    agent.bus.send(agent.name, to, text)
    return f"sent to {to}"


@tool("agents")
def read_inbox(agent) -> str:
    """Read unread messages other agents sent to you."""
    mail = agent.bus.inbox(agent.name)
    return "\n".join(f"from {s}: {b}" for s, b in mail) or "no new messages"


# -- acting in the world (System 1 drives; see operate.py) --------------------------------


@tool("web")
def browse(agent, goal: str, url: str = "") -> str:
    """Do something in a web browser: open a URL, click, type, read. Describe the goal in plain
    words; returns the final page's text."""
    from .operate import Browser, operate

    if not agent.approve(f"browse: {goal} {url}".strip()):
        return DENIED
    return operate(agent, Browser(url), goal)


@tool("desktop")
def use_computer(agent, goal: str) -> str:
    """Operate this computer's screen with mouse and keyboard to reach a goal."""
    from .operate import Desktop, operate

    if not agent.approve(f"control the mouse and keyboard to: {goal}"):
        return DENIED
    return operate(agent, Desktop(agent.s1.see), goal)
