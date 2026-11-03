"""The part that makes FRIDAY not just an agent: it speaks up on its own."""
import asyncio
import time
from datetime import datetime

from . import plugins


async def tick(state, now=None):
    """One pass: due reminders, owner-scheduled routines, plugin feeds, maybe a question. Returns what was said."""
    now = now or time.time()
    said = [("reminder", f"Reminder: {text}") for text in state.memory.due_reminders(now)]
    for task in state.memory.due_routines(now):  # owner-scheduled: FRIDAY does the task, then says it
        try:
            said.append(("routine", await state.brain.respond(f"(Scheduled routine, nobody asked just now.) {task}")))
        except Exception as e:
            said.append(("error", f"Routine '{task}' failed: {type(e).__name__}"))
    for kind, text in said:
        await state.notify(kind, text)
    said += await plugins.poll(state.plugins, state.memory, state.notify, now)  # zero-token checks
    if 10 <= datetime.fromtimestamp(now).hour < 21:
        q = await state.ask_question(now)
        if q:
            said.append(("question", q))
    return said


async def run(state, every_s=60):
    last_sleep_day = None
    while True:
        try:
            await tick(state)
        except Exception as e:  # one bad tick must not stop FRIDAY speaking up for good
            await state.notify("error", f"Proactive check failed: {type(e).__name__}")
        today = datetime.now()
        if today.hour == 3 and last_sleep_day != today.date():  # the nightly "sleep" pass
            last_sleep_day = today.date()
            try:
                await state.memory.consolidate(state.llm)  # keep what matters, drop the rest
                # The skill half of the learning loop: turn today's tool use into reusable how-tos.
                worklog, state.brain.worklog = state.brain.worklog, []
                await state.skills.review(state.llm, worklog)
                await state.memory.think_of_questions(state.llm)
                state.brain.refresh()  # the one daily moment the cached prompt prefix changes
            except Exception as e:
                await state.notify("error", f"Nightly memory/skill pass failed: {type(e).__name__}")
        await asyncio.sleep(every_s)
