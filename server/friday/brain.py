"""FRIDAY's reasoning loop: builds context (profile, relevant memories, situation), lets the
model call tools, and returns one short spoken reply.

Prompt layout is built for small local models: the system prompt and tool list never change, so
llama.cpp / vLLM reuse their KV cache for that prefix and only the new turn gets processed.
Everything that changes per turn (clock, place, memories) rides in the latest user message."""
import json
from datetime import datetime

PERSONA = """You are FRIDAY, {owner}'s personal assistant. You speak out loud, so keep replies short,
natural and specific: one to three sentences, no lists, no markdown. Dry wit is fine; filler is not.
You know {owner} well; use what you know. If you don't know something, say so or look it up with a tool.
Rules:
- Before sending a message, placing a call, or anything that spends money or can't be undone, read back
  what you're about to do and get a clear yes, unless {owner} already said exactly what to do in this request.
- On calls you place for {owner}, say up front that you're an AI assistant calling on their behalf.
- Each message starts with a [context] block: the time, where {owner} is, and what you remember
  that may matter. Use it quietly; never read it out.
- Skills are saved how-tos. When a request matches one below, call skill_view first and follow it.
  After working out something new that will come up again, save it with skill_save.
Skills:
{skills}"""

CONTEXT = """[context]
Now: {now}
Where: {where}
Situation: {situation}
Known about {owner}: {profile}
Maybe relevant: {recall}
[/context]
{text}"""


def _lines(items, n, width=160):
    """Context budget: small models choke on long prompts, so cap count and length."""
    return "; ".join(i[:width] for i in items[:n]) or "(nothing)"


class Brain:
    def __init__(self, llm, memory, tools, owner="the owner", model=None, skills=None):
        self.llm, self.memory, self.tools, self.owner, self.model = llm, memory, tools, owner, model
        self.skills = skills
        self.history = []  # short rolling conversation; long-term lives in memory
        self.worklog = []  # tool-using tasks since the last nightly review (in memory: a restart loses them)
        self.refresh()

    def refresh(self):
        """Snapshot the skill index into the system prompt. Only at startup and after the nightly
        review: a skill saved mid-day is usable via skill_view but doesn't break the cached prefix."""
        self.system = PERSONA.format(owner=self.owner, skills=self.skills.index() if self.skills else "(none)")

    def with_context(self, text, where="unknown", situation="nothing notable"):
        return CONTEXT.format(
            owner=self.owner, now=datetime.now().astimezone().strftime("%a %b %d %Y %H:%M %Z"),
            where=where, situation=situation, text=text,
            profile=_lines(self.memory.profile(), 8), recall=_lines(self.memory.search(text), 4))

    async def respond(self, text, where="unknown", situation="nothing notable", max_steps=6):
        # History keeps bare text so earlier turns stay byte-identical (cacheable) on the next call.
        self.history = (self.history + [{"role": "user", "content": text}])[-12:]
        messages = [{"role": "system", "content": self.system}, *self.history[:-1],
                    {"role": "user", "content": self.with_context(text, where, situation)}]
        used = []
        for _ in range(max_steps):
            msg = await self.llm.chat(messages, tools=self.tools.schemas(), model=self.model)
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = (msg.get("content") or "").strip()
                self.history.append({"role": "assistant", "content": reply})
                if used:
                    self.worklog = (self.worklog + [{"ask": text, "tools": used, "reply": reply}])[-50:]
                return reply
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for call in calls:
                used.append(f"{call['function']['name']}({str(call['function'].get('arguments'))[:200]})")
                result = await self.tools.run(call["function"]["name"], call["function"].get("arguments") or "{}")
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)[:3000]})
        return "I got tangled up on that one. Try me again?"
