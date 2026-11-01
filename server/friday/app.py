"""FRIDAY home server. Run: uvicorn friday.app:app --host 0.0.0.0 --port 8080"""
import asyncio
import hmac
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

    async def notify(kind, text):
        state.memory.log_notification(kind, text)
        await broadcast({"type": "notify", "kind": kind, "text": text})

    async def push_phone(action):
        sent = await broadcast({"type": "action", **action})
        return {"sent": bool(sent), **({} if sent else {"error": "FRIDAY app isn't connected"})}

    state.notify, state.push_phone = notify, push_phone
    state.calls = Calls(s, state.llm, state.memory, notify)
    state.brain = Brain(state.llm, state.memory, tools.build(state), s.owner)

    async def hear(text, speaker="unknown", others_present=False):
        """Every utterance is remembered; only ones meant for FRIDAY get a reply."""
        verdict, cleaned = state.addressee.check(text, others_present)
        if verdict == "maybe":
            context = "\n".join(f"{u['speaker']}: {u['text']}" for u in state.memory.recent(6))
            verdict = "yes" if await Addressee.ask_model(state.llm, text, context, s.fast_model) else "no"
        addressed = verdict == "yes"
        state.memory.add_utterance(text, speaker="owner" if addressed and speaker == "unknown" else speaker,
                                   addressed=addressed)
        if not addressed:
            return None
        loc = state.location
        reply = await state.brain.respond(cleaned, where=f"{loc['lat']:.4f},{loc['lon']:.4f}" if loc else "unknown")
        state.memory.add_utterance(reply, speaker="friday", addressed=True)
        state.addressee.spoke()
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
        try:
            while True:
                msg = await socket.receive_json()
                if msg.get("type") == "location":
                    state.location = {"lat": float(msg["lat"]), "lon": float(msg["lon"]), "ts": time.time()}
                elif msg.get("type") == "utterance" and msg.get("text", "").strip():
                    reply = await hear(msg["text"], msg.get("speaker", "unknown"), bool(msg.get("others_present")))
                    if reply:
                        await socket.send_json({"type": "say", "text": reply})
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.discard(socket)

    @app.post("/api/utterance", dependencies=[Depends(auth)])
    async def utterance(body: dict):
        reply = await hear(body["text"], body.get("speaker", "unknown"), bool(body.get("others_present")))
        return {"addressed": reply is not None, "reply": reply}

    @app.post("/api/ask", dependencies=[Depends(auth)])
    async def ask(body: dict):
        """Siri / Action button / Shortcuts entry point: always addressed, always the owner."""
        state.memory.add_utterance(body["text"], speaker="owner", addressed=True)
        reply = await state.brain.respond(body["text"])
        state.memory.add_utterance(reply, speaker="friday", addressed=True)
        return {"reply": reply}

    @app.post("/api/location", dependencies=[Depends(auth)])
    async def location(body: dict):
        state.location = {"lat": float(body["lat"]), "lon": float(body["lon"]), "ts": time.time()}
        return {"ok": True}

    @app.get("/api/hud", dependencies=[Depends(auth)])
    async def hud():
        return {"location": state.location, "notifications": state.memory.notifications(),
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
