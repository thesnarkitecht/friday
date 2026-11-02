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
  that may matter. Use it quietly; never read it out."""

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
    def __init__(self, llm, memory, tools, owner="the owner", model=None):
        self.llm, self.memory, self.tools, self.owner, self.model = llm, memory, tools, owner, model
        self.history = []  # short rolling conversation; long-term lives in memory

        self.system = PERSONA.format(owner=owner)

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
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)[:3000]})
        return "I got tangled up on that one. Try me again?"
