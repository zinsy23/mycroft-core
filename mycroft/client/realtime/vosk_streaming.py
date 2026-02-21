"""
Lightweight Vosk streaming wrapper.

Provides continuous partial results like ovos-stt-plugin-vosk but without dependencies.
"""

import json
import threading
from queue import Queue, Empty

from vosk import Model, KaldiRecognizer
from mycroft.util.log import LOG


class VoskStreamingThread(threading.Thread):
    """Thread that continuously processes audio and updates transcription."""

    def __init__(self, model, sample_rate):
        super().__init__(daemon=True)
        self.model = model
        self.sample_rate = sample_rate
        self.recognizer = KaldiRecognizer(model, sample_rate)
        self.recognizer.SetWords(True)

        self.queue = Queue()
        self.text = ""  # Current partial transcription
        self._stop = threading.Event()

    def run(self):
        """Process audio chunks from queue and update transcription."""
        while not self._stop.is_set():
            try:
                audio_chunk = self.queue.get(timeout=0.1)

                # Feed to Vosk
                self.recognizer.AcceptWaveform(audio_chunk)

                # Get partial result
                partial = json.loads(self.recognizer.PartialResult())
                self.text = partial.get("partial", "").strip()

            except Empty:
                continue
            except Exception as e:
                LOG.error(f"Error in Vosk streaming thread: {e}")

    def stop(self):
        """Stop the streaming thread."""
        self._stop.set()

    def reset(self):
        """Reset the recognizer for a new session."""
        self.recognizer = KaldiRecognizer(self.model, self.sample_rate)
        self.recognizer.SetWords(True)
        self.text = ""
        # Clear the queue
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except Empty:
                break
