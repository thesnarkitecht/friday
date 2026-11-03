"""Hand heavy research and coding to a Hermes Agent over its OpenAI-compatible API.

FRIDAY assists; it doesn't automate. Hermes investigates or writes code and reports back, and FRIDAY
tells you when the answer is in. Needs HERMES_URL (e.g. http://hermes:8642/v1) and HERMES_KEY
(Hermes' API_SERVER_KEY)."""
import asyncio

BRIEF = ("You are being consulted by FRIDAY, a personal voice assistant, on behalf of its owner. Research or "
         "write code to answer the task, then report: a two-sentence spoken summary first, then details. "
         "Don't change the owner's systems, send messages, or spend money; this is a consultation.")


def setup(p):
    url, key = p.env("HERMES_URL").rstrip("/"), p.env("HERMES_KEY")
    running = set()  # keep task references so they aren't garbage-collected mid-run

    async def consult(task):
        try:
            r = await p.http.post(f"{url}/chat/completions", timeout=1800,
                                  headers={"Authorization": f"Bearer {key}"},
                                  json={"model": "hermes-agent", "messages": [
                                      {"role": "system", "content": BRIEF}, {"role": "user", "content": task}]})
            r.raise_for_status()
            answer = r.json()["choices"][0]["message"]["content"] or "(empty answer)"
            p.memory.remember(f"Hermes on '{task[:120]}': {answer[:2000]}", "research", 0.6, "hermes")
            await p.notify("hermes", f"Hermes is back on {task[:80]}. {answer.split(chr(10))[0][:300]}", urgent=True)
        except Exception as e:
            await p.notify("hermes", f"Hermes couldn't finish {task[:80]}: {type(e).__name__}.", urgent=True)

    @p.tool("Hand heavy research or coding to Hermes Agent. Returns at once; FRIDAY speaks up when it's done.",
            task=("string", "the full task, with all the context Hermes needs"))
    async def ask_hermes(task):
        if not url:
            return {"error": "Hermes isn't set up (HERMES_URL)"}
        t = asyncio.create_task(consult(task))
        running.add(t)
        t.add_done_callback(running.discard)
        return {"status": "handed to Hermes; I'll say when it's back"}
