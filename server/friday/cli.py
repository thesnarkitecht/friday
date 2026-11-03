"""Drive FRIDAY from a terminal. Talks to the server, so it works on the box or anywhere on your network.

    friday ask "did a new model drop today?"     one question, one answer
    friday chat                                   back-and-forth until Ctrl-D
    friday heard "pass the salt"                  as if overheard: FRIDAY decides if it was for it
    friday watch                                  live: what FRIDAY hears, says and holds back
    friday status                                 plugins, situation, what it learned, skills, routines
    friday memory [query]                         what FRIDAY knows about you (or a search)
    friday forget "text"                          delete facts containing text

Set FRIDAY_URL (default http://localhost:8080) and FRIDAY_TOKEN."""
import argparse
import asyncio
import json
import os
import sys

import httpx

DIM, BOLD, CYAN, YELLOW, OFF = ("\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m") if sys.stdout.isatty() \
    else ("",) * 5


def client():
    token = os.environ.get("FRIDAY_TOKEN")
    if not token:
        sys.exit("Set FRIDAY_TOKEN (the same one the server uses).")
    return httpx.Client(base_url=os.environ.get("FRIDAY_URL", "http://localhost:8080"), timeout=300,
                        headers={"Authorization": f"Bearer {token}"})


def call(c, method, path, **kw):
    try:
        r = c.request(method, path, **kw)
    except httpx.HTTPError as e:
        sys.exit(f"Can't reach FRIDAY at {c.base_url}: {e}")
    if r.status_code == 401:
        sys.exit("FRIDAY refused the token. Check FRIDAY_TOKEN.")
    if r.status_code >= 400:
        sys.exit(f"FRIDAY said {r.status_code}: {r.text}")
    return r.json()


def speak(r):
    print(f"{CYAN}FRIDAY{OFF} {r['reply']}")
    if r.get("catch_up"):
        print(f"{YELLOW}FRIDAY{OFF} {r['catch_up']}")


def cmd_ask(c, a):
    speak(call(c, "POST", "/api/ask", json={"text": " ".join(a.text)}))


def cmd_chat(c, a):
    print(f"{DIM}Talking to FRIDAY. Ctrl-D to stop.{OFF}")
    while True:
        try:
            text = input(f"{BOLD}you{OFF} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if text:
            speak(call(c, "POST", "/api/ask", json={"text": text}))


def cmd_heard(c, a):
    r = call(c, "POST", "/api/utterance", json={"text": " ".join(a.text)})
    print(f"{CYAN}FRIDAY{OFF} {r['reply']}" if r["addressed"] else f"{DIM}(not for FRIDAY; remembered){OFF}")


def cmd_status(c, a):
    h = call(c, "GET", "/api/hud")
    print(f"{BOLD}Situation{OFF}  {h['situation']}")
    print(f"{BOLD}Plugins{OFF}    {', '.join(h['plugins']) or 'none'}")
    for err in h["plugin_errors"]:
        print(f"  {YELLOW}failed{OFF} {err}")
    if h["learned"]:
        print(f"{BOLD}Learned{OFF}    " + ", ".join(
            f"{k} {'interrupts' if v >= 0.4 else 'waits'} ({v:.2f})" for k, v in sorted(h["learned"].items())))
    print(f"{BOLD}Skills{OFF}     " + (", ".join(h["skills"]) or "none yet"))
    for r in h["routines"]:
        print(f"{BOLD}Routine{OFF}    #{r['id']} {r['at']} {r['days']}: {r['task']}")
    print(f"{BOLD}Lately{OFF}")
    for n in reversed(h["notifications"][:8]):
        print(f"  {DIM}{n['kind']:<9}{OFF} {n['text']}")


def cmd_memory(c, a):
    q = " ".join(a.query)
    m = call(c, "GET", "/api/memory", params={"q": q})
    for line in (m["results"] if q else m["profile"]) or ["(nothing yet)"]:
        print(f"  {line}")


def cmd_forget(c, a):
    gone = call(c, "POST", "/api/forget", json={"text": " ".join(a.text)})["forgot"]
    print("\n".join(f"  forgot: {g}" for g in gone) or "  nothing matched")


def cmd_watch(c, a):
    import websockets

    url = str(c.base_url).replace("http", "ws", 1).rstrip("/") + "/ws?token=" + os.environ["FRIDAY_TOKEN"]
    show = {"heard": lambda m: f"{DIM}heard{OFF}  {m['text']}" + ("" if m["addressed"] else f" {DIM}(not for me){OFF}"),
            "say": lambda m: f"{CYAN}said{OFF}   {m['text']}",
            "notify": lambda m: f"{YELLOW}{m['kind']:<6}{OFF} {m['text']}"}

    async def run():
        async with websockets.connect(url) as ws:
            print(f"{DIM}Watching FRIDAY. Ctrl-C to stop.{OFF}")
            async for raw in ws:
                m = json.loads(raw)
                if m.get("type") in show:
                    print(show[m["type"]](m), flush=True)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="friday", description="Drive FRIDAY from a terminal.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, arg, about in [
            ("ask", cmd_ask, "text", "one question, one answer"),
            ("chat", cmd_chat, None, "back-and-forth until Ctrl-D"),
            ("heard", cmd_heard, "text", "as if overheard: FRIDAY decides if it was for it"),
            ("watch", cmd_watch, None, "live: what FRIDAY hears, says and holds back"),
            ("status", cmd_status, None, "plugins, situation, what it learned, skills, routines"),
            ("memory", cmd_memory, "query", "what FRIDAY knows about you, or a search"),
            ("forget", cmd_forget, "text", "delete facts containing text")]:
        sp = sub.add_parser(name, help=about)
        if arg:
            sp.add_argument(arg, nargs="+" if arg == "text" else "*")
        sp.set_defaults(fn=fn)
    a = p.parse_args(argv)
    a.fn(client(), a)


if __name__ == "__main__":
    main()
