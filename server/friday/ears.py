"""Ears and voice: paired Bluetooth earbuds <-> FRIDAY. Run: python -m friday.ears

Mic: `parec` reads the default PipeWire/PulseAudio source (pair the earbuds and pick their headset
profile; see docs/GUIDE.md). Speech detection cuts utterances, faster-whisper transcribes them on this
machine, and the text goes to the server over /ws, which decides whether you were talking to FRIDAY
(wake word, follow-up window, the model breaking ties). Replies and alerts are spoken with Piper and
played with `paplay` on the default sink (the earbuds). No audio is stored anywhere."""
import asyncio
import json
import os
import subprocess
import tempfile
import time

RATE, FRAME_MS = 16000, 30
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2  # 16-bit mono


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


async def main():
    import numpy as np
    import webrtcvad
    import websockets
    from faster_whisper import WhisperModel

    url = os.environ.get("FRIDAY_WS", "ws://127.0.0.1:8080/ws") + "?token=" + os.environ["FRIDAY_TOKEN"]
    stt = WhisperModel(os.environ.get("FRIDAY_WHISPER_MODEL", "base.en"), device="cpu", compute_type="int8")
    voice = os.environ.get("FRIDAY_PIPER_VOICE", "/data/voices/en_US-lessac-medium.onnx")
    vad = webrtcvad.Vad(int(os.environ.get("FRIDAY_VAD_LEVEL", "2")))  # 0-3: higher ignores more noise
    speaking = asyncio.Event()

    def transcribe(pcm):
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
        segments, _ = stt.transcribe(audio, language="en", vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    def speak(text):
        with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
            subprocess.run(["piper", "-m", voice, "-f", wav.name], input=text.encode(), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["paplay", wav.name], check=False)

    async with websockets.connect(url, max_size=2**20) as ws:
        async def listen():
            mic = await asyncio.create_subprocess_exec(
                "parec", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=30",
                stdout=asyncio.subprocess.PIPE)
            seg = Segmenter()
            while True:
                frame = await mic.stdout.readexactly(FRAME_BYTES)
                if speaking.is_set():
                    continue  # don't transcribe FRIDAY's own voice leaking back through the headset
                pcm = seg.push(frame, vad.is_speech(frame, RATE))
                if pcm:
                    text = await asyncio.to_thread(transcribe, pcm)
                    if text:
                        await ws.send(json.dumps({"type": "utterance", "text": text}))

        async def talk():
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") in ("say", "notify") and msg.get("text"):
                    speaking.set()
                    try:
                        await asyncio.to_thread(speak, msg["text"])
                    finally:
                        speaking.clear()

        await asyncio.gather(listen(), talk())


if __name__ == "__main__":
    while True:  # the server restarting or the earbuds dropping shouldn't end FRIDAY's hearing
        try:
            asyncio.run(main())
        except Exception as e:
            print(f"ears: {type(e).__name__}: {e}; retrying in 5 s", flush=True)
            time.sleep(5)
