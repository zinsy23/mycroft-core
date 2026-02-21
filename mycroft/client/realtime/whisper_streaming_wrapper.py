"""
Whisper streaming wrapper for real-time word-by-word transcription.

Based on whisper_streaming library with ASRProcessor for live streaming.
"""

import threading
from queue import Queue, Empty
import numpy as np

from whisper_streaming.backend import FasterWhisperASR, FasterWhisperModelConfig, FasterWhisperTranscribeConfig, FasterWhisperFeatureExtractorConfig
from whisper_streaming.processor import ASRProcessor, AudioReceiver, OutputSender, TimeTrimming
from whisper_streaming.base import Backend, Word

from mycroft.util.log import LOG


class QueueAudioReceiver(AudioReceiver):
    """Audio receiver that reads from an input queue instead of microphone.

    Accumulates small chunks into larger chunks matching chunk_seconds.
    Uses dynamic chunking: larger first chunk for context, then smaller chunks.
    The parent AudioReceiver thread will call _do_receive() and put results
    into self.queue for the ASRProcessor to consume.
    """

    def __init__(self, chunk_seconds: float, sample_rate: int, initial_chunk_seconds: float = 0.5):
        super().__init__()
        self.chunk_seconds = chunk_seconds
        self.initial_chunk_seconds = initial_chunk_seconds
        self.sample_rate = sample_rate
        self.chunk_samples = int(chunk_seconds * sample_rate)
        self.initial_chunk_samples = int(initial_chunk_seconds * sample_rate)
        self.input_queue = Queue()  # Unbounded queue - block instead of dropping audio
        self._buffer = []  # Accumulate samples here
        self._first_chunk = True  # Track if this is first chunk of session
        self._received_real_audio = False  # Track if we've ever received real audio

    def _do_receive(self) -> np.ndarray | None:
        """Receive audio chunk from input_queue.

        Returns silence at startup, then blocks for real audio once a session starts.
        """
        if self.stopped.is_set():
            return None

        try:
            # Use timeout until we've received real audio (for startup)
            # Then block indefinitely like test script's mic.read()
            timeout = None if self._received_real_audio else 0.5

            chunk = self.input_queue.get(block=True, timeout=timeout)
            self._received_real_audio = True  # Mark that we've seen real audio
            return chunk
        except Empty:
            # Timeout at startup - return silence to keep ASRProcessor alive
            return np.zeros(int(self.chunk_seconds * self.sample_rate), dtype=np.float32)
        except:
            return None

    def _do_close(self):
        """Stop receiving audio."""
        pass  # stopped flag is in parent class


class WordCollector(OutputSender):
    """Collects words from whisper_streaming and calls callback for each word.

    This matches the PrintSender behavior in test_live_stt2.py but instead of
    printing, it calls a callback function.
    """

    def __init__(self, word_callback=None):
        super().__init__()
        self.word_callback = word_callback  # Called for each word as it arrives
        self.words = []
        self.text = ""
        self._lock = threading.Lock()

    def _do_output(self, word: Word) -> None:
        """Receive a new word from the streaming processor.

        This is called by ASRProcessor when a word is recognized - exactly like
        PrintSender._do_output() in the test script.

        Note: word.word may contain multiple words (e.g., "Set color"), so we
        split it and call the callback for each individual word.
        """
        with self._lock:
            word_text = word.word.strip()
            LOG.info(f"[WHISPER WORD] {word_text}")

            # Split into individual words and process each one
            individual_words = word_text.split()
            for w in individual_words:
                self.words.append(w)
                self.text = " ".join(self.words)

                # Call callback for each individual word (event-driven, not polling)
                if self.word_callback:
                    self.word_callback(w)

    def _do_close(self):
        """Cleanup."""
        pass

    def get_text(self):
        """Get current accumulated text."""
        with self._lock:
            return self.text

    def reset(self):
        """Clear accumulated words."""
        with self._lock:
            self.words = []
            self.text = ""


class WhisperStreamingThread(threading.Thread):
    """Thread that runs ASRProcessor for continuous whisper streaming."""

    def __init__(self, model_path, device, compute_type, sample_rate, chunk_seconds=0.5, window_seconds=3.0, word_callback=None):
        super().__init__(daemon=True)
        self.sample_rate = sample_rate
        self.chunk_seconds = chunk_seconds
        self.window_seconds = window_seconds
        self.word_callback = word_callback

        # Store config for recreating processor on restart
        self.model_path = model_path
        self.device = device
        self.compute_type = compute_type

        LOG.info(f"Initializing Whisper streaming: {model_path}")
        LOG.info(f"  Device: {device}, Compute type: {compute_type}")

        # Create initial components
        self._create_processor()
        self._stop = threading.Event()

        # Note: audio_receiver thread is started by ASRProcessor.run(), not here
        LOG.info("Whisper streaming initialized")

    def _create_processor(self):
        """Create or recreate the ASR processor with fresh thread instances."""
        # Create audio receiver (queue-based)
        # Reuse existing queue if we have one (on restart)
        if hasattr(self, 'audio_receiver'):
            old_queue = self.audio_receiver.input_queue
            self.audio_receiver = QueueAudioReceiver(self.chunk_seconds, self.sample_rate)
            self.audio_receiver.input_queue = old_queue  # Preserve the queue
        else:
            self.audio_receiver = QueueAudioReceiver(self.chunk_seconds, self.sample_rate)

        # Create word collector (with callback for event-driven word processing)
        # Always create fresh instance since threads can only be started once
        self.word_collector = WordCollector(word_callback=self.word_callback)

        # Configure whisper backend
        model_config = FasterWhisperModelConfig(
            model_size_or_path=self.model_path,
            device=self.device,
            compute_type=self.compute_type,
        )
        transcribe_config = FasterWhisperTranscribeConfig(
            vad_filter=True,  # Keep VAD enabled like test script
            no_speech_threshold=0.9,  # CORRECTED: Higher = faster end-of-speech detection
            hallucination_silence_threshold=0.5,  # Use test script defaults
        )
        feature_extractor_config = FasterWhisperFeatureExtractorConfig()

        # Configure processor (use default buffer config like test script)
        processor_config = ASRProcessor.ProcessorConfig(
            sampling_rate=self.sample_rate,
            prompt_size=200,
            audio_receiver_timeout=5.0,
            audio_trimming=TimeTrimming(seconds=self.window_seconds),
            language="en",
        )

        # Create ASR processor
        self.processor = ASRProcessor(
            processor_config=processor_config,
            audio_receiver=self.audio_receiver,
            output_senders=self.word_collector,
            backend=Backend.FASTER_WHISPER,
            model_config=model_config,
            transcribe_config=transcribe_config,
            feature_extractor_config=feature_extractor_config,
        )

    @property
    def queue(self):
        """Audio input queue for feeding audio from realtime_loop."""
        return self.audio_receiver.input_queue

    @property
    def text(self):
        """Current accumulated text."""
        return self.word_collector.get_text()

    def run(self):
        """Run the ASR processor with auto-restart on crash.

        Note: ASRProcessor catches its own exceptions and logs them, so we can't
        catch them. Instead, we detect when run() exits and restart it.
        """
        import time
        while not self._stop.is_set():
            LOG.info("Starting Whisper streaming processor")
            try:
                self.processor.run()
            except Exception as e:
                LOG.error(f"Whisper streaming exception: {e}")

            # If we get here, processor.run() exited (either normally or due to crash)
            if self._stop.is_set():
                LOG.info("Whisper streaming stopped normally")
                break

            # run() exited unexpectedly - recreate processor with fresh threads
            LOG.warning("Whisper streaming processor exited unexpectedly (whisper_streaming bug), restarting in 1s...")
            time.sleep(1.0)
            LOG.info("Recreating processor with fresh thread instances")
            self._create_processor()

    def stop(self):
        """Stop the streaming thread."""
        LOG.info("Stopping Whisper streaming")
        self._stop.set()
        self.audio_receiver._do_close()

    def reset(self):
        """Reset accumulated words and prepare for new session."""
        self.word_collector.reset()
        self.audio_receiver._first_chunk = True  # Reset for dynamic chunking
