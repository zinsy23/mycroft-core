"""
NVIDIA Riva streaming wrapper for realtime STT.

Provides word-by-word streaming recognition via Riva gRPC API.
"""

import threading
import queue
from mycroft.util.log import LOG

try:
    import riva.client
    RIVA_AVAILABLE = True
except ImportError:
    RIVA_AVAILABLE = False
    LOG.warning("nvidia-riva-client not installed - Riva STT unavailable")


class RivaStreamingThread(threading.Thread):
    """Thread that streams audio to Riva and provides word-by-word transcription."""

    def __init__(self, server_uri, model_name, sample_rate, word_callback=None):
        """Initialize Riva streaming thread.

        Args:
            server_uri: Riva server address (e.g., "localhost:50051")
            model_name: Riva model to use (e.g., "conformer-xl-en-US-asr-streaming-asr-bls-ensemble")
            sample_rate: Audio sample rate in Hz
            word_callback: Optional callback(words_list, is_final) for word events
        """
        super().__init__(daemon=True)

        if not RIVA_AVAILABLE:
            raise ImportError("nvidia-riva-client is required for Riva STT")

        self.server_uri = server_uri
        self.model_name = model_name
        self.sample_rate = sample_rate
        self.word_callback = word_callback

        # Audio queue for feeding Riva
        self.audio_queue = queue.Queue()

        # Current transcription state
        self.interim_text = ""
        self.final_text = ""

        # Control
        self._stop = threading.Event()
        self._reset_requested = threading.Event()

        # Connect to Riva
        try:
            self.auth = riva.client.Auth(uri=server_uri)
            self.asr_service = riva.client.ASRService(self.auth)
            LOG.info(f"Connected to Riva server at {server_uri}")
        except Exception as e:
            LOG.error(f"Failed to connect to Riva server: {e}")
            raise

    def run(self):
        """Main thread loop - processes streaming recognition."""
        while not self._stop.is_set():
            try:
                # Wait for reset or start streaming
                if self._reset_requested.is_set():
                    self._reset_requested.clear()
                    # Clear audio queue
                    while not self.audio_queue.empty():
                        try:
                            self.audio_queue.get_nowait()
                        except queue.Empty:
                            break
                    self.interim_text = ""
                    self.final_text = ""
                    continue

                # Start a new streaming session
                self._stream_session()

            except Exception as e:
                LOG.error(f"Error in Riva streaming thread: {e}")
                import time
                time.sleep(0.1)

    def _stream_session(self):
        """Run one streaming recognition session."""
        # Configure streaming recognition
        config = riva.client.StreamingRecognitionConfig(
            config=riva.client.RecognitionConfig(
                encoding=riva.client.AudioEncoding.LINEAR_PCM,
                language_code="en-US",
                sample_rate_hertz=self.sample_rate,
                max_alternatives=1,
                profanity_filter=False,
                enable_automatic_punctuation=False,
                verbatim_transcripts=True,
                model=self.model_name,
            ),
            interim_results=True,
        )

        try:
            # Start streaming
            responses = self.asr_service.streaming_response_generator(
                audio_chunks=self._audio_generator(),
                streaming_config=config,
            )

            # Process responses
            for response in responses:
                if self._stop.is_set() or self._reset_requested.is_set():
                    break

                if not response.results:
                    continue

                for result in response.results:
                    if not result.alternatives:
                        continue

                    transcript = result.alternatives[0].transcript.strip()
                    if not transcript:
                        continue

                    if result.is_final:
                        # Final result
                        self.final_text = transcript
                        self.interim_text = ""

                        # Trigger callback with final words
                        if self.word_callback:
                            words = transcript.split()
                            self.word_callback(words, is_final=True)

                    else:
                        # Interim result
                        self.interim_text = transcript

                        # Trigger callback with interim words
                        if self.word_callback:
                            words = transcript.split()
                            self.word_callback(words, is_final=False)

        except Exception as e:
            if not self._stop.is_set():
                LOG.error(f"Riva streaming session error: {e}")

    def _audio_generator(self):
        """Generator that yields audio chunks from the queue."""
        while not self._stop.is_set() and not self._reset_requested.is_set():
            try:
                chunk = self.audio_queue.get(timeout=0.1)
                if chunk is None:  # Sentinel to stop
                    break
                yield chunk
            except queue.Empty:
                continue

    def feed_audio(self, audio_chunk):
        """Feed audio chunk to Riva for recognition.

        Args:
            audio_chunk: bytes, PCM audio data
        """
        if not self._stop.is_set():
            self.audio_queue.put(audio_chunk)

    def reset(self):
        """Reset for a new recognition session."""
        self._reset_requested.set()
        self.interim_text = ""
        self.final_text = ""

    def stop(self):
        """Stop the streaming thread."""
        self._stop.set()
        self.audio_queue.put(None)  # Sentinel to unblock generator
