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
from mycroft.client.realtime.riva_streaming import RivaStreamingThread, RIVA_AVAILABLE
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

        # STT backend selection
        self.stt_backend = self.realtime_config.get('stt_backend', 'whisper')

        # Whisper for streaming (using whisper_streaming for word-by-word)
        self.whisper_stream_thread = None
        self.whisper_model_path = self.realtime_config.get('whisper_model')
        self.whisper_device = self.realtime_config.get('whisper_device', 'cuda')
        self.whisper_compute_type = self.realtime_config.get('whisper_compute_type', 'float16')
        self.whisper_backend = None

        # Riva for streaming
        self.riva_stream_thread = None
        self.riva_server_uri = self.realtime_config.get('riva', {}).get('server_uri')
        self.riva_model_name = self.realtime_config.get('riva', {}).get('model_name')

        # Trigger words
        self.trigger_words = self.realtime_config.get('trigger_words', [])

        # Session management
        self.session_timeout = self.realtime_config.get('session_timeout_seconds', 15)
        self.audio_buffer = deque(maxlen=16000 * 30)  # 30 seconds
        self.session_active = False
        self.last_word_time = None

        # Word-by-word tracking
        self.previous_partial_text = ""

        # Track Riva word count through the cumulative transcript
        # Riva sends: interim ["turn"] -> interim ["turn","off"] -> final ["turn","off"]
        # We track how many words we've processed to extract only NEW words
        self.riva_word_count = 0

        # Deduplication: track last executed utterance to prevent re-execution
        # when Riva repeatedly sends the same transcript
        self.last_executed_utterance = None
        self.last_executed_time = 0

        # Debug mode
        self.debug = self.realtime_config.get('debug', True)

        # Command matcher for streaming intent matching
        # Single matcher handles both interim and final (like test script)
        # Interim results can trigger early execution if pattern completes
        # Final results are just higher-confidence versions of same transcript
        filler_config = self.realtime_config.get('filler_words', {})
        self.command_matcher = StreamingCommandMatcher(filler_config)

        # Pattern list that gets populated when skills load
        self.shared_patterns = []
        self.command_matcher.patterns = self.shared_patterns

        # Audio batching buffer - accumulate 64ms chunks into 0.5s batches
        # This matches test script's feeding rate (2 chunks/sec not 15 chunks/sec)
        self.whisper_chunk_buffer = []
        self.whisper_chunk_size = 8000  # 0.5s at 16kHz = same as test script

        # Timing diagnostics
        self.last_batch_time = None

        LOG.info("RealtimeRecognizerLoop initialized")
        LOG.info(f"  Trigger words: {self.trigger_words}")
        LOG.info(f"  Session timeout: {self.session_timeout}s")

        # Load STT backend
        if self.stt_backend == 'riva':
            self._load_riva_streaming()
        else:
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

    def _load_riva_streaming(self):
        """Load Riva streaming for word-by-word transcription."""
        if not RIVA_AVAILABLE:
            LOG.error("Riva backend selected but nvidia-riva-client not installed!")
            raise ImportError("nvidia-riva-client is required for Riva STT")

        if not self.riva_server_uri or not self.riva_model_name:
            LOG.error("Riva backend selected but server_uri or model_name not configured!")
            raise ValueError("Must configure realtime.riva.server_uri and realtime.riva.model_name")

        sample_rate = self.config.get('sample_rate', 16000)

        LOG.info(f"Loading Riva streaming: {self.riva_model_name} @ {self.riva_server_uri}")

        # Create and start Riva streaming thread
        self.riva_stream_thread = RivaStreamingThread(
            server_uri=self.riva_server_uri,
            model_name=self.riva_model_name,
            sample_rate=sample_rate,
            word_callback=self._on_words_recognized  # Handle both interim and final
        )
        self.riva_stream_thread.start()

        LOG.info("Riva streaming thread started")

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

        # Reset STT backend for new session
        if self.stt_backend == 'riva' and self.riva_stream_thread:
            self.riva_stream_thread.reset()
            self.riva_word_count = 0
        elif self.whisper_stream_thread:
            self.whisper_stream_thread.reset()

        # Audio buffering
        self.audio_buffer.clear()

        # Reset command matcher for new session (full reset including budget)
        self.command_matcher.reset_session()

        if stream:
            stream.stream_start()

        # Process wake word frames first if available
        if ww_frames:
            for chunk in ww_frames:
                chunk_float = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                for sample in chunk_float:
                    self.audio_buffer.append(sample)
                self._process_audio_chunk(chunk)

        # Continue streaming until timeout
        while self.session_active:
            # Check timeout
            if time.time() - self.last_word_time > self.session_timeout:
                LOG.info("Session timeout - ending")
                break

            # Get audio chunk
            chunk = self.responsive_recognizer.record_sound_chunk(source)

            # Buffer audio for potential fallback processing
            chunk_float = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            for sample in chunk_float:
                self.audio_buffer.append(sample)

            if stream:
                stream.stream_chunk(chunk)

            # Feed to STT backend
            self._process_audio_chunk(chunk)

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

    def _on_words_recognized(self, words, is_final):
        """Callback when Riva recognizes words (interim or final).

        Riva sends TWO SEPARATE STREAMS:
        1. Interim stream: Multiple cumulative updates as speech is recognized
        2. Final stream: One final cumulative result when recognition is complete

        We maintain two parallel command matchers, one for each stream.
        When EITHER matcher completes a command, we execute and clear BOTH.

        Args:
            words: List of recognized words (ENTIRE transcript so far, cumulative!)
            is_final: True if final result, False if interim
        """
        now = time.time()

        stream_name = "FINAL" if is_final else "INTERIM"
        current_count = self.riva_word_count
        matcher = self.command_matcher

        # Detect if Riva reset its stream (word count > current transcript length)
        # This happens when a new utterance starts or Riva completely changed the transcription
        if current_count > len(words):
            LOG.debug(f"Riva stream reset detected (count={current_count} > len={len(words)}), resetting")
            matcher.reset()
            current_count = 0
            self.riva_word_count = 0

        # Change detection - check if Riva changed words we already matched
        # This prevents building incorrect word lists when Riva changes its mind
        if len(matcher.matched_words) > 0:
            num_matched = len(matcher.matched_words)
            if num_matched <= len(words):
                riva_prefix = words[:num_matched]
                if riva_prefix != matcher.matched_words:
                    # Riva changed words we already committed! Reset and reprocess
                    LOG.debug(f"Riva changed matched words: {matcher.matched_words} -> {riva_prefix}, resetting")
                    matcher.reset()
                    current_count = 0
                    self.riva_word_count = 0

        # Extract only NEW words (beyond what we've already processed)
        new_words = words[current_count:]

        if new_words:
            LOG.debug(f"[RIVA {stream_name}] New words: {new_words} (processed {current_count}/{len(words)})")

        # Process NEW words one at a time through the matcher
        for word in new_words:
            # Update word count as we process (mark as seen before processing)
            current_count += 1
            self.riva_word_count = current_count

            LOG.info(f"[RIVA {stream_name}] {word}")

            # Update last word time
            self.last_word_time = now

            # Emit for CLI display
            if self.debug:
                event_name = 'mycroft.debug.riva.final' if is_final else 'mycroft.debug.riva.partial'
                self.emit(event_name, {'utterance': word})

            # Check for command match in this stream's matcher
            match = matcher.add_word(word)

            if match:
                # Complete command matched!
                utterance = match['utterance']
                LOG.info(f"✓ COMMAND MATCHED ({stream_name}): '{utterance}' (intent: {match['intent']})")

                # Deduplication: Check if we just executed this exact utterance
                # Riva may repeat the same transcript multiple times, causing re-execution
                if utterance == self.last_executed_utterance and (now - self.last_executed_time) < 2.0:
                    LOG.debug(f"Skipping duplicate execution of '{utterance}' (executed {now - self.last_executed_time:.1f}s ago)")
                    continue

                # Emit for skills processing
                self.emit('recognizer_loop:utterance', {
                    'utterances': [utterance],
                    'lang': self.lang
                })

                # Emit for CLI display
                if self.debug:
                    self.emit('mycroft.debug.riva.matched', {
                        'utterance': utterance
                    })

                # Reset matcher - command executed, start fresh for next command in session
                self.command_matcher.reset()

                # Mark this utterance as executed to prevent re-execution if Riva repeats it
                self.last_executed_utterance = utterance
                self.last_executed_time = now

                LOG.debug(f"Matcher cleared after command execution ({stream_name})")

                # IMPORTANT: Break out of word processing loop after command execution
                # Riva may continue sending the same transcript repeatedly, and we don't want
                # to re-execute the same command. We'll process new words in the next callback.
                break

        # Check if we should timeout due to too many filler words
        if self.command_matcher.should_timeout():
            LOG.info(f"Too many consecutive filler words - ending session")
            self.session_active = False

    def _process_audio_chunk(self, audio_chunk):
        """Route audio chunk to appropriate STT backend."""
        if self.stt_backend == 'riva':
            self._process_riva_chunk(audio_chunk)
        else:
            self._process_whisper_chunk(audio_chunk)

    def _process_riva_chunk(self, audio_chunk):
        """Feed audio chunk directly to Riva streaming.

        Riva handles its own buffering and streaming, so we just feed chunks as they come.
        """
        if self.riva_stream_thread:
            self.riva_stream_thread.feed_audio(audio_chunk)

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
