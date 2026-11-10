"""Proactive mode: Friday watches things and speaks first.

Events come from [[watch]] entries in the config (a timer, or a command whose output changed)
and from mail other agents send. System 1 decides whether each event is worth a turn at all, so
an idle watcher costs nothing.
"""

import hashlib
import json
import shutil
import subprocess
import time

from .config import HOME

STATE = HOME / "watch.json"


def due_events(watches: list[dict], state: dict, now: float) -> list[str]:
    """Events ready to fire. Updates `state` in place."""
    events = []
    for w in watches:
        name = w["name"]
        seen = state.setdefault(name, {"at": 0, "hash": ""})
        if now - seen["at"] < w.get("every", 300):
            continue
        seen["at"] = now
        if not w.get("cmd"):
            events.append(w["prompt"])
            continue
        r = subprocess.run(w["cmd"], shell=True, capture_output=True, text=True, timeout=120)
        output = r.stdout + r.stderr
        digest = hashlib.sha256(output.encode()).hexdigest()
        if digest != seen["hash"]:
            first_look = not seen["hash"]
            seen["hash"] = digest
            if not first_look:  # the first run only records the baseline
                events.append(f"{w['prompt']}\n\nOutput of `{w['cmd']}`:\n{output[:4000]}")
    return events


def worth_a_turn(agent, event: str) -> bool:
    answer = agent.s1.decide(
        event,
        {
            "act": {
                "type": "noul",
                "instructions": "This event needs the user's attention or an action now; it is not routine noise.",
                "default": True,
            }
        },
    )["act"]
    return bool(answer.value) or answer.confidence < agent.cfg["s1"]["confidence"]


def notify(name: str, text: str) -> None:
    with (HOME / "notifications.md").open("a") as f:
        f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')} {name}\n{text}\n")
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", name, text[:300]], check=False)
    elif shutil.which("osascript"):
        script = f"display notification {json.dumps(text[:300])} with title {json.dumps(name)}"
        subprocess.run(["osascript", "-e", script], check=False)


def watch(agent, tick: float = 5.0, once: bool = False) -> None:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    while True:
        events = due_events(agent.cfg["watch"], state, time.time())
        events += [f"Message from agent {sender}: {body}" for sender, body in agent.bus.inbox(agent.name)]
        STATE.write_text(json.dumps(state))
        for event in events:
            if not worth_a_turn(agent, event):
                agent.out(f"  · skipped (System 1: not worth a turn): {event[:80]}")
                continue
            reply = agent.turn(event)
            agent.out(reply)
            notify(agent.name, reply)
        if once:
            return
        time.sleep(tick)
