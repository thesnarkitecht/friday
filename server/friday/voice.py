"""FRIDAY's ears and voice, run where the engine runs; devices only send and play audio. No audio is stored.

Both come from sherpa-onnx, one library that works with any Python (including the one uv installs):
- Ears: SenseVoice, which transcribes with punctuation in about 0.2 s and also hears *how* you said it:
  emotion (happy, sad, angry, surprised…) and sounds like laughter or a cough.
- Voice: a Piper voice (default: British "Jenny"), about 0.1 s per second of speech.
Models download once into ~/.friday/models and load once."""
import io
import os
import threading
import wave

RATE = 16000  # what clients send: 16 kHz mono 16-bit PCM
MAX_BYTES = RATE * 2 * 30  # 30 s per utterance is plenty; more is a stuck mic
SENSEVOICE = "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/"
VOICES = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-piper-{}.tar.bz2"
TONES = {"HAPPY": "happy", "SAD": "sad", "ANGRY": "frustrated", "FEARFUL": "worried", "DISGUSTED": "fed up",
         "SURPRISED": "surprised"}
SOUNDS = {"Laughter": "laughing", "Cry": "crying", "Cough": "coughing", "Sneeze": "sneezing"}


def heard_as(emotion, event):
    """SenseVoice tags -> a few words for FRIDAY, e.g. "sounded frustrated, laughing". None if unremarkable."""
    bits = [f"sounded {TONES[t]}" for t in TONES if t in (emotion or "")]
    bits += [SOUNDS[s] for s in SOUNDS if s in (event or "")]
    return ", ".join(bits) or None


def _download(url, path):
    import httpx
    with httpx.stream("GET", url, follow_redirects=True, timeout=600) as r:
        r.raise_for_status()
        with open(path + ".part", "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    os.replace(path + ".part", path)  # never leave a half file where a whole one should be


class Voice:
    def __init__(self, name="en_GB-jenny_dioco-medium", models_dir="."):
        self.name, self.models_dir = name, models_dir
        self.stt_dir = os.path.join(models_dir, "sensevoice")
        self.tts_dir = os.path.join(models_dir, f"vits-piper-{name}")
        self._stt = self._tts = None
        self._lock = threading.Lock()  # transcription/synthesis run in worker threads

    def ensure(self):
        """Download what's missing (about 300 MB, once). Returns whether speech is ready."""
        import tarfile
        os.makedirs(self.stt_dir, exist_ok=True)
        try:
            for name in ("tokens.txt", "model.int8.onnx"):
                path = os.path.join(self.stt_dir, name)
                if not os.path.exists(path):
                    _download(SENSEVOICE + name, path)
            if not os.path.isdir(self.tts_dir):
                archive = self.tts_dir + ".tar.bz2"
                _download(VOICES.format(self.name), archive)
                with tarfile.open(archive) as t:
                    t.extractall(self.models_dir, filter="data")  # "data": no paths outside models_dir
                os.remove(archive)
        except Exception:
            pass  # offline or blocked: FRIDAY still works in text
        return self.available()

    def available(self):
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return False
        return os.path.exists(os.path.join(self.stt_dir, "model.int8.onnx")) and \
            os.path.exists(os.path.join(self.tts_dir, "tokens.txt"))

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
        import numpy as np
        import sherpa_onnx
        with self._lock:
            if self._tts is None:
                onnx = next(f for f in os.listdir(self.tts_dir) if f.endswith(".onnx"))
                self._tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
                    model=sherpa_onnx.OfflineTtsModelConfig(vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=os.path.join(self.tts_dir, onnx), tokens=os.path.join(self.tts_dir, "tokens.txt"),
                        data_dir=os.path.join(self.tts_dir, "espeak-ng-data")),
                        num_threads=min(4, os.cpu_count() or 1))))
            audio = self._tts.generate(text, sid=0, speed=1.0)
            pcm = (np.clip(np.asarray(audio.samples), -1, 1) * 32767).astype(np.int16)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(audio.sample_rate), w.writeframes(pcm.tobytes())
            return buf.getvalue()
