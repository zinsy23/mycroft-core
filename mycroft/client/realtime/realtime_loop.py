"""
Realtime Recognizer Loop - extends the voice service RecognizerLoop.

Only overrides the STT behavior to use Whisper streaming for word-by-word transcription.
Wake word detection, microphone handling, and bus integration remain unchanged.
"""

import json
import time
from collections import deque

import numpy as np

from mycroft.client.realtime.listener import RecognizerLoop
from mycroft.client.realtime.mic import ResponsiveRecognizer
from mycroft.client.realtime.command_matcher import StreamingCommandMatcher
from mycroft.client.realtime.whisper_streaming_wrapper import WhisperStreamingThread
from mycroft.configuration import Configuration
from mycroft.util.log import LOG


class RealtimeResponsiveRecognizer(ResponsiveRecognizer):
    """Extends ResponsiveRecognizer to use streaming instead of recording."""

    def __init__(self, wake_word_recognizer, watchdog, realtime_loop):
        super().__init__(wake_word_recognizer, watchdog)
        self.realtime_loop = realtime_loop

    def _record_phrase(self, source, sec_per_buffer, stream=None, ww_frames=None):
        """Override to use realtime streaming instead of recording."""
        # Call the realtime loop's streaming method
        return self.realtime_loop._stream_realtime_phrase(
            source, sec_per_buffer, stream, ww_frames
        )


class RealtimeRecognizerLoop(RecognizerLoop):
    """Extends RecognizerLoop to use Whisper streaming for realtime intent matching."""

    def __init__(self, watchdog=None):
        LOG.info("RealtimeRecognizerLoop.__init__ starting")
        super().__init__(watchdog)
        LOG.info("RealtimeRecognizerLoop parent init complete")

        # Load realtime-specific config
        self.realtime_config = Configuration.get().get('realtime', {})

        # Whisper for streaming (using whisper_streaming for word-by-word)
        self.whisper_stream_thread = None

        # Whisper for accuracy
        self.whisper_model_path = self.realtime_config.get('whisper_model')
        self.whisper_device = self.realtime_config.get('whisper_device', 'cuda')
        self.whisper_compute_type = self.realtime_config.get('whisper_compute_type', 'float16')
        self.whisper_backend = None

        # Trigger words
        self.trigger_words = self.realtime_config.get('trigger_words', [])

        # Session management
        self.session_timeout = self.realtime_config.get('session_timeout_seconds', 15)
        self.audio_buffer = deque(maxlen=16000 * 30)  # 30 seconds
        self.session_active = False
        self.last_word_time = None

        # Word-by-word tracking
        self.previous_partial_text = ""

        # Debug mode
        self.debug = self.realtime_config.get('debug', True)

        # Command matcher for streaming intent matching
        filler_config = self.realtime_config.get('filler_words', {})
        self.command_matcher = StreamingCommandMatcher(filler_config)

        # Audio batching buffer - accumulate 64ms chunks into 0.5s batches
        # This matches test script's feeding rate (2 chunks/sec not 15 chunks/sec)
        self.whisper_chunk_buffer = []
        self.whisper_chunk_size = 8000  # 0.5s at 16kHz = same as test script

        # Timing diagnostics
        self.last_batch_time = None

        LOG.info("RealtimeRecognizerLoop initialized")
        LOG.info(f"  Trigger words: {self.trigger_words}")
        LOG.info(f"  Session timeout: {self.session_timeout}s")

        # Load Whisper streaming model
        self._load_whisper_streaming()

        # Don't load intent patterns here - they will be sent by skills service
        # via messagebus when available (see __main__.py handle_intents_ready)
        LOG.info("Intent patterns will be received from skills service via messagebus")

        # Replace responsive_recognizer with our custom one
        # This must happen AFTER super().__init__() which creates the original
        LOG.info("Creating RealtimeResponsiveRecognizer")
        self.responsive_recognizer = RealtimeResponsiveRecognizer(
            self.wakeword_recognizer,
            self._watchdog,
            self
        )
        LOG.info("RealtimeRecognizerLoop.__init__ complete")

    def _load_whisper_streaming(self):
        """Load Whisper streaming model for word-by-word transcription."""
        sample_rate = self.config.get('sample_rate', 16000)
        chunk_seconds = self.realtime_config.get('whisper_chunk_seconds', 0.5)  # Balanced chunk size
        window_seconds = self.realtime_config.get('whisper_window_seconds', 3.0)  # Balanced window for accuracy and speed

        LOG.info(f"Loading Whisper streaming: {self.whisper_model_path}")

        # Create and start the streaming thread with callback for event-driven word processing
        self.whisper_stream_thread = WhisperStreamingThread(
            model_path=self.whisper_model_path,
            device=self.whisper_device,
            compute_type=self.whisper_compute_type,
            sample_rate=sample_rate,
            chunk_seconds=chunk_seconds,
            window_seconds=window_seconds,
            word_callback=self._on_word_recognized  # Event-driven like test script
        )
        self.whisper_stream_thread.start()

        LOG.info("Whisper streaming thread started")

    def _stream_realtime_phrase(self, source, sec_per_buffer, stream=None, ww_frames=None):
        """Stream and process audio in realtime with Whisper word-by-word.

        Replaces the _record_phrase method to process chunks immediately
        instead of recording first then transcribing.

        Args:
            source: AudioSource producing audio chunks
            sec_per_buffer: Fractional seconds per chunk
            stream: AudioStreamHandler for streaming chunks
            ww_frames: Deque of wake word audio frames

        Returns:
            str: Final transcribed text
        """
        LOG.info("🎤 Starting realtime streaming session")

        self.session_active = True
        self.last_word_time = time.time()
        self.previous_partial_text = ""

        # Reset for new session (but Whisper ASRProcessor keeps its internal context)
        self.whisper_stream_thread.reset()

        # Audio buffering for Whisper
        self.audio_buffer.clear()

        # Reset command matcher for new session
        self.command_matcher.reset()

        if stream:
            stream.stream_start()

        # Process wake word frames first if available
        if ww_frames:
            for chunk in ww_frames:
                chunk_float = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                for sample in chunk_float:
                    self.audio_buffer.append(sample)
                self._process_whisper_chunk(chunk)

        # Continue streaming until timeout
        while self.session_active:
            # Check timeout
            if time.time() - self.last_word_time > self.session_timeout:
                LOG.info("Session timeout - ending")
                break

            # Get audio chunk
            chunk = self.responsive_recognizer.record_sound_chunk(source)

            # Buffer for Whisper
            chunk_float = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            for sample in chunk_float:
                self.audio_buffer.append(sample)

            if stream:
                stream.stream_chunk(chunk)

            # Always feed to Whisper - let VAD handle silence detection
            # (energy-based filtering was too aggressive and missed speech)
            self._process_whisper_chunk(chunk)

        self.session_active = False
        LOG.info("Session ended")

        if stream:
            stream.stream_stop()

        # Return dummy audio bytes (not used since we already emitted utterance)
        # The parent code expects audio bytes but we've already processed everything
        return b''

    def _on_word_recognized(self, word):
        """Callback when Whisper recognizes a word.

        This is event-driven - called by WordCollector._do_output() when ASRProcessor
        outputs a word. Matches the flow of PrintSender in test_live_stt2.py.
        """
        now = time.time()

        # Calculate latency from last batch (approximate)
        if self.last_batch_time:
            latency = now - self.last_batch_time
            LOG.info(f"[WHISPER] {word} (latency: {latency*1000:.0f}ms from last batch)")
        else:
            LOG.info(f"[WHISPER] {word}")

        # Update last word time
        self.last_word_time = now

        # Emit for CLI display
        if self.debug:
            self.emit('mycroft.debug.whisper.partial', {
                'utterance': word
            })

        # Check for command match
        match = self.command_matcher.add_word(word)

        if match:
            # Complete command matched!
            utterance = match['utterance']
            LOG.info(f"✓ COMMAND MATCHED: '{utterance}' (intent: {match['intent']})")

            # Emit for skills processing
            self.emit('recognizer_loop:utterance', {
                'utterances': [utterance],
                'lang': self.lang
            })

            # Emit for CLI display
            if self.debug:
                self.emit('mycroft.debug.whisper.final', {
                    'utterance': utterance
                })

            # Reset command matcher after successful match to prevent re-triggering
            self.command_matcher.reset()

        # Check if we should timeout due to too many filler words
        if self.command_matcher.should_timeout():
            LOG.info(f"Too many consecutive filler words ({self.command_matcher.consecutive_fillers}) - ending session")
            self.session_active = False

    def _process_whisper_chunk(self, audio_chunk):
        """Batch 64ms chunks into 0.5s chunks before feeding to Whisper.

        This matches the test script's feeding rate: 2 chunks/second instead of 15 chunks/second.
        Prevents queue buildup and matches Whisper's natural processing rate.
        """
        # Convert to float32 for whisper_streaming
        chunk_float = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32) / 32768.0

        # Accumulate samples in buffer
        self.whisper_chunk_buffer.extend(chunk_float)

        # When we have 0.5s worth (8000 samples), feed to Whisper
        if len(self.whisper_chunk_buffer) >= self.whisper_chunk_size:
            # Extract exactly 0.5s worth
            batch = np.array(self.whisper_chunk_buffer[:self.whisper_chunk_size], dtype=np.float32)
            self.whisper_chunk_buffer = self.whisper_chunk_buffer[self.whisper_chunk_size:]

            # Monitor queue depth BEFORE putting
            queue_size = self.whisper_stream_thread.queue.qsize()

            # Track time between batches (should be ~0.5s)
            now = time.time()
            if self.last_batch_time:
                batch_interval = now - self.last_batch_time
                LOG.debug(f"[TIMING] Batch interval: {batch_interval*1000:.0f}ms (target: 500ms)")
            self.last_batch_time = now

            # Feed complete 0.5s batch to Whisper (matches test script rate!)
            self.whisper_stream_thread.queue.put(batch)

            # Log queue status (info level so we can always see it)
            if queue_size > 0:
                LOG.warning(f"⚠️ [QUEUE] Size: {queue_size} batches ({queue_size * 0.5:.1f}s audio behind)")
            else:
                LOG.info(f"✓ [QUEUE] Size: 0 (keeping up)")

    def _process_with_whisper(self):
        """Process buffered audio with Whisper for accurate transcription."""
        if len(self.audio_buffer) < 16000 * 0.3:  # At least 0.3s
            LOG.warning("Audio buffer too short for Whisper")
            return None

        buffer_array = np.array(list(self.audio_buffer), dtype=np.float32)

        try:
            segments, _ = self.whisper_backend.transcribe(buffer_array, "")
            words_list = self.whisper_backend.segments_to_words(list(segments))
            whisper_text = " ".join([w.word.strip() for w in words_list])

            LOG.info(f"✓ WHISPER result: {whisper_text}")
            return whisper_text

        except Exception as e:
            LOG.error(f"Whisper processing failed: {e}")
            return None

    def start_async(self):
        """Override to add logging."""
        LOG.info("RealtimeRecognizerLoop.start_async() called")
        try:
            super().start_async()
            LOG.info("start_async() completed successfully")
        except Exception as e:
            LOG.error(f"Error in start_async(): {e}")
            import traceback
            LOG.error(traceback.format_exc())
            raise

    def run(self):
        """Override to add logging."""
        LOG.info("RealtimeRecognizerLoop.run() called - starting audio loop")
        LOG.info(f"  state.running: {self.state.running}")
        LOG.info(f"  responsive_recognizer: {self.responsive_recognizer}")
        try:
            LOG.info("Calling parent run()")
            super().run()
            LOG.info("Parent run() returned")
        except Exception as e:
            LOG.error(f"Error in run(): {e}")
            import traceback
            LOG.error(traceback.format_exc())

    # Override the transcribe method to use Whisper streaming
    def transcribe(self, audio):
        """Override to use Whisper streaming instead of traditional STT.

        This is called after wake word detection with the recorded audio.
        Instead of sending to cloud STT, we use Whisper streaming for word-by-word transcription.
        """
        LOG.debug("Realtime transcribe called - using streaming instead")

        # We don't use the recorded audio - we stream in realtime instead
        # This will be handled by overriding _record_phrase
        return None
