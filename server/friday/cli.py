"""FRIDAY from any device. Pair your headphones, run `friday`, talk.

    friday                                        talk: headphone mic in, FRIDAY's voice out, live transcript
                                                  (you can also type while it listens)
    friday ask "did a new model drop today?"     one question, one answer
    friday chat | watch | status | memory | forget | devices

FRIDAY runs right on this device and keeps everything in ~/.friday. To share one FRIDAY across devices,
run `friday serve` on an always-on machine and `friday login http://<it>:8080 <token>` everywhere else.
FRIDAY_URL and FRIDAY_TOKEN override the saved login."""
import argparse
import asyncio
import base64
import contextlib
import json
import os
import sys
import wave

import httpx

from .config import HOME

DIM, BOLD, CYAN, YELLOW, OFF = ("\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m") if sys.stdout.isatty() \
    else ("",) * 5


CONFIG = str(HOME / "config.json")  # this device's login (or its own local token), mode 0600


def saved():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(**changes):
    data = {**saved(), **changes}
    data = {k: v for k, v in data.items() if v is not None}
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # tokens are keys to your life
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def login_settings():
    """(url, token) of the FRIDAY this device talks to, or None to run FRIDAY right here."""
    conf = saved()
    url = os.environ.get("FRIDAY_URL") or conf.get("url")
    token = os.environ.get("FRIDAY_TOKEN") or conf.get("token")
    if not url:
        return None
    if not token:
        sys.exit("No token for that FRIDAY. Run: friday login <url> <token>")
    return url.rstrip("/"), token


def local_token():
    token = saved().get("local_token")
    if not token:
        import secrets
        token = secrets.token_urlsafe(24)
        save(local_token=token)
    return token


def start_here(background, host="127.0.0.1", port=0):
    """Run FRIDAY in this process: brain, memory, plugins, speech. Returns its URL and token."""
    import shutil
    import socket
    import threading
    import time

    import uvicorn

    from . import config
    from .app import create_app
    from .voice import Voice

    config.load_env_file()
    s = config.load()
    s.token = s.token or local_token()
    os.makedirs(s.skills_dir, exist_ok=True)
    for skill in (config.PACKAGE / "starter_skills").iterdir():  # starters; never overwrite FRIDAY's own edits
        if not os.path.exists(os.path.join(s.skills_dir, skill.name)):
            shutil.copytree(skill, os.path.join(s.skills_dir, skill.name))
    voice = Voice(piper_path=s.voice_path, models_dir=s.models_dir)
    if not voice.available():
        print(f"{DIM}Getting FRIDAY's ears and voice ready (one-time download, about 300 MB)…{OFF}", flush=True)
        if not voice.ensure(s.voice):
            print(f"{YELLOW}Couldn't download the voice; FRIDAY will answer in text.{OFF}")
    if not port:
        with socket.socket() as sock:
            sock.bind((host, 0))
            port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(s, background=background, voice=voice),
                                           host=host, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return f"http://{host}:{port}", s.token


def client(background=False):
    target = login_settings() or start_here(background)
    url, token = target
    return httpx.Client(base_url=url, timeout=300, headers={"Authorization": f"Bearer {token}"})


def cmd_login(a):
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=15, headers={"Authorization": f"Bearer {a.token}"})
    call(c, "GET", "/api/hud")  # proves the URL and token before saving them
    save(url=a.url.rstrip("/"), token=a.token)
    print(f"This device now talks to the FRIDAY at {a.url}. Run: friday")


def cmd_logout(a):
    save(url=None, token=None)
    print("This device runs its own FRIDAY again.")


def cmd_serve(a):
    """Run the always-on FRIDAY that your other devices log in to."""
    import time
    url, token = start_here(background=True, host=a.host, port=a.port)
    print(f"FRIDAY is running at {url}.")
    print(f"On your other devices: friday login http://<this machine>:{a.port} {token}")
    while True:
        time.sleep(3600)


def call(c, method, path, **kw):
    try:
        r = c.request(method, path, **kw)
    except httpx.HTTPError as e:
        sys.exit(f"Can't reach FRIDAY at {c.base_url}: {e}")
    if r.status_code == 401:
        sys.exit("FRIDAY refused the token. Run friday login again (or friday logout to run FRIDAY here).")
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


def printer():
    """Live transcript: FRIDAY's sentences appear on one line as they're spoken."""
    mid = [False]

    def out(m):
        if m.get("type") == "say_part":
            print((f"{CYAN}FRIDAY{OFF} " if not mid[0] else "") + m["text"], end=" ", flush=True)
            mid[0] = True
            return
        if mid[0]:
            print()
            mid[0] = False
            if m.get("type") == "say":
                return  # already shown sentence by sentence
        line = show(m)
        if line:
            print(line, flush=True)
    return out


def ws_url(c):
    return str(c.base_url).replace("http", "ws", 1).rstrip("/") + "/ws?token=" + c.headers["authorization"][7:]


def cmd_watch(c, a):
    import websockets

    async def run():
        async with websockets.connect(ws_url(c)) as ws:
            print(f"{DIM}Watching FRIDAY. Ctrl-C to stop.{OFF}")
            out = printer()
            async for raw in ws:
                out(json.loads(raw))
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


RATE, FRAME_MS = 16000, 30
FRAME = RATE * FRAME_MS // 1000  # samples per frame the speech detector wants


class Segmenter:
    """Turns a stream of (frame, is_speech) into utterances: speech, then `end_ms` of silence.
    Short blips (coughs, clicks) under `min_ms` are dropped; very long speech is cut at `max_ms`."""

    def __init__(self, end_ms=600, min_ms=300, max_ms=30000):
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


class SystemSound:
    """Linux without PortAudio: the recorder and player the system already has (PulseAudio/PipeWire or
    ALSA), so installing FRIDAY never needs sudo. Offers the few sounddevice calls FRIDAY uses."""

    def __init__(self):
        import shutil
        self.rec = (["parec", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=30"]
                    if shutil.which("parec") else
                    ["arecord", "-q", "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"]
                    if shutil.which("arecord") else None)
        self.player = ["paplay"] if shutil.which("paplay") else ["aplay", "-q"] if shutil.which("aplay") else None
        if not self.rec or not self.player:
            raise RuntimeError("No way to use the mic here: install PortAudio (libportaudio2) or "
                               "PulseAudio/ALSA tools (pulseaudio-utils or alsa-utils).")
        self.proc, self.wav = None, None

    def check_input_settings(self, **kw):
        pass  # the recorder delivers 16 kHz mono itself

    def query_devices(self, device=None, kind=None):
        return {"name": f"the system default ({self.rec[0]})", "default_samplerate": RATE}

    def RawInputStream(self, samplerate, blocksize, callback, **kw):
        import contextlib
        import subprocess
        import threading

        @contextlib.contextmanager
        def stream():
            proc = subprocess.Popen(self.rec, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

            def pump():
                while (data := proc.stdout.read(blocksize * 2)) and len(data) == blocksize * 2:
                    callback(data, blocksize, None, None)
            threading.Thread(target=pump, daemon=True).start()
            try:
                yield self
            finally:
                proc.terminate()
        return stream()

    def play(self, pcm, rate, device=None):
        import subprocess
        import tempfile
        self.stop()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f, wave.open(f, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(pcm.tobytes())
        self.wav = f.name
        self.proc = subprocess.Popen(self.player + [self.wav], stderr=subprocess.DEVNULL)

    def get_stream(self):
        return self

    @property
    def active(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.active:
            self.proc.terminate()
        if self.wav:
            with contextlib.suppress(OSError):
                os.remove(self.wav)
            self.wav = None


def sound():
    """sounddevice when PortAudio is there (always on macOS/Windows), else the system's own tools."""
    try:
        import sounddevice
        return sounddevice
    except OSError:  # Linux without libportaudio2
        return SystemSound()


def cmd_devices(c, a):
    sd = sound()
    if isinstance(sd, SystemSound):
        print(f"Using {sd.query_devices()['name']} for the mic and {sd.player[0]} for sound. Pick devices in your "
              "system sound settings (install libportaudio2 to choose them here instead).")
        return
    print(sd.query_devices())
    print(f"{DIM}Defaults are marked > (in) and < (out). Pick others with friday --mic N --speaker N.{OFF}")


BARGE_IN_MS = 400  # this much of your voice while FRIDAY talks cuts it off


def cmd_listen(c, a):
    """Voice mode: this device's mic (your paired headphones) is FRIDAY's ears; its speaker is FRIDAY's voice."""
    import collections
    import io

    import numpy as np
    import webrtcvad
    import websockets

    sd = sound()
    vad, strict = webrtcvad.Vad(a.vad), webrtcvad.Vad(3)  # stricter while FRIDAY talks: ignore leakage
    speaking, cut = asyncio.Event(), asyncio.Event()

    def decode(wav_b64):
        with wave.open(io.BytesIO(base64.b64decode(wav_b64))) as w:
            return np.frombuffer(w.readframes(w.getnframes()), np.int16), w.getframerate()

    async def run():
        loop = asyncio.get_running_loop()
        frames, voice_q = asyncio.Queue(), asyncio.Queue()
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
                seg, recent, run_len = Segmenter(), collections.deque(maxlen=BARGE_IN_MS // FRAME_MS + 5), 0
                with sd.RawInputStream(samplerate=rate, blocksize=block, device=a.mic, channels=1,
                                       dtype="int16", callback=on_audio):
                    name = sd.query_devices(a.mic, "input")["name"]
                    print(f"{DIM}Listening on {name}. Talk to FRIDAY, or type. Ctrl-C to stop.{OFF}", flush=True)
                    while True:
                        frame = await frames.get()
                        if speaking.is_set():
                            recent.append(frame)
                            run_len = run_len + 1 if strict.is_speech(frame, RATE) else 0
                            if a.barge_in and run_len * FRAME_MS >= BARGE_IN_MS:  # you're talking over FRIDAY
                                cut.set()
                                while not voice_q.empty():  # the rest of this reply is moot
                                    voice_q.get_nowait()
                                await ws.send(json.dumps({"type": "interrupt"}))
                                seg = Segmenter()
                                for f in recent:  # keep what you already said
                                    seg.push(f, True)
                                run_len = 0
                            continue
                        pcm = seg.push(frame, vad.is_speech(frame, RATE))
                        if pcm:
                            await ws.send(json.dumps({"type": "audio", "pcm": base64.b64encode(pcm).decode()}))

            async def speak():
                while True:
                    pcm, sr = decode(await voice_q.get())
                    speaking.set()
                    sd.play(pcm, sr, device=a.speaker)
                    while sd.get_stream().active and not cut.is_set():
                        await asyncio.sleep(0.03)
                    if cut.is_set():
                        sd.stop()
                    if voice_q.empty():
                        speaking.clear()

            async def type_():
                while True:
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                    if not line:
                        return
                    if line.strip():
                        await ws.send(json.dumps({"type": "ask", "text": line.strip()}))

            async def talk():
                out = printer()
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") == "ready" and not m.get("voice"):
                        print(f"{YELLOW}No voice on this server: {m.get('why')}. Typing still works.{OFF}")
                    if m.get("type") == "heard" and m.get("addressed"):
                        cut.clear()  # a new turn: FRIDAY may speak again
                        while not voice_q.empty():
                            voice_q.get_nowait()
                        speaking.clear()
                    out(m)
                    if m.get("audio") and not (cut.is_set() and m.get("type") == "say_part"):
                        voice_q.put_nowait(m["audio"])
                    if m.get("type") == "say":
                        cut.clear()  # the interrupted reply is over; alerts and the next reply may speak

            await asyncio.gather(hear(), speak(), type_(), talk())

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print()


# --- your data -------------------------------------------------------------------------------
def private_write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # it's your life: only you can read it
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def cmd_export(c, a):
    from datetime import date
    data = call(c, "GET", "/api/export", params={"transcripts": a.transcripts})
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if a.path == "-":
        print(text)
        return
    path = a.path or f"friday-export-{date.today():%Y%m%d}.json"
    private_write(path, text)
    print(f"Saved {len(data['facts'])} facts, {len(data['links'])} links and {len(data['skills'])} skills "
          f"to {path}.")


def cmd_import(c, a):
    try:
        with open(a.path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        sys.exit(f"Can't read {a.path}: {e.strerror}")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("friday_export"):
        n = call(c, "POST", "/api/import", json=data)
        print("Imported " + ", ".join(f"{v} {k}" for k, v in n.items() if v) + ".")
    else:  # notes, a chat export, a bio: FRIDAY reads it and keeps what matters
        print(f"{DIM}Reading {a.path}…{OFF}", flush=True)
        n = call(c, "POST", "/api/learn", json={"text": text})
        print(f"Learned {n['facts']} facts, {n['reminders']} reminders and {n['links']} links from {a.path}.")


def mermaid(g):
    ids = {n["name"]: f"n{i}" for i, n in enumerate(g["nodes"])}
    q = lambda t: str(t).replace('"', "'")  # noqa: E731
    return "\n".join(["graph LR"] + [f'  {ids[n["name"]]}["{q(n["name"])}"]' for n in g["nodes"]]
                     + [f'  {ids[e["from"]]} -- "{q(e["rel"])}" --> {ids[e["to"]]}' for e in g["edges"]])


def dot(g):
    q = lambda t: json.dumps(str(t))  # noqa: E731
    return "\n".join(["digraph friday {", "  rankdir=LR;"]
                     + [f'  {q(e["from"])} -> {q(e["to"])} [label={q(e["rel"])}];' for e in g["edges"]] + ["}"])


def cmd_graph(c, a):
    g = call(c, "GET", "/api/graph", params={"entity": " ".join(a.entity), "depth": a.depth})
    if a.format == "json":
        print(json.dumps(g, indent=2, ensure_ascii=False))
    elif a.format == "mermaid":
        print(mermaid(g))
    elif a.format == "dot":
        print(dot(g))
    elif not g["edges"]:
        print("  (nothing mapped yet: FRIDAY builds this as you talk, and every night)")
    else:
        kinds = {n["name"]: n["kind"] for n in g["nodes"]}
        for e in sorted(g["edges"], key=lambda e: -e["weight"]):
            tag = lambda n: f"{n} {DIM}({kinds.get(n, 'other')}){OFF}" if kinds.get(n, "other") != "other" else n  # noqa: E731
            print(f"  {tag(e['from'])} {CYAN}─{e['rel']}→{OFF} {tag(e['to'])}" + (f" {DIM}×{e['weight']}{OFF}"
                                                                                 if e["weight"] > 1 else ""))


# --- settings, setup, health --------------------------------------------------------------------
ENV = str(HOME / ".env")
SECRET = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def env_lines():
    try:
        with open(ENV, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def set_env(key, value):
    """Set (or with value None, remove) one setting in ~/.friday/.env, keeping everything else."""
    key = key.strip().upper()
    if not key.replace("_", "").isalnum():
        sys.exit(f"Not a setting name: {key}")
    lines = [ln for ln in env_lines() if ln.split("=", 1)[0].strip() != key]
    if value is not None:
        lines.append(f"{key}={value}")
    os.makedirs(HOME, exist_ok=True)
    private_write(ENV, "\n".join(lines) + "\n")


def cmd_config(a):
    if a.action == "set":
        if len(a.args) < 2:
            sys.exit("Usage: friday config set NAME VALUE")
        set_env(a.args[0], " ".join(a.args[1:]))
        print(f"Set {a.args[0].upper()}. It takes effect next time FRIDAY starts.")
    elif a.action == "unset":
        for k in a.args:
            set_env(k, None)
        print("Removed " + ", ".join(k.upper() for k in a.args) + ".")
    else:
        rows = [ln.split("=", 1) for ln in env_lines() if "=" in ln and not ln.lstrip().startswith("#")]
        if not rows:
            print(f"  No settings yet ({ENV}). Try: friday setup")
        for k, v in rows:
            shown = (v[:4] + "…" if len(v) > 8 else "…") if any(w in k for w in SECRET) and v else v
            print(f"  {k}={shown}")
        if saved().get("url"):
            print(f"{DIM}This device uses the FRIDAY at {saved()['url']}; its settings live there.{OFF}")


def cmd_setup(a):
    import getpass
    import shutil
    print(f"{BOLD}Let's set FRIDAY up.{OFF} Everything stays on this machine, in {HOME}.\n")
    name = input("What should FRIDAY call you? ").strip()
    if name:
        set_env("FRIDAY_OWNER_NAME", name)
    print("\nWhich brain?\n  1) Claude, with an API key (console.anthropic.com)\n"
          "  2) Any OpenAI-compatible API: OpenRouter, Ollama, LM Studio, llama.cpp, OpenAI…")
    if shutil.which("ant"):
        print("  3) Claude, signing in with `ant auth login`")
    choice = input("Pick one [1]: ").strip() or "1"
    if choice == "2":
        url = input("Base URL [http://localhost:11434/v1]: ").strip() or "http://localhost:11434/v1"
        key = getpass.getpass("API key (Enter if none): ").strip()
        model = ""
        while not model:
            model = input("Model name (e.g. llama3.3, anthropic/claude-opus-5.5): ").strip()
        set_env("FRIDAY_LLM_URL", url), set_env("FRIDAY_LLM_KEY", key or None), set_env("FRIDAY_MODEL", model)
    else:
        set_env("FRIDAY_LLM_URL", None), set_env("FRIDAY_LLM_KEY", None)
        if choice == "3" and shutil.which("ant"):
            import subprocess
            subprocess.run(["ant", "auth", "login"], check=False)
        else:
            key = getpass.getpass("Anthropic API key: ").strip()
            if key:
                set_env("ANTHROPIC_API_KEY", key)
    print()
    cmd_doctor(client(), a)
    print(f"\n{BOLD}All set.{OFF} Put your headphones on and run: {CYAN}friday{OFF}")


def cmd_doctor(c, a):
    ok, bad = f"{CYAN}✓{OFF}", f"{YELLOW}✗{OFF}"
    try:
        sd = sound()
        mic = sd.query_devices(None, "input")["name"] if not isinstance(sd, SystemSound) else sd.query_devices()["name"]
        print(f"  {ok} Mic: {mic}")
    except Exception as e:
        print(f"  {bad} Mic: {e}")
    d = call(c, "GET", "/api/doctor")
    print(f"  {ok if d['brain'] == 'ok' else bad} Brain: {d['model']} via {d['via']}"
          + ("" if d["brain"] == "ok" else f": {d['brain']}"))
    print(f"  {ok if d['speech'] else bad} Ears and voice" + ("" if d["speech"] else ": models missing (run friday)"))
    print(f"  {ok} Plugins: {', '.join(d['plugins']) or 'none'}")
    for err in d["plugin_errors"]:
        print(f"  {bad} {err}")
    print(f"  {ok} {'Shared hub: ' + str(c.base_url) if saved().get('url') else 'Running on this device'}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="friday", description="FRIDAY from any device. Run with no command to talk.")
    p.add_argument("--mic", type=int, help="input device number (see friday devices); default: system default")
    p.add_argument("--speaker", type=int, help="output device number; default: system default")
    p.add_argument("--vad", type=int, default=2, choices=range(4), help="speech detection strictness, 0-3")
    p.add_argument("--no-barge-in", dest="barge_in", action="store_false",
                   help="don't stop FRIDAY when you talk over it (use with speakers instead of headphones)")
    sub = p.add_subparsers(dest="cmd")
    lg = sub.add_parser("login", help="use the FRIDAY running on another machine (shared memory)")
    lg.add_argument("url")
    lg.add_argument("token")
    sub.add_parser("logout", help="go back to running FRIDAY on this device")
    sv = sub.add_parser("serve", help="run the always-on FRIDAY your other devices log in to")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)
    ex = sub.add_parser("export", help="save everything FRIDAY knows to a JSON file you own")
    ex.add_argument("path", nargs="?", help="file (default friday-export-DATE.json; - for stdout)")
    ex.add_argument("--transcripts", action="store_true", help="also include kept raw speech")
    ex.set_defaults(fn=cmd_export)
    im = sub.add_parser("import", help="merge a FRIDAY export, or learn from any notes/text file")
    im.add_argument("path")
    im.set_defaults(fn=cmd_import)
    gr = sub.add_parser("graph", help="the map of your world FRIDAY builds")
    gr.add_argument("entity", nargs="*", help="look around one thing (default: strongest links)")
    gr.add_argument("--depth", type=int, default=1)
    gr.add_argument("--format", choices=("text", "mermaid", "dot", "json"), default="text")
    gr.set_defaults(fn=cmd_graph)
    cf = sub.add_parser("config", help="show or change settings: config | config set NAME VALUE | config unset NAME")
    cf.add_argument("action", nargs="?", choices=("show", "set", "unset"), default="show")
    cf.add_argument("args", nargs="*")
    sub.add_parser("setup", help="first-run setup: your name and FRIDAY's brain")
    sub.add_parser("doctor", help="check mic, brain, ears, voice and plugins").set_defaults(fn=cmd_doctor)
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
    if a.cmd in ("login", "logout", "serve", "config", "setup"):
        return {"login": cmd_login, "logout": cmd_logout, "serve": cmd_serve, "config": cmd_config,
                "setup": cmd_setup}[a.cmd](a)
    fn = getattr(a, "fn", None) or cmd_listen
    fn(client(background=fn in (cmd_listen, cmd_watch)), a)


if __name__ == "__main__":
    main()
