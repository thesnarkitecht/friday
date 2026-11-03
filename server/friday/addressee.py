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
        return "no", text

    @staticmethod
    async def ask_model(llm, text, context):
        prompt = ("A voice assistant named FRIDAY just finished speaking. Recent conversation:\n"
                  f"{context}\n\nNew utterance: \"{text}\"\n"
                  "Is the new utterance addressed to FRIDAY (not to another person in the room)?")
        try:
            verdict = await llm.decide(prompt, Addressed)
        except Unavailable:
            return False  # can't ask: stay quiet rather than butt in

        return bool(verdict and verdict.addressed)
