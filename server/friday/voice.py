"""Speech on the server, so any device with headphones can be FRIDAY's ears and voice: the `friday`
CLI sends what your mic heard; this turns it into text (Whisper) and turns FRIDAY's words into speech
(Piper). Models load once, on first use. No audio is stored."""
import io
import os
import threading
import wave

RATE = 16000  # what clients send: 16 kHz mono 16-bit PCM
MAX_BYTES = RATE * 2 * 30  # 30 s per utterance is plenty; more is a stuck mic


class Voice:
    def __init__(self, whisper=None, piper_path=None):
        self.whisper_name = whisper or os.environ.get("FRIDAY_WHISPER_MODEL", "base.en")
        self.piper_path = piper_path
        self._stt = self._tts = None
        self._lock = threading.Lock()  # transcription/synthesis run in worker threads

    def ensure(self, name):
        """Download the voice the first time (about 60 MB, once). Returns whether speech is ready."""
        if self.available() or not self.piper_path:
            return self.available()
        import subprocess
        import sys
        folder = os.path.dirname(self.piper_path)
        os.makedirs(folder, exist_ok=True)
        subprocess.run([sys.executable, "-m", "piper.download_voices", "--data-dir", folder, name],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.available()

    def available(self):
        try:
            import faster_whisper  # noqa: F401
            import piper  # noqa: F401
        except ImportError:
            return False
        return bool(self.piper_path) and os.path.exists(self.piper_path)

    def warm(self):
        """Load both models now, so FRIDAY's first answer is as quick as every other one."""
        if self.available():
            self.transcribe(b"\0" * RATE)  # one second of silence
            self.speak("Ready.")

    def transcribe(self, pcm):
        import numpy as np
        from faster_whisper import WhisperModel
        with self._lock:
            if self._stt is None:
                self._stt = WhisperModel(self.whisper_name, device="cpu", compute_type="int8")
            audio = np.frombuffer(pcm[:MAX_BYTES], np.int16).astype(np.float32) / 32768
            segments, _ = self._stt.transcribe(audio, language="en", vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()

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
