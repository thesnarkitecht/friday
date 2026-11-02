"""FRIDAY home server. Run: uvicorn friday.app:app --host 0.0.0.0 --port 8080"""
import asyncio
import hmac
import re
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from . import config, proactive, tools
from .addressee import Addressee
from .brain import Brain
from .calls import Calls
from .feeds import Feeds
from .llm import LLM
from .memory import Memory


# Kinds that interrupt no matter what: the owner asked for them, or they're about safety.
URGENT = {"reminder", "alert", "call", "error"}
INTERRUPT, FEEDBACK_S = 0.4, 90  # below INTERRUPT a kind waits for the next good moment
VOICE_COMPANY_S = 120  # someone else spoke this recently: you're probably talking with them
PUSHBACK = re.compile(r"\b(stop|enough|shut up|quiet|not now|later|don'?t care|who cares|not interested|"
                      r"don'?t (tell|need|want)|i know|whatever|no one asked|annoying)\b", re.I)


def create_app(settings=None, llm=None, feeds=None, background=True):
    s = settings or config.load()
    if not s.token:
        raise RuntimeError("Set FRIDAY_TOKEN: the server holds your life, it doesn't run unauthenticated.")
    state = SimpleNamespace(settings=s, location=None, clients=set())
    state.llm = llm or LLM(s.llm_base, s.llm_key, s.llm_model)
    state.memory = Memory(s.db, s.retain_days, s.keep_importance, s.record_others)
    state.feeds = feeds or Feeds(s.github_token)
    state.addressee = Addressee()

    async def broadcast(msg):
        for ws in list(state.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                state.clients.discard(ws)
        return len(state.clients)

    async def say(kind, text, about=()):
        """about: the learnable kinds this line is made of; the owner's answer tunes them."""
        state.memory.log_notification(kind, text)
        if about:
            state.last_said = (set(about), time.time())
        state.addressee.spoke()  # open the follow-up window so "stop telling me that" is heard
        await broadcast({"type": "notify", "kind": kind, "text": text})

    async def notify(kind, text):
        """Speak now or hold for a better moment: urgent kinds always speak; the rest wait while
        the owner has company, and wait for good if the learning loop says they don't want them live."""
        if kind in URGENT:
            await say(kind, text)
        elif busy() or state.memory.taste(kind) < INTERRUPT:
            state.memory.hold(kind, text)
        else:
            await say(kind, text, about=[kind])

    async def brief():
        """Deliver what was held, as one short line, at a good moment."""
        held = state.memory.release()
        if held:
            await say("briefing", "Catching you up: " + " ".join(t.rstrip(".") + "." for _, t in held),
                      about=[k for k, _ in held])

    def busy():
        """With company: room sensors count others, or another voice was heard lately (works anywhere)."""
        return sum(state.presence.values()) > 1 or time.time() - state.other_voice < VOICE_COMPANY_S

    def learn_from(text):
        """The learning loop: how the owner answers an interruption tunes the next one."""
        if not state.last_said or time.time() - state.last_said[1] > FEEDBACK_S:
            return
        kinds, state.last_said = state.last_said[0], None
        for kind in kinds:
            state.memory.learn(kind, liked=not PUSHBACK.search(text))

    def situation():
        p = state.presence
        if not p:
            return "nothing notable"
        rooms = [r for r, n in p.items() if n]
        if not rooms:
            return "away from home"
        company = sum(p.values()) - 1
        return f"at home ({', '.join(rooms)})" + (f" with {company} other {'person' if company == 1 else 'people'}" if company else ", alone")

    state.last_said, state.presence, state.other_voice = None, {}, 0.0  # presence: room -> people the sensors count
    state.notify = notify
    state.calls = Calls(s, state.llm, state.memory, notify)
    state.brain = Brain(state.llm, state.memory, tools.build(state), s.owner)

    async def hear(text, speaker="unknown", others_present=None):
        """Every utterance is remembered; only ones meant for FRIDAY get a reply.
        Every connected client sees what was heard and what FRIDAY said."""
        if speaker not in ("owner", "unknown"):  # the client's voice ID says it's someone else
            state.other_voice = time.time()
        if others_present is None:
            others_present = busy()
        verdict, cleaned = state.addressee.check(text, others_present)
        if speaker not in ("owner", "unknown"):
            verdict = "no"  # only the owner commands FRIDAY, even if a guest says "Friday"
        if verdict == "maybe":
            context = "\n".join(f"{u['speaker']}: {u['text']}" for u in state.memory.recent(6))
            verdict = "yes" if await Addressee.ask_model(state.llm, text, context, s.fast_model) else "no"
        addressed = verdict == "yes"
        state.memory.add_utterance(text, speaker="owner" if addressed and speaker == "unknown" else speaker,
                                   addressed=addressed)
        await broadcast({"type": "heard", "text": text, "addressed": addressed})
        if not addressed:
            return None
        learn_from(cleaned)
        loc = state.location
        reply = await state.brain.respond(cleaned, where=f"{loc['lat']:.4f},{loc['lon']:.4f}" if loc else "unknown",
                                          situation=situation())
        state.memory.add_utterance(reply, speaker="friday", addressed=True)
        state.addressee.spoke()
        await broadcast({"type": "say", "text": reply})
        await broadcast({"type": "memory", "facts": state.memory.profile()})
        if not busy():
            await brief()  # the owner is engaged anyway: a good moment for what was held
        return reply

    def auth(request: Request):
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(given, s.token):
            raise HTTPException(401)

    @asynccontextmanager
    async def lifespan(_):
        task = asyncio.create_task(proactive.run(state)) if background else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="FRIDAY", lifespan=lifespan)
    app.state.friday = state

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        if not hmac.compare_digest(socket.query_params.get("token", ""), s.token):
            await socket.close(code=4401)
            return
        await socket.accept()
        state.clients.add(socket)
        await socket.send_json({"type": "memory", "facts": state.memory.profile()})
        try:
            while True:
                msg = await socket.receive_json()
                if msg.get("type") == "location":
                    state.location = {"lat": float(msg["lat"]), "lon": float(msg["lon"]), "ts": time.time()}
                elif msg.get("type") == "presence":
                    try:
                        await presence_update(msg)
                    except (KeyError, TypeError, ValueError):
                        pass  # a garbled sensor message shouldn't drop the connection
                elif msg.get("type") == "utterance" and msg.get("text", "").strip():
                    await hear(msg["text"], msg.get("speaker", "unknown"), msg.get("others_present"))
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.discard(socket)

    @app.post("/api/utterance", dependencies=[Depends(auth)])
    async def utterance(body: dict):
        reply = await hear(body["text"], body.get("speaker", "unknown"), body.get("others_present"))
        return {"addressed": reply is not None, "reply": reply}

    async def presence_update(body):
        """A room sensor reports how many people it sees. The moment company leaves, FRIDAY
        delivers what it held back."""
        was_busy = busy()
        state.presence[str(body["room"])[:40]] = max(0, min(int(body["people"]), 20))
        if was_busy and not busy():
            await brief()
        return {"situation": situation()}

    @app.post("/api/presence", dependencies=[Depends(auth)])
    async def presence(body: dict):
        try:
            return await presence_update(body)
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, 'Send {"room": "office", "people": 1}')

    @app.post("/api/ask", dependencies=[Depends(auth)])
    async def ask(body: dict):
        """Push-to-talk / scripts entry point: always addressed, always the owner."""
        state.memory.add_utterance(body["text"], speaker="owner", addressed=True)
        reply = await state.brain.respond(body["text"], situation=situation())
        state.memory.add_utterance(reply, speaker="friday", addressed=True)
        return {"reply": reply}

    @app.post("/api/location", dependencies=[Depends(auth)])
    async def location(body: dict):
        state.location = {"lat": float(body["lat"]), "lon": float(body["lon"]), "ts": time.time()}
        return {"ok": True}

    @app.get("/api/hud", dependencies=[Depends(auth)])
    async def hud():
        return {"location": state.location, "presence": state.presence, "situation": situation(),
                "learned": state.memory.tastes(), "notifications": state.memory.notifications(),
                "recent": state.memory.recent(20)}

    @app.get("/api/memory", dependencies=[Depends(auth)])
    async def memory(q: str = ""):
        return {"profile": state.memory.profile(), "results": state.memory.search(q) if q else []}

    @app.post("/api/consolidate", dependencies=[Depends(auth)])
    async def consolidate():
        return await state.memory.consolidate(state.llm, model=s.fast_model) or {}

    @app.post("/twilio/turn")
    async def twilio_turn(request: Request, call: str):
        params = dict(parse_qsl((await request.body()).decode()))
        if not state.calls.verify(state.calls.turn_url(call), params, request.headers.get("x-twilio-signature")):
            raise HTTPException(403)
        xml = await state.calls.turn(call, params.get("SpeechResult", ""))
        return Response(xml, media_type="application/xml")

    return app


def __getattr__(name):
    # `uvicorn friday.app:app` builds the app lazily so importing this module (tests) needs no env.
    if name == "app":
        return create_app()
    raise AttributeError(name)
