"""
NVIDIA Riva streaming wrapper for realtime STT.

Provides word-by-word streaming recognition via Riva gRPC API.
Streams are created on-demand per command session and torn down when complete.
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
    """Thread that streams audio to Riva and provides word-by-word transcription.

    Design: Stream starts when reset() is called (after wakeword), processes audio
    until session timeout or command completion, then stops completely. Next wakeword
    creates a fresh stream with START flag. This prevents Riva idle timeout errors.
    """

    def __init__(self, server_uri, model_name, sample_rate, word_callback=None):
        """Initialize Riva streaming thread.

        Args:
            server_uri: Riva server address (e.g., "localhost:50051")
            model_name: Riva model to use (e.g., "conformer-xl-en-US-asr-streaming-asr-bls-ensemble")
            sample_rate: Audio sample rate in Hz
            word_callback: Optional callback(words_list, is_final, word_timings) for word events.
                word_timings is a list of (start_sec, end_sec) floats parallel to words_list,
                or None if timings unavailable (always None for interim results).
                Riva provides timings as int nanoseconds; converted to float seconds here.
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

        # Control - stream only runs when session_active is set
        self._stop = threading.Event()
        self._session_active = threading.Event()
        self._session_active.clear()  # Start with no active session

        # Connect to Riva (connection is persistent, but streams are per-session)
        try:
            self.auth = riva.client.Auth(uri=server_uri)
            self.asr_service = riva.client.ASRService(self.auth)
            LOG.info(f"Connected to Riva server at {server_uri}")
        except Exception as e:
            LOG.error(f"Failed to connect to Riva server: {e}")
            raise

    def run(self):
        """Main thread loop - waits for session activation then runs stream.

        Stream lifecycle:
        1. Wait for reset() to set _session_active (triggered by wakeword)
        2. Start fresh Riva stream with START flag
        3. Process audio until timeout or command completion
        4. Stream ends, _session_active cleared, back to step 1

        This ensures each command session gets a fresh stream and prevents
        Riva idle timeout errors from long periods between wakewords.
        """
        while not self._stop.is_set():
            try:
                # Wait for session to be activated (by wakeword/reset call)
                LOG.debug("Riva: Waiting for session activation...")
                while not self._session_active.is_set() and not self._stop.is_set():
                    self._session_active.wait(timeout=1.0)

                if self._stop.is_set():
                    break

                # Session activated - start streaming with fresh START flag
                LOG.debug("Riva: Session activated, starting stream")
                self._stream_session()

                # Session ended - clear state and wait for next activation.
                # Do NOT clear if reset() was already called during wind-down
                # (e.g. free-text mode calls end_session() then reset() before
                # _stream_session fully exits — clearing here would stomp it).
                if not self._session_active.is_set():
                    LOG.debug("Riva: Session ended, waiting for next wakeword")
                else:
                    LOG.debug("Riva: Session ended but reset() already called — keeping active")

            except Exception as e:
                LOG.error(f"Error in Riva streaming thread: {e}")
                self._session_active.clear()
                import time
                time.sleep(0.1)

    def _stream_session(self):
        """Run one streaming recognition session.

        Creates a fresh gRPC stream with START flag, processes audio until
        timeout or completion, then tears down completely.
        """
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
                enable_word_time_offsets=True,
            ),
            interim_results=True,
        )

        try:
            LOG.debug("Riva: Starting new stream with START flag")
            # Start streaming - this creates a new sequence with START flag
            responses = self.asr_service.streaming_response_generator(
                audio_chunks=self._audio_generator(),
                streaming_config=config,
            )

            # Process responses
            for response in responses:
                if self._stop.is_set():
                    LOG.debug("Riva: Stream stopped")
                    break

                # Check if session was ended (end_session() called)
                if not self._session_active.is_set():
                    LOG.debug("Riva: Session ended, closing stream")
                    break

                if not response.results:
                    continue

                # Process each result - Riva sends cumulative transcripts
                for result in response.results:
                    if not result.alternatives:
                        continue

                    transcript = result.alternatives[0].transcript.strip()
                    if not transcript:
                        continue

                    # DEBUG: Log what Riva actually sends
                    stream_type = "FINAL" if result.is_final else "INTERIM"
                    LOG.debug(f"[RIVA RAW {stream_type}] Full transcript: '{transcript}'")

                    if result.is_final:
                        # Final result
                        self.final_text = transcript
                        self.interim_text = ""

                        # Extract word-level timings if available (enabled via enable_word_time_offsets)
                        # In this Riva Python client, start_time/end_time are plain ints in nanoseconds
                        word_timings = None
                        if result.alternatives[0].words:
                            try:
                                word_timings = [
                                    (w.start_time / 1e9, w.end_time / 1e9)
                                    for w in result.alternatives[0].words
                                ]
                            except Exception as _e:
                                LOG.debug(f"Could not extract word timings: {_e}")

                        # Trigger callback with final words (CUMULATIVE list)
                        # word_timings: list of (start_sec, end_sec) parallel to words, or None
                        if self.word_callback:
                            words = transcript.split()
                            self.word_callback(words, is_final=True, word_timings=word_timings)

                    else:
                        # Interim result — no word timings (Riva only provides these on FINAL)
                        self.interim_text = transcript

                        # Trigger callback with interim words (CUMULATIVE list)
                        if self.word_callback:
                            words = transcript.split()
                            self.word_callback(words, is_final=False, word_timings=None)

            LOG.debug("Riva: Streaming session ended")

        except Exception as e:
            if not self._stop.is_set():
                LOG.warning(f"Riva streaming session error: {e}")

    def _audio_generator(self):
        """Generator that yields audio chunks from the queue.

        Yields chunks while session is active. When session ends or times out,
        generator exits to cleanly tear down the stream.
        """
        while not self._stop.is_set() and self._session_active.is_set():
            try:
                # Short timeout to check session status periodically
                # Allows clean shutdown when end_session() is called
                chunk = self.audio_queue.get(block=True, timeout=0.5)
                if chunk is None:  # Sentinel to stop
                    break
                yield chunk
            except queue.Empty:
                # No audio right now, loop and check session status
                continue

    def feed_audio(self, audio_chunk):
        """Feed audio chunk to Riva for recognition.

        Args:
            audio_chunk: bytes, PCM audio data
        """
        if not self._stop.is_set() and self._session_active.is_set():
            self.audio_queue.put(audio_chunk)

    def reset(self):
        """Start a new recognition session (called after wakeword detection).

        Clears any previous state and activates streaming session.
        """
        # Clear audio queue from any previous session
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

        # Clear transcription state
        self.interim_text = ""
        self.final_text = ""

        # Activate session - this will cause run() to start a fresh stream
        self._session_active.set()
        LOG.debug("Riva: Session reset, stream will start")

    def end_session(self):
        """End the current recognition session (called after command completion/timeout).

        This cleanly tears down the Riva stream. Next reset() will start fresh.
        """
        LOG.debug("Riva: Ending session")
        self._session_active.clear()
        # Generator will exit on next timeout check, closing the stream

    def stop(self):
        """Stop the streaming thread completely."""
        self._stop.set()
        self._session_active.clear()
        self.audio_queue.put(None)  # Sentinel to unblock generator
