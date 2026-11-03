"""FRIDAY server. Run: uvicorn friday.app:app --host 0.0.0.0 --port 8080"""
import asyncio
import hmac
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from . import config, plugins, proactive, tools
from .addressee import Addressee
from .brain import Brain
from .llm import Claude
from .memory import SEED_QUESTIONS, Memory
from .skills import Skills


# Kinds that interrupt no matter what: the owner asked for them, or they're about safety.
URGENT = {"reminder", "error", "routine"}
INTERRUPT, FEEDBACK_S = 0.4, 90  # below INTERRUPT a kind waits for the next good moment
VOICE_COMPANY_S = 120  # someone else spoke this recently: you're probably talking with them
ANSWER_S = 120  # a reply this soon after FRIDAY asks a question is the answer
PUSHBACK = re.compile(r"\b(stop|enough|shut up|quiet|not now|later|don'?t care|who cares|not interested|"
                      r"don'?t (tell|need|want)|i know|whatever|no one asked|annoying)\b", re.I)


def create_app(settings=None, llm=None, background=True, http=None):
    s = settings or config.load()
    if not s.token:
        raise RuntimeError("Set FRIDAY_TOKEN: the server holds your life, it doesn't run unauthenticated.")
    state = SimpleNamespace(settings=s, clients=set())
    state.llm = llm or Claude(s.model)
    state.http = http or httpx.AsyncClient(timeout=20, headers={"User-Agent": "friday-assistant"})
    state.memory = Memory(s.db, s.retain_days, s.keep_importance, s.record_others)
    state.memory.add_questions(SEED_QUESTIONS)
    state.skills = Skills(s.skills_dir, extra=sorted(Path(s.plugins_dir).glob("*/skills")))
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

    async def notify(kind, text, urgent=False):
        """Speak now or hold for a better moment: urgent kinds always speak; the rest wait while
        the owner has company, and wait for good if the learning loop says they don't want them live."""
        if urgent or kind in URGENT:
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

    async def ask_question(now=None):
        """Getting to know the owner: one preference question at a quiet moment, at most every 20 hours,
        only while they're around (spoke in the last half hour) and learning says they don't mind."""
        now = now or time.time()
        q = state.memory.next_question()
        # Around (spoke to FRIDAY in the last half hour) but not mid-conversation (nothing for 5 minutes).
        quiet = not busy() and now - state.addressee.last_spoke > 300 and 300 < now - state.last_owner < 1800
        if q and quiet and now - state.memory.last_asked() > 20 * 3600 and state.memory.taste("question") >= INTERRUPT:
            state.memory.mark_asked(q["id"], now)
            state.pending_question = (q["id"], now)
            state.brain.said(q["text"])
            await say("question", q["text"], about=["question"])
            return q["text"]

    state.last_said, state.presence, state.other_voice = None, {}, 0.0  # presence: room -> people the sensors count
    state.pending_question, state.last_owner = None, 0.0
    state.notify, state.ask_question = notify, ask_question
    registry = tools.build(state)
    state.plugins, state.plugin_errors = plugins.load(s.plugins_dir, state, registry)
    state.brain = Brain(state.llm, state.memory, registry, s.owner, skills=state.skills)

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
            verdict = "yes" if await Addressee.ask_model(state.llm, text, context) else "no"
        addressed = verdict == "yes"
        state.memory.add_utterance(text, speaker="owner" if addressed and speaker == "unknown" else speaker,
                                   addressed=addressed)
        await broadcast({"type": "heard", "text": text, "addressed": addressed})
        if not addressed:
            return None
        state.last_owner = time.time()
        learn_from(cleaned)
        if state.pending_question and time.time() - state.pending_question[1] < ANSWER_S:
            state.memory.answer(state.pending_question[0], cleaned)
        state.pending_question = None
        reply = await state.brain.respond(cleaned, situation=situation())
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
                if msg.get("type") == "presence":
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
        state.last_owner = time.time()
        reply = await state.brain.respond(body["text"], situation=situation())
        state.memory.add_utterance(reply, speaker="friday", addressed=True)
        return {"reply": reply}

    @app.get("/api/hud", dependencies=[Depends(auth)])
    async def hud():
        return {"presence": state.presence, "situation": situation(), "plugins": [p.name for p in state.plugins],
                "plugin_errors": state.plugin_errors,
                "learned": state.memory.tastes(), "notifications": state.memory.notifications(),
                "recent": state.memory.recent(20)}

    @app.get("/api/memory", dependencies=[Depends(auth)])
    async def memory(q: str = ""):
        return {"profile": state.memory.profile(), "results": state.memory.search(q) if q else []}

    @app.post("/api/consolidate", dependencies=[Depends(auth)])
    async def consolidate():
        return await state.memory.consolidate(state.llm) or {}

    return app


def __getattr__(name):
    # `uvicorn friday.app:app` builds the app lazily so importing this module (tests) needs no env.
    if name == "app":
        return create_app()
    raise AttributeError(name)
