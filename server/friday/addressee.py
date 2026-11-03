"""Decides whether speech is meant for FRIDAY. Cheap rules first; the model only breaks ties."""
import re
import time

from pydantic import BaseModel

from .llm import Unavailable

_WAKE = re.compile(r"\b(?:hey|ok|okay|yo)?[\s,]*friday\b[\s,.!?]*", re.I)
# "see you friday", "this friday night" mean the weekday, not the assistant.
_WEEKDAY = re.compile(
    r"\b(on|this|next|last|by|until|till|every|since|before|after|from)\s+friday\b"
    r"|\bfriday(?:'s|s)?\s+(night|morning|afternoon|evening|at|is|was)\b", re.I)


# Sounds like a request to an assistant. Alone, that's probably for FRIDAY even without its name;
# Claude makes the call (typed) with the recent conversation, so musing out loud stays private.
_REQUEST = re.compile(
    r"^\W*(remind me|remember (that|to)|what('?s| is| are| was| were)|when('?s| is| does| did)|where('?s| is)|"
    r"who('?s| is)|how (do|does|much|many|long|far|is)|tell me|set (a|an|up)|add |can you|could you|would you|"
    r"please|look up|search|find|check|any (news|updates)|did (a|any)|is there|play |turn (on|off)|"
    r"what do you think)\b", re.I)


class Addressed(BaseModel):
    addressed: bool


class Addressee:
    def __init__(self, followup_s=10):
        self.followup_s = followup_s
        self.last_spoke = 0.0

    def spoke(self, now=None):
        self.last_spoke = now or time.time()

    def check(self, text, others_present=False, now=None):
        """Returns (verdict, cleaned_text) with verdict 'yes', 'no' or 'maybe'."""
        now = now or time.time()
        if _WAKE.search(text) and not _WEEKDAY.search(text):
            cleaned = _WAKE.sub(" ", text, count=1).strip(" ,.")
            return "yes", cleaned or text
        if now - self.last_spoke < self.followup_s:
            # Mid-conversation: alone, it's for us; with company, ask the model.
            return ("maybe" if others_present else "yes"), text
        if not others_present and _REQUEST.search(text):
            return "maybe", text
        return "no", text

    @staticmethod
    async def ask_model(llm, text, context):
        prompt = ("A voice assistant named FRIDAY listens all day through its owner's headphones. Recent speech:\n"
                  f"{context}\n\nNew utterance: \"{text}\"\n"
                  "Is the new utterance addressed to FRIDAY, rather than to another person, a phone call, or "
                  "the owner thinking out loud?")
        try:
            verdict = await llm.decide(prompt, Addressed)
        except Unavailable:
            return False  # can't ask: stay quiet rather than butt in

        return bool(verdict and verdict.addressed)
