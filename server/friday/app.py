"""FRIDAY server. Run: uvicorn friday.app:app --host 0.0.0.0 --port 8080"""
import asyncio
import base64
import hmac
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from . import config, plugins, proactive, tools
from .addressee import Addressee
from .brain import Brain
from .llm import Claude
from .memory import SEED_QUESTIONS, Memory
from .skills import Skills
from .voice import Voice


# Kinds that interrupt no matter what: the owner asked for them, or they're about safety.
URGENT = {"reminder", "error", "routine"}
INTERRUPT, FEEDBACK_S = 0.4, 90  # below INTERRUPT a kind waits for the next good moment
VOICE_COMPANY_S = 120  # someone else spoke this recently: you're probably talking with them
ANSWER_S = 120  # a reply this soon after FRIDAY asks a question is the answer
SENTENCE_END = re.compile(r"[.!?](\s+)(?=\S)")  # a sentence is done once the next one has started
PUSHBACK = re.compile(r"\b(stop|enough|shut up|quiet|not now|later|don'?t care|who cares|not interested|"
                      r"don'?t (tell|need|want)|i know|whatever|no one asked|annoying)\b", re.I)


def create_app(settings=None, llm=None, background=True, http=None, voice=None):
    s = settings or config.load()
    if not s.token:
        raise RuntimeError("Set FRIDAY_TOKEN: the server holds your life, it doesn't run unauthenticated.")
    state = SimpleNamespace(settings=s, clients=set(), voice_clients=set())
    state.voice = voice or Voice(piper_path=s.voice_path)
    state.turn_lock, state.tasks, state.hush = asyncio.Lock(), set(), False
    state.llm = llm or Claude(s.model)
    state.http = http or httpx.AsyncClient(timeout=20, headers={"User-Agent": "friday-assistant"})
    Path(s.db).parent.mkdir(parents=True, exist_ok=True)
    state.memory = Memory(s.db, s.retain_days, s.keep_importance, s.record_others)
    state.memory.add_questions(SEED_QUESTIONS)
    state.skills = Skills(s.skills_dir, extra=[d for r in s.plugin_roots for d in sorted(Path(r).glob("*/skills"))])
    state.addressee = Addressee()

    async def broadcast(msg):
        """Send to every connected client. Devices in voice mode also get FRIDAY's words as speech."""
        audio = None
        if msg.get("type") in ("say", "say_part", "notify") and not msg.get("spoken") and state.voice_clients:
            try:
                audio = base64.b64encode(await asyncio.to_thread(state.voice.speak, msg["text"])).decode()
            except Exception:
                pass  # the text still arrives; a voice glitch shouldn't silence FRIDAY entirely
        for ws in list(state.clients):
            try:
                await ws.send_json({**msg, "audio": audio} if audio and ws in state.voice_clients else msg)
            except Exception:
                state.clients.discard(ws)
        return len(state.clients)

    async def say(kind, text, about=()):
        """about: the learnable kinds this line is made of; the owner's answer tunes them."""
        state.memory.log_notification(kind, text)
        state.brain.said(text)  # so "it went great" answers the check-in FRIDAY just asked
        if about:
            state.last_said = (set(about), time.time())
        state.addressee.spoke()  # open the follow-up window so "stop telling me that" is heard
        await broadcast({"type": "notify", "kind": kind, "text": text})

    async def notify(kind, text, urgent=False):
        """Speak now or hold for a better moment: urgent kinds always speak; the rest wait while
        the owner has company, and wait for good if the learning loop says they don't want them live."""
        if urgent or kind in URGENT:
            await say(kind, text)
        elif busy() or quiet_hours() or state.memory.taste(kind) < INTERRUPT:
            state.memory.hold(kind, text)
        else:
            await say(kind, text, about=[kind])

    async def brief():
        """Deliver what was held, as one short line, at a good moment."""
        held = state.memory.release()
        if held:
            text = "Catching you up: " + " ".join(t.rstrip(".") + "." for _, t in held)
            await say("briefing", text, about=[k for k, _ in held])
            return text

    def quiet_hours(now=None):
        """Non-urgent things wait overnight; they're delivered the next time you talk to FRIDAY."""
        start, end = s.quiet_hours
        h = datetime.fromtimestamp(now or time.time()).hour
        return start != end and (start <= h < end if start < end else h >= start or h < end)

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
            await say("question", q["text"], about=["question"])
            return q["text"]

    state.last_said, state.presence, state.other_voice = None, {}, 0.0  # presence: room -> people the sensors count
    state.pending_question, state.last_owner = None, 0.0
    state.notify, state.ask_question, state.quiet_hours = notify, ask_question, quiet_hours
    registry = tools.build(state)
    state.plugins, state.plugin_errors = plugins.load(s.plugin_roots, state, registry)
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
        return (await converse(cleaned))[0]

    async def converse(cleaned):
        """The owner said something to FRIDAY (by voice, CLI or an OpenAI client). Returns the reply and
        any catch-up line delivered with it."""
        state.last_owner = time.time()
        learn_from(cleaned)
        if state.pending_question and time.time() - state.pending_question[1] < ANSWER_S:
            state.memory.answer(state.pending_question[0], cleaned)
        state.pending_question = None
        reply = await respond_aloud(cleaned)
        await broadcast({"type": "memory", "facts": state.memory.profile()})
        catch_up = None if busy() else await brief()  # the owner is engaged anyway: a good moment
        return reply, catch_up

    async def respond_aloud(prompt):
        """Ask the brain and speak the answer sentence by sentence as it's written."""
        state.hush, pending, spoken = False, [""], [0]

        async def on_text(chunk):
            # Speak each sentence the moment it's complete: FRIDAY starts talking while Claude still writes.
            pending[0] += chunk
            while (m := SENTENCE_END.search(pending[0])):
                await say_part(pending[0][:m.end()].strip())
                pending[0] = pending[0][m.end():]

        async def say_part(text):
            if text and not state.hush:
                spoken[0] += 1
                await broadcast({"type": "say_part", "text": text})

        reply = await state.brain.respond(prompt, situation=situation(), on_text=on_text)
        await say_part(pending[0].strip())
        state.memory.add_utterance(reply, speaker="friday", addressed=True)
        state.addressee.spoke()
        await broadcast({"type": "say", "text": reply, "spoken": spoken[0] > 0})
        return reply

    async def greet():
        """Headphones on after a while away: FRIDAY says hello, then delivers anything it held."""
        away = time.time() - state.last_owner
        state.last_owner = time.time()
        when = "for the first time" if away > 30 * 86400 else f"after about {round(away / 3600)} hours away"
        await respond_aloud(f"(Note from your senses, not said by {s.owner}: they just put their headphones on "
                            f"{when}. Greet them in one short line, like a friend who's glad they're back; "
                            "make it personal if you know something that fits. Don't list errands: anything "
                            "waiting is delivered right after you.)")
        await brief()

    def auth(request: Request):
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(given, s.token):
            raise HTTPException(401)

    @asynccontextmanager
    async def lifespan(_):
        task = asyncio.create_task(proactive.run(state)) if background else None
        if background:
            warm = asyncio.create_task(asyncio.to_thread(state.voice.warm))
            state.tasks.add(warm)
            warm.add_done_callback(state.tasks.discard)
        yield
        if task:
            task.cancel()

    app = FastAPI(title="FRIDAY", lifespan=lifespan)
    app.state.friday = state

    async def typed(text):
        state.memory.add_utterance(text, speaker="owner", addressed=True)
        await broadcast({"type": "heard", "text": text, "addressed": True})
        await converse(text)

    async def spoken_in(pcm, speaker):
        text = await asyncio.to_thread(state.voice.transcribe, pcm)
        if text:
            await hear(text, speaker)

    def turn(coro):
        """Run a turn in the background, one at a time, so the socket stays free for "interrupt"."""
        async def run():
            async with state.turn_lock:
                await coro
        t = asyncio.create_task(run())
        state.tasks.add(t)
        t.add_done_callback(state.tasks.discard)

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
                elif msg.get("type") == "interrupt":  # you talked over FRIDAY: stop speaking now
                    state.hush = True
                elif msg.get("type") == "utterance" and msg.get("text", "").strip():
                    turn(hear(msg["text"], msg.get("speaker", "unknown"), msg.get("others_present")))
                elif msg.get("type") == "ask" and str(msg.get("text", "")).strip():  # typed: always for FRIDAY
                    turn(typed(str(msg["text"]).strip()))
                elif msg.get("type") == "hello" and msg.get("voice"):
                    ok = await asyncio.to_thread(state.voice.available)
                    if ok:
                        state.voice_clients.add(socket)
                        if s.greet_after_h and time.time() - state.last_owner > s.greet_after_h * 3600 \
                                and not quiet_hours():
                            turn(greet())
                    await socket.send_json({"type": "ready", "voice": ok, **({} if ok else {
                        "why": "this server has no speech models (run the Docker image)"})})
                elif msg.get("type") == "audio" and socket in state.voice_clients:
                    try:
                        pcm = base64.b64decode(msg["pcm"], validate=True)
                    except (KeyError, TypeError, ValueError):
                        continue  # garbled audio shouldn't drop the connection
                    turn(spoken_in(pcm, msg.get("speaker", "unknown")))
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.discard(socket)
            state.voice_clients.discard(socket)

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
        """CLI / push-to-talk / scripts: always addressed, always the owner."""
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(400, 'Send {"text": "..."}')
        state.memory.add_utterance(text, speaker="owner", addressed=True)
        reply, catch_up = await converse(text)
        return {"reply": reply, "catch_up": catch_up}

    @app.get("/v1/models", dependencies=[Depends(auth)])
    async def models():
        return {"object": "list", "data": [{"id": "friday", "object": "model", "created": 0, "owned_by": "friday"}]}

    @app.post("/v1/chat/completions", dependencies=[Depends(auth)])
    async def chat_completions(body: dict):
        """OpenAI-compatible: any OpenAI client can talk to FRIDAY. FRIDAY keeps its own memory and
        history, so only the latest user message is used; the client's system prompt is ignored."""
        last = next((m for m in reversed(body.get("messages") or []) if m.get("role") == "user"), None)
        content = (last or {}).get("content")
        if isinstance(content, list):  # [{"type": "text", "text": ...}, ...]
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(400, "Send at least one user message with text.")
        result = await ask({"text": content})
        text = "\n\n".join(x for x in (result["reply"], result["catch_up"]) if x)
        cid, now = f"chatcmpl-{secrets.token_hex(8)}", int(time.time())
        if body.get("stream"):
            def chunk(delta, finish=None):
                return "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": now,
                                              "model": "friday", "choices": [
                                                  {"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
            events = [chunk({"role": "assistant", "content": text}), chunk({}, "stop"), "data: [DONE]\n\n"]
            return StreamingResponse(iter(events), media_type="text/event-stream")
        return {"id": cid, "object": "chat.completion", "created": now, "model": "friday",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    @app.post("/api/forget", dependencies=[Depends(auth)])
    async def forget(body: dict):
        try:
            return {"forgot": state.memory.forget(str(body.get("text", "")))}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/hud", dependencies=[Depends(auth)])
    async def hud():
        return {"presence": state.presence, "situation": situation(), "plugins": [p.name for p in state.plugins],
                "plugin_errors": state.plugin_errors,
                "learned": state.memory.tastes(), "notifications": state.memory.notifications(),
                "skills": {n: v["description"] for n, v in state.skills.all().items()},
                "routines": state.memory.routines(),
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
