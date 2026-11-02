"""The part that makes FRIDAY not just an agent: it speaks up on its own."""
import asyncio
import time
from datetime import datetime

SEVERE = {"Extreme", "Severe"}


async def tick(state, now=None):
    """One pass: due reminders, new severe alerts, nearby quakes, new GitHub activity. Returns what was said."""
    now = now or time.time()
    said = []
    for text in state.memory.due_reminders(now):
        said.append(("reminder", f"Reminder: {text}"))
    lat = (state.location or {}).get("lat", state.settings.home_lat)
    lon = (state.location or {}).get("lon", state.settings.home_lon)
    if lat or lon:
        try:
            for a in await state.feeds.alerts(lat, lon):
                if a["severity"] in SEVERE and state.memory.first_time(f"nws:{a['id']}"):
                    said.append(("alert", a["headline"] or a["event"]))
            for q in await state.feeds.quakes(lat, lon, radius_km=200):
                if (q["mag"] or 0) >= 4 and state.memory.first_time(f"usgs:{q['id']}"):
                    said.append(("alert", f"Magnitude {q['mag']} earthquake {q['km']} km away, {q['place']}."))
        except Exception:
            pass  # feeds flake; try again next tick
    try:
        for n in await state.feeds.github():
            if n["reason"] in ("review_requested", "mention", "assign") and state.memory.first_time(f"gh:{n['id']}"):
                said.append(("github", f"GitHub: {n['reason'].replace('_', ' ')} on {n['repo']}: {n['title']}"))
    except Exception:
        pass
    for task in state.memory.due_routines(now):  # owner-scheduled: FRIDAY does the task, then says it
        try:
            said.append(("routine", await state.brain.respond(f"(Scheduled routine, nobody asked just now.) {task}")))
        except Exception as e:
            said.append(("error", f"Routine '{task}' failed: {type(e).__name__}"))
    for kind, text in said:
        await state.notify(kind, text)
    return said


async def run(state, every_s=120):
    last_sleep_day = None
    while True:
        await tick(state)
        today = datetime.now()
        if today.hour == 3 and last_sleep_day != today.date():  # the nightly "sleep" pass
            last_sleep_day = today.date()
            try:
                await state.memory.consolidate(state.llm, model=state.settings.fast_model)
                # The skill half of the learning loop: turn today's tool use into reusable how-tos.
                worklog, state.brain.worklog = state.brain.worklog, []
                await state.skills.review(state.llm, worklog, model=state.settings.fast_model)
                state.brain.refresh()  # the one daily moment the cached prompt prefix changes
            except Exception as e:
                await state.notify("error", f"Nightly memory/skill pass failed: {e}")
        await asyncio.sleep(every_s)
