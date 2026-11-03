"""FRIDAY from any device. Pair your headphones with the device, then just run `friday`.

    friday login http://<box>:8080 <token>       once per device
    friday                                        talk: headphone mic in, FRIDAY's voice out, live transcript
                                                  (you can also type while it listens)
    friday ask "did a new model drop today?"     one question, one answer
    friday chat                                   typed back-and-forth until Ctrl-D
    friday watch                                  live transcript only, no mic
    friday status | memory [query] | forget "…"   what FRIDAY knows and has learned
    friday devices                                list mics and speakers (use --mic / --speaker N)

FRIDAY_URL and FRIDAY_TOKEN override the saved login."""
import argparse
import asyncio
import base64
import json
import os
import sys

import httpx

DIM, BOLD, CYAN, YELLOW, OFF = ("\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m") if sys.stdout.isatty() \
    else ("",) * 5


CONFIG = os.path.join(os.path.expanduser("~"), ".config", "friday", "config.json")


def login_settings():
    saved = {}
    try:
        with open(CONFIG) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        pass
    url = os.environ.get("FRIDAY_URL") or saved.get("url") or "http://localhost:8080"
    token = os.environ.get("FRIDAY_TOKEN") or saved.get("token")
    if not token:
        sys.exit("Not logged in. Run: friday login http://<box>:8080 <FRIDAY_TOKEN>")
    return url.rstrip("/"), token


def client():
    url, token = login_settings()
    return httpx.Client(base_url=url, timeout=300, headers={"Authorization": f"Bearer {token}"})


def cmd_login(a):
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=15, headers={"Authorization": f"Bearer {a.token}"})
    call(c, "GET", "/api/hud")  # proves the URL and token before saving them
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # the token is a key to your life
    with os.fdopen(fd, "w") as f:
        json.dump({"url": a.url.rstrip("/"), "token": a.token}, f)
    print(f"Logged in to FRIDAY at {a.url}. Now run: friday")


def call(c, method, path, **kw):
    try:
        r = c.request(method, path, **kw)
    except httpx.HTTPError as e:
        sys.exit(f"Can't reach FRIDAY at {c.base_url}: {e}")
    if r.status_code == 401:
        sys.exit("FRIDAY refused the token. Run friday login again.")
    if r.status_code >= 400:
        sys.exit(f"FRIDAY said {r.status_code}: {r.text}")
    return r.json()


def speak(r):
    print(f"{CYAN}FRIDAY{OFF} {r['reply']}")
    if r.get("catch_up"):
        print(f"{YELLOW}FRIDAY{OFF} {r['catch_up']}")


def cmd_ask(c, a):
    speak(call(c, "POST", "/api/ask", json={"text": " ".join(a.text)}))


def cmd_chat(c, a):
    print(f"{DIM}Talking to FRIDAY. Ctrl-D to stop.{OFF}")
    while True:
        try:
            text = input(f"{BOLD}you{OFF} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if text:
            speak(call(c, "POST", "/api/ask", json={"text": text}))


def cmd_heard(c, a):
    r = call(c, "POST", "/api/utterance", json={"text": " ".join(a.text)})
    print(f"{CYAN}FRIDAY{OFF} {r['reply']}" if r["addressed"] else f"{DIM}(not for FRIDAY; remembered){OFF}")


def cmd_status(c, a):
    h = call(c, "GET", "/api/hud")
    print(f"{BOLD}Situation{OFF}  {h['situation']}")
    print(f"{BOLD}Plugins{OFF}    {', '.join(h['plugins']) or 'none'}")
    for err in h["plugin_errors"]:
        print(f"  {YELLOW}failed{OFF} {err}")
    if h["learned"]:
        print(f"{BOLD}Learned{OFF}    " + ", ".join(
            f"{k} {'interrupts' if v >= 0.4 else 'waits'} ({v:.2f})" for k, v in sorted(h["learned"].items())))
    print(f"{BOLD}Skills{OFF}     " + (", ".join(h["skills"]) or "none yet"))
    for r in h["routines"]:
        print(f"{BOLD}Routine{OFF}    #{r['id']} {r['at']} {r['days']}: {r['task']}")
    print(f"{BOLD}Lately{OFF}")
    for n in reversed(h["notifications"][:8]):
        print(f"  {DIM}{n['kind']:<9}{OFF} {n['text']}")


def cmd_memory(c, a):
    q = " ".join(a.query)
    m = call(c, "GET", "/api/memory", params={"q": q})
    for line in (m["results"] if q else m["profile"]) or ["(nothing yet)"]:
        print(f"  {line}")


def cmd_forget(c, a):
    gone = call(c, "POST", "/api/forget", json={"text": " ".join(a.text)})["forgot"]
    print("\n".join(f"  forgot: {g}" for g in gone) or "  nothing matched")


def show(m):
    """One line per event in the live transcript."""
    t = m.get("type")
    if t == "heard":
        return f"{DIM}you{OFF}    {m['text']}" + ("" if m["addressed"] else f" {DIM}(not for FRIDAY){OFF}")
    if t == "say":
        return f"{CYAN}FRIDAY{OFF} {m['text']}"
    if t == "notify":
        return f"{YELLOW}FRIDAY{OFF} {m['text']} {DIM}({m['kind']}){OFF}"
    return None


def ws_url(c):
    return str(c.base_url).replace("http", "ws", 1).rstrip("/") + "/ws?token=" + c.headers["authorization"][7:]


def cmd_watch(c, a):
    import websockets

    async def run():
        async with websockets.connect(ws_url(c)) as ws:
            print(f"{DIM}Watching FRIDAY. Ctrl-C to stop.{OFF}")
            async for raw in ws:
                line = show(json.loads(raw))
                if line:
                    print(line, flush=True)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


RATE, FRAME_MS = 16000, 30
FRAME = RATE * FRAME_MS // 1000  # samples per frame the speech detector wants


class Segmenter:
    """Turns a stream of (frame, is_speech) into utterances: speech, then `end_ms` of silence.
    Short blips (coughs, clicks) under `min_ms` are dropped; very long speech is cut at `max_ms`."""

    def __init__(self, end_ms=700, min_ms=300, max_ms=30000):
        self.end, self.min, self.max = end_ms // FRAME_MS, min_ms // FRAME_MS, max_ms // FRAME_MS
        self.frames, self.voiced, self.silence = [], 0, 0

    def push(self, frame, is_speech):
        if not self.frames and not is_speech:
            return None
        self.frames.append(frame)
        self.voiced += is_speech
        self.silence = 0 if is_speech else self.silence + 1
        if self.silence >= self.end or len(self.frames) >= self.max:
            audio, voiced = b"".join(self.frames), self.voiced
            self.frames, self.voiced, self.silence = [], 0, 0
            return audio if voiced >= self.min else None
        return None


def cmd_devices(c, a):
    import sounddevice as sd
    print(sd.query_devices())
    print(f"{DIM}Defaults are marked > (in) and < (out). Pick others with friday --mic N --speaker N.{OFF}")


def cmd_listen(c, a):
    """Voice mode: this device's mic (your paired headphones) is FRIDAY's ears; its speaker is FRIDAY's voice."""
    import io
    import wave

    import numpy as np
    import sounddevice as sd
    import webrtcvad
    import websockets

    vad = webrtcvad.Vad(a.vad)
    speaking = asyncio.Event()

    def play(wav_b64):
        with wave.open(io.BytesIO(base64.b64decode(wav_b64))) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16)
            sd.play(pcm, w.getframerate(), device=a.speaker, blocking=True)

    async def run():
        loop = asyncio.get_running_loop()
        frames = asyncio.Queue()
        # Bluetooth headset mics often only run at their own rate: open at what the device supports, resample.
        rate = RATE
        try:
            sd.check_input_settings(device=a.mic, samplerate=RATE, channels=1, dtype="int16")
        except Exception:
            rate = int(sd.query_devices(a.mic, "input")["default_samplerate"])
        block = int(rate * FRAME_MS / 1000)

        def on_audio(data, n, t, status):
            x = np.frombuffer(bytes(data), np.int16)
            if rate != RATE:
                x = np.interp(np.linspace(0, len(x) - 1, FRAME), np.arange(len(x)), x).astype(np.int16)
            loop.call_soon_threadsafe(frames.put_nowait, x.tobytes())

        async with websockets.connect(ws_url(c), max_size=2 ** 24) as ws:
            await ws.send(json.dumps({"type": "hello", "voice": True}))

            async def hear():
                seg = Segmenter()
                with sd.RawInputStream(samplerate=rate, blocksize=block, device=a.mic, channels=1,
                                       dtype="int16", callback=on_audio):
                    name = sd.query_devices(a.mic, "input")["name"]
                    print(f"{DIM}Listening on {name}. Say \"Friday…\", or type. Ctrl-C to stop.{OFF}", flush=True)
                    while True:
                        frame = await frames.get()
                        if speaking.is_set():
                            seg = Segmenter()  # don't send FRIDAY's own voice back to it
                            continue
                        pcm = seg.push(frame, vad.is_speech(frame, RATE))
                        if pcm:
                            await ws.send(json.dumps({"type": "audio", "pcm": base64.b64encode(pcm).decode()}))

            async def type_():
                while True:
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                    if not line:
                        return
                    if line.strip():
                        await ws.send(json.dumps({"type": "ask", "text": line.strip()}))

            async def talk():
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") == "ready" and not m.get("voice"):
                        print(f"{YELLOW}No voice on this server: {m.get('why')}. Typing still works.{OFF}")
                    line = show(m)
                    if line:
                        print(line, flush=True)
                    if m.get("audio"):
                        speaking.set()
                        try:
                            await asyncio.to_thread(play, m["audio"])
                        finally:
                            speaking.clear()

            await asyncio.gather(hear(), type_(), talk())

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print()


def main(argv=None):
    p = argparse.ArgumentParser(prog="friday", description="FRIDAY from any device. Run with no command to talk.")
    p.add_argument("--mic", type=int, help="input device number (see friday devices); default: system default")
    p.add_argument("--speaker", type=int, help="output device number; default: system default")
    p.add_argument("--vad", type=int, default=2, choices=range(4), help="speech detection strictness, 0-3")
    sub = p.add_subparsers(dest="cmd")
    lg = sub.add_parser("login", help="save the server URL and token on this device")
    lg.add_argument("url")
    lg.add_argument("token")
    for name, fn, arg, about in [
            ("listen", cmd_listen, None, "talk to FRIDAY (the default)"),
            ("ask", cmd_ask, "text", "one question, one answer"),
            ("chat", cmd_chat, None, "typed back-and-forth until Ctrl-D"),
            ("heard", cmd_heard, "text", "as if overheard: FRIDAY decides if it was for it"),
            ("watch", cmd_watch, None, "live transcript only, no mic"),
            ("status", cmd_status, None, "plugins, situation, what it learned, skills, routines"),
            ("memory", cmd_memory, "query", "what FRIDAY knows about you, or a search"),
            ("forget", cmd_forget, "text", "delete facts containing text"),
            ("devices", cmd_devices, None, "list mics and speakers")]:
        sp = sub.add_parser(name, help=about)
        if arg:
            sp.add_argument(arg, nargs="+" if arg == "text" else "*")
        sp.set_defaults(fn=fn)
    a = p.parse_args(argv)
    if a.cmd == "login":
        return cmd_login(a)
    (getattr(a, "fn", None) or cmd_listen)(client(), a)


if __name__ == "__main__":
    main()
