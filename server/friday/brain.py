"""FRIDAY's reasoning loop: builds context (profile, relevant memories, situation), lets the
model call tools, and returns one short spoken reply."""
import json
from datetime import datetime

PERSONA = """You are FRIDAY, {owner}'s personal assistant. You speak out loud, so keep replies short,
natural and specific: one to three sentences, no lists, no markdown. Dry wit is fine; filler is not.
You know {owner} well; use what you know. If you don't know something, say so or look it up with a tool.
Rules:
- Before sending a message, placing a call, or anything that spends money or can't be undone, read back
  what you're about to do and get a clear yes, unless {owner} already said exactly what to do in this request.
- On calls you place for {owner}, say up front that you're an AI assistant calling on their behalf.
- Phone actions happen on {owner}'s iPhone through Shortcuts and system prompts they confirm.

Now: {now}
Where {owner} is: {where}
Situation: {situation}
What you know about {owner}:
{profile}
Possibly relevant memories:
{recall}"""


class Brain:
    def __init__(self, llm, memory, tools, owner="the owner", model=None):
        self.llm, self.memory, self.tools, self.owner, self.model = llm, memory, tools, owner, model
        self.history = []  # short rolling conversation; long-term lives in memory

    def system_prompt(self, query, where="unknown", situation="nothing notable"):
        return PERSONA.format(
            owner=self.owner, now=datetime.now().astimezone().strftime("%A %B %d %Y, %H:%M %Z"),
            where=where, situation=situation,
            profile="\n".join(self.memory.profile()) or "(nothing yet)",
            recall="\n".join(self.memory.search(query)) or "(nothing)")

    async def respond(self, text, where="unknown", situation="nothing notable", max_steps=6):
        self.history = (self.history + [{"role": "user", "content": text}])[-20:]
        messages = [{"role": "system", "content": self.system_prompt(text, where, situation)}, *self.history]
        for _ in range(max_steps):
            msg = await self.llm.chat(messages, tools=self.tools.schemas(), model=self.model)
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = (msg.get("content") or "").strip()
                self.history.append({"role": "assistant", "content": reply})
                return reply
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for call in calls:
                result = await self.tools.run(call["function"]["name"], call["function"].get("arguments") or "{}")
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)[:8000]})
        return "I got tangled up on that one. Try me again?"
