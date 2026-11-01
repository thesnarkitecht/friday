"""Background phone calls on the owner's behalf via Twilio. Each turn: Twilio transcribes the
other side (<Gather input="speech">), the model writes FRIDAY's next line, Twilio speaks it."""
import base64
import hashlib
import hmac
import uuid
from xml.sax.saxutils import escape

import httpx

from .llm import parse_json


def twilio_signature(auth_token, url, params):
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def twiml(say, gather_action=None, hangup=False):
    body = f"<Say>{escape(say)}</Say>" if say else ""
    if gather_action:
        body = (f'<Gather input="speech" speechTimeout="auto" action="{escape(gather_action)}" method="POST">'
                f"{body}</Gather><Redirect method=\"POST\">{escape(gather_action)}</Redirect>")
    if hangup:
        body += "<Hangup/>"
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


class Calls:
    def __init__(self, settings, llm, memory, notify, client=None):
        self.s, self.llm, self.memory, self.notify = settings, llm, memory, notify
        self.client = client or httpx.AsyncClient(timeout=20)
        self.calls = {}

    def verify(self, url, params, signature):
        if not self.s.twilio_token:
            return False
        return hmac.compare_digest(twilio_signature(self.s.twilio_token, url, params), signature or "")

    async def place(self, to, goal):
        if not (self.s.twilio_sid and self.s.twilio_token and self.s.twilio_from and self.s.public_url):
            raise RuntimeError("Twilio isn't configured (TWILIO_* and FRIDAY_PUBLIC_URL)")
        cid = uuid.uuid4().hex[:12]
        self.calls[cid] = {"to": to, "goal": goal, "transcript": [], "done": False}
        r = await self.client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{self.s.twilio_sid}/Calls.json",
            auth=(self.s.twilio_sid, self.s.twilio_token),
            data={"To": to, "From": self.s.twilio_from, "Url": self.turn_url(cid)})
        r.raise_for_status()
        return cid

    def turn_url(self, cid):
        return f"{self.s.public_url.rstrip('/')}/twilio/turn?call={cid}"

    async def turn(self, cid, heard):
        call = self.calls.get(cid)
        if not call or call["done"]:
            return twiml("Goodbye.", hangup=True)
        if heard:
            call["transcript"].append(f"Them: {heard}")
        prompt = (
            f"You are FRIDAY, an AI assistant on a phone call on behalf of {self.s.owner}. Goal: {call['goal']}\n"
            "If this is your first line, greet them and say you're an AI assistant calling for "
            f"{self.s.owner}. Be brief and polite; never invent details you weren't given.\n"
            "Transcript so far:\n" + ("\n".join(call["transcript"]) or "(call just connected)") +
            '\nReturn ONLY JSON: {"say": "your next line", "done": true|false, "outcome": "result if done"}')
        data = parse_json(await self.llm.text(prompt, max_tokens=300)) or {"say": "Sorry, could you repeat that?"}
        call["transcript"].append(f"FRIDAY: {data.get('say', '')}")
        if data.get("done") or len(call["transcript"]) > 40:
            call["done"] = True
            await self._finish(call, data.get("outcome") or "call ended")
            return twiml(data.get("say") or "Thank you, goodbye.", hangup=True)
        return twiml(data.get("say", ""), gather_action=self.turn_url(cid))

    async def _finish(self, call, outcome):
        text = f"Call to {call['to']} about '{call['goal']}': {outcome}"
        self.memory.remember(text, "commitment", 0.7, "call")
        await self.notify("call", text)
