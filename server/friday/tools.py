"""The tools FRIDAY's brain can call. Each is a plain async function plus a JSON schema."""
import base64
import json
from datetime import datetime


class Tools:
    def __init__(self):
        self._fns, self._schemas = {}, []

    def tool(self, description, **params):
        """Register fn as a tool; params map name -> (json type, description). Names ending in '_' are optional."""
        def wrap(fn):
            props = {k.rstrip("_"): {"type": t, "description": d} for k, (t, d) in params.items()}
            required = [k for k in params if not k.endswith("_")]
            self._schemas.append({"type": "function", "function": {
                "name": fn.__name__, "description": description,
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
    """state: the app's shared objects (memory, feeds, calls, llm, settings, location, phone push)."""
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

    @t.tool("Look at a public traffic camera's latest frame and describe it.",
            camera_id=("string", "id from cameras_near"), question_=("string", "what to look for"))
    async def look_at_camera(camera_id, question="Describe traffic, weather and any incident in one sentence."):
        img = await feeds.frame(camera_id)
        if not img:
            return {"error": "no such camera"}
        msg = await state.llm.chat([{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}}]}],
            model=s.vision_model, max_tokens=150)
        return {"camera": camera_id, "sees": msg.get("content")}

    @t.tool("Unread GitHub notifications.")
    async def github():
        return {"notifications": (await feeds.github())[:15]}

    @t.tool("Start a phone call from the owner's iPhone. iOS shows a confirm prompt before dialing.",
            number=("string", "phone number"))
    async def call_from_iphone(number):
        return await state.push_phone({"kind": "url", "url": f"tel:{_digits(number)}"})

    @t.tool("Open a text message on the owner's iPhone, pre-filled; the owner taps send.",
            number=("string", "phone number"), body=("string", "message text"))
    async def text_from_iphone(number, body):
        from urllib.parse import quote
        return await state.push_phone({"kind": "url", "url": f"sms:{_digits(number)}&body={quote(body)}"})

    @t.tool("Run one of the owner's Apple Shortcuts on their iPhone (music, smart home, Focus, alarms, anything they've built).",
            name=("string", "exact shortcut name"), input_=("string", "optional text input"))
    async def run_shortcut(name, input=""):
        from urllib.parse import quote
        url = f"shortcuts://run-shortcut?name={quote(name)}"
        if input:
            url += f"&input=text&text={quote(input)}"
        return await state.push_phone({"kind": "url", "url": url})

    @t.tool("Have FRIDAY phone someone in the background on the owner's behalf (book, ask, wait on hold) and report back. "
            "FRIDAY discloses it's an AI assistant.",
            number=("string", "E.164 phone number"), goal=("string", "what to accomplish on the call"))
    async def call_on_my_behalf(number, goal):
        return {"call_id": await state.calls.place(number, goal), "status": "dialing"}

    return t


def _digits(number):
    return "".join(c for c in number if c.isdigit() or c == "+")
