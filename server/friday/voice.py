"""FRIDAY's ears and voice, run where the engine runs; devices only send and play audio. No audio is stored.

Ears: SenseVoice (via sherpa-onnx), which transcribes with punctuation in about 0.2 s and also hears *how*
you said it: emotion (happy, sad, angry, surprised…) and sounds like laughter or a cough.
Voice: Piper. Models download once into ~/.friday/models and load once."""
import io
import os
import threading
import wave

RATE = 16000  # what clients send: 16 kHz mono 16-bit PCM
MAX_BYTES = RATE * 2 * 30  # 30 s per utterance is plenty; more is a stuck mic
SENSEVOICE = "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/"
TONES = {"HAPPY": "happy", "SAD": "sad", "ANGRY": "frustrated", "FEARFUL": "worried", "DISGUSTED": "fed up",
         "SURPRISED": "surprised"}
SOUNDS = {"Laughter": "laughing", "Cry": "crying", "Cough": "coughing", "Sneeze": "sneezing"}


def heard_as(emotion, event):
    """SenseVoice tags -> a few words for FRIDAY, e.g. "sounded frustrated, laughing". None if unremarkable."""
    bits = [f"sounded {TONES[t]}" for t in TONES if t in (emotion or "")]
    bits += [SOUNDS[s] for s in SOUNDS if s in (event or "")]
    return ", ".join(bits) or None


class Voice:
    def __init__(self, piper_path=None, models_dir=None):
        self.piper_path = piper_path
        self.stt_dir = os.path.join(models_dir or os.path.dirname(piper_path or "."), "sensevoice")
        self._stt = self._tts = None
        self._lock = threading.Lock()  # transcription/synthesis run in worker threads

    def ensure(self, voice_name):
        """Download what's missing (about 300 MB, once). Returns whether speech is ready."""
        import subprocess
        import sys

        import httpx
        if self.piper_path and not os.path.exists(self.piper_path):
            os.makedirs(os.path.dirname(self.piper_path), exist_ok=True)
            subprocess.run([sys.executable, "-m", "piper.download_voices", "--data-dir",
                            os.path.dirname(self.piper_path), voice_name],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.makedirs(self.stt_dir, exist_ok=True)
        for name in ("tokens.txt", "model.int8.onnx"):
            path = os.path.join(self.stt_dir, name)
            if not os.path.exists(path):
                try:
                    with httpx.stream("GET", SENSEVOICE + name, follow_redirects=True, timeout=600) as r:
                        r.raise_for_status()
                        with open(path + ".part", "wb") as f:
                            for chunk in r.iter_bytes(1 << 20):
                                f.write(chunk)
                    os.replace(path + ".part", path)  # never leave a half model where a whole one should be
                except Exception:
                    break
        return self.available()

    def available(self):
        try:
            import piper  # noqa: F401
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return False
        return bool(self.piper_path) and os.path.exists(self.piper_path) and \
            os.path.exists(os.path.join(self.stt_dir, "model.int8.onnx"))

    def warm(self):
        """Load both models now, so FRIDAY's first answer is as quick as every other one."""
        if self.available():
            self.transcribe(b"\0" * RATE)  # one second of silence
            self.speak("Ready.")

    def transcribe(self, pcm):
        """16 kHz PCM -> (text, how it sounded or None)."""
        import numpy as np
        import sherpa_onnx
        with self._lock:
            if self._stt is None:
                self._stt = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=os.path.join(self.stt_dir, "model.int8.onnx"),
                    tokens=os.path.join(self.stt_dir, "tokens.txt"),
                    language="en", use_itn=True, num_threads=min(4, os.cpu_count() or 1))
            stream = self._stt.create_stream()
            stream.accept_waveform(RATE, np.frombuffer(pcm[:MAX_BYTES], np.int16).astype(np.float32) / 32768)
            self._stt.decode_stream(stream)
            r = stream.result
            return r.text.strip(), heard_as(r.emotion, r.event)

    def speak(self, text):
        """Text -> WAV bytes."""
        from piper import PiperVoice
        with self._lock:
            if self._tts is None:
                self._tts = PiperVoice.load(self.piper_path)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                self._tts.synthesize_wav(text, w)
            return buf.getvalue()
