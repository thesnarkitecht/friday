"""The tools FRIDAY's brain can call. Each is a plain async function plus a JSON schema."""
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
            self._schemas.append({"name": fn.__name__, "description": doc,
                                  "input_schema": {"type": "object", "properties": props, "required": required}})
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
    """The core tools. Plugins add theirs to the same registry (see plugins.py)."""
    t, mem = Tools(), state.memory

    @t.tool("Save a durable fact about the owner: an interest, a preference, how they like things done.",
            text=("string", "the fact, self-contained"), category_=("string", "interest|preference|work|place|other"))
    async def remember(text, category="other"):
        mem.remember(text, category)
        return {"saved": text}

    @t.tool("Forget saved facts that match (the owner says something is wrong or private).",
            text=("string", "words from the fact to forget"))
    async def forget(text):
        return {"forgot": mem.forget(text)}

    @t.tool("How things in the owner's world connect: projects, tools, topics, places, orgs, people.",
            entity_=("string", "what to look around; empty for the strongest links overall"))
    async def graph(entity=""):
        return mem.graph(entity or None, depth=2 if entity else 1, limit=40)

    @t.tool("Add a connection to the owner's map, e.g. ('me', 'works on', 'FRIDAY') or ('FRIDAY', 'uses', 'Rust').",
            subject=("string", "use 'me' for the owner"), relation=("string", "short verb phrase"),
            object=("string", "the other thing"))
    async def connect(subject, relation, object):
        mem.link(subject, relation, object)
        return {"linked": [subject, relation, object]}

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

    return t

