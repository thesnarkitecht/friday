"""The tools FRIDAY's brain can call. Each is a plain async function plus a JSON schema."""
import base64
import json
from datetime import datetime


class Tools:
    def __init__(self):
        self._fns, self._schemas = {}, []

    def tool(self, doc, **params):
        """Register fn as a tool; params map name -> (json type, description). Names ending in '_' are optional."""
        def wrap(fn):
            props = {k.rstrip("_"): {"type": t, "description": d} for k, (t, d) in params.items()}
            required = [k for k in params if not k.endswith("_")]
            self._schemas.append({"type": "function", "function": {
                "name": fn.__name__, "description": doc,
                "parameters": {"type": "object", "properties": props, "required": required}}})
            self._fns[fn.__name__] = fn
            return fn
        return wrap

    def schemas(self):
        return self._schemas

    async def run(self, name, arguments):
        fn = self._fns.get(name)
        if not fn:
            return {"error": f"unknown tool {name}"}
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            return await fn(**args)
        except Exception as e:  # a bad tool call should become a message to the model, not a crash
            return {"error": f"{type(e).__name__}: {e}"}


def build(state):
    """state: the app's shared objects (memory, feeds, calls, llm, settings, location)."""
    t, mem, feeds, s = Tools(), state.memory, state.feeds, state.settings

    def here():
        loc = state.location or {}
        return loc.get("lat", s.home_lat), loc.get("lon", s.home_lon)

    @t.tool("Save a durable fact about the owner or people in their life.",
            text=("string", "the fact, self-contained"), category_=("string", "person|preference|health|work|place|other"))
    async def remember(text, category="other"):
        mem.remember(text, category)
        return {"saved": text}

    @t.tool("Search everything FRIDAY has heard, been told, or summarized.", query=("string", "what to look for"))
    async def recall(query):
        return {"results": mem.search(query, n=10)}

    @t.tool("Set a reminder FRIDAY will speak at the given time.",
            text=("string", "what to remind"), due=("string", "ISO-8601 local datetime"))
    async def set_reminder(text, due):
        when = datetime.fromisoformat(due)
        if when.tzinfo is None:
            when = when.astimezone()
        mem.add_reminder(text, when.timestamp())
        return {"reminder": text, "at": when.isoformat()}

    @t.tool("Schedule something FRIDAY does on its own at a time each day, e.g. a morning briefing.",
            at=("string", "HH:MM, 24-hour local time"), task=("string", "what to do, as you'd ask FRIDAY"),
            days_=("string", "daily | weekdays | weekends | mon,wed,fri (default daily)"))
    async def add_routine(at, task, days="daily"):
        mem.add_routine(at, days, task)
        return {"routines": mem.routines()}

    @t.tool("Cancel a routine by id.", id=("integer", "routine id from add_routine"))
    async def remove_routine(id):
        mem.remove_routine(int(id))
        return {"routines": mem.routines()}

    @t.tool("Load a saved skill's full how-to before doing a task it covers.", name=("string", "skill name"))
    async def skill_view(name):
        body = state.skills.view(name)
        return {"skill": name, "steps": body} if body else {"error": f"no skill {name}", "skills": list(state.skills.all())}

    @t.tool("Save or improve a reusable how-to for a task that will come up again.",
            name=("string", "kebab-case name"), description=("string", "one line: when to use it"),
            body=("string", "short numbered steps naming the tools to call, plus pitfalls"))
    async def skill_save(name, description, body):
        return {"saved": state.skills.save(name, description, body)}

    @t.tool("Weather alerts and earthquakes near the owner right now.")
    async def situation():
        lat, lon = here()
        out = {"lat": lat, "lon": lon}
        for key, fn in (("alerts", feeds.alerts), ("quakes", feeds.quakes)):
            try:
                out[key] = await fn(lat, lon)
            except Exception as e:
                out[key] = f"unavailable ({type(e).__name__})"
        return out

    @t.tool("List public traffic cameras near a point (defaults to the owner's location). Covers NYC, London, California.",
            lat_=("number", "latitude"), lon_=("number", "longitude"))
    async def cameras_near(lat=None, lon=None):
        if lat is None or lon is None:
            lat, lon = here()
        cams = await feeds.cameras_near(lat, lon)
        return {"cameras": [{k: c[k] for k in ("id", "name", "provider", "km")} for c in cams]}

    async def describe(img, question):
        msg = await state.llm.chat([{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}}]}],
            model=s.vision_model, max_tokens=200)
        return msg.get("content")

    @t.tool("Look at a public traffic camera's latest frame and describe it.",
            camera_id=("string", "id from cameras_near"), question_=("string", "what to look for"))
    async def look_at_camera(camera_id, question="Describe traffic, weather and any incident in one sentence."):
        img = await feeds.frame(camera_id)
        if not img:
            return {"error": "no such camera"}
        return {"camera": camera_id, "sees": await describe(img, question)}

    @t.tool("Unread GitHub notifications.")
    async def github():
        return {"notifications": (await feeds.github())[:15]}

    @t.tool("Have FRIDAY phone someone in the background on the owner's behalf (book, ask, wait on hold) and report back. "
            "FRIDAY discloses it's an AI assistant.",
            number=("string", "E.164 phone number"), goal=("string", "what to accomplish on the call"))
    async def call_on_my_behalf(number, goal):
        return {"call_id": await state.calls.place(number, goal), "status": "dialing"}

    return t

