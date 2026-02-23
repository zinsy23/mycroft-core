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

        # STT backend selection - Riva is primary
        self.stt_backend = self.realtime_config.get('stt_backend', 'riva')

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

        # Track Riva word counts for BOTH streams separately
        # Riva sends TWO cumulative streams:
        # - Interim: ["turn"] -> ["turn","lights"] -> ["turn","like"] -> ["turn","lights","on"]
        # - Final: ["turn","lights","on"]
        # Each stream tracks separately where it left off
        self.riva_interim_word_count = 0
        self.riva_final_word_count = 0

        # Track previous transcripts to detect when Riva changes its mind
        # Example: 'h' (1 word) -> 'turn' (1 word) - count stays same but words changed!
        self.prev_interim_transcript = ""
        self.prev_final_transcript = ""

        # Deduplication: track recent executed utterances to prevent re-execution
        # Uses a short history to handle interleaved commands (e.g., INTERIM "pause"
        # then INTERIM "dim lights" then delayed FINAL "pause")
        # Format: [(utterance, timestamp), ...]
        self.executed_utterances_history = []
        self.dedup_window_seconds = 3.0  # Check duplicates within 3 second window (FINALs can be delayed 2+ seconds)
        self.dedup_history_size = 5  # Keep last 5 executed commands

        # Debug mode
        self.debug = self.realtime_config.get('debug', True)

        # DUAL command matchers for Riva's two streams (interim and final)
        # Both share global budget via coordination, like CDN edge nodes
        # Interim can be more accurate than final, so both compete
        # When EITHER completes a command -> execute and reset BOTH
        filler_config = self.realtime_config.get('filler_words', {})
        self.interim_matcher = StreamingCommandMatcher(filler_config)
        self.final_matcher = StreamingCommandMatcher(filler_config)

        # Global budget coordination - shared between both matchers
        # Each matcher's global_budget syncs to/from this shared value
        # When a path wins, it syncs back to this shared budget
        # Read from config: filler_words.base_max (default 4)
        self.shared_global_budget = filler_config.get('base_max', 4)

        # Pattern list that gets populated when skills load
        # Both matchers share the same pattern list
        self.shared_patterns = []
        self.interim_matcher.patterns = self.shared_patterns
        self.final_matcher.patterns = self.shared_patterns

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
            self.riva_interim_word_count = 0
            self.riva_final_word_count = 0
        elif self.whisper_stream_thread:
            self.whisper_stream_thread.reset()

        # Audio buffering
        self.audio_buffer.clear()

        # Reset BOTH matchers for new session (full reset including budget)
        # This resets the shared global budget to base_max_fillers
        self.interim_matcher.reset_session()
        self.final_matcher.reset_session()
        # Sync shared budget from one of the matchers (they're identical after reset)
        self.shared_global_budget = self.interim_matcher.global_budget

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
                self.session_active = False
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

        # End the Riva stream cleanly (if using Riva)
        if self.stt_backend == 'riva' and self.riva_stream_thread:
            self.riva_stream_thread.end_session()

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

        # Check for command match (Whisper only uses interim_matcher)
        match = self.interim_matcher.add_word(word)

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
            self.interim_matcher.reset()
            self.shared_global_budget = self.interim_matcher.global_budget

        # Check if we should timeout (global budget exhausted)
        if self.interim_matcher.global_budget <= 0:
            LOG.info(f"Whisper matcher budget exhausted (budget: {self.interim_matcher.global_budget}) - ending session")
            self.session_active = False
            # Whisper doesn't have persistent stream, so no end_session() needed

    def _on_words_recognized(self, words, is_final):
        """Callback when Riva recognizes words (interim or final).

        Based on test script's tried-and-true logic for handling Riva streams.

        Riva sends TWO SEPARATE CUMULATIVE STREAMS:
        1. Interim stream: ["turn"] → ["turn","lights"] → ["turn","like"] → ["turn","lights","on"]
        2. Final stream: ["turn","lights","on"]

        Each stream is independent and can change words. Sometimes interim is more accurate than final!

        We maintain TWO parallel evolutionary matchers with SHARED global budget:
        - interim_matcher: Processes interim stream
        - final_matcher: Processes final stream
        - Both share global_budget (CDN-style coordination)
        - When EITHER completes a command → execute and reset BOTH

        Args:
            words: List of recognized words (ENTIRE transcript so far, cumulative!)
            is_final: True if final result, False if interim
        """
        now = time.time()
        stream_name = "FINAL" if is_final else "INTERIM"

        # Select the appropriate matcher and word count for this stream
        if is_final:
            matcher = self.final_matcher
            current_count = self.riva_final_word_count
        else:
            matcher = self.interim_matcher
            current_count = self.riva_interim_word_count

        # BUDGET COORDINATION: Sync matcher's global_budget from shared budget before processing
        # This ensures both matchers see the same budget (CDN-style coordination)
        matcher.global_budget = self.shared_global_budget

        # Get the full transcript as a string for change detection
        transcript = ' '.join(words)

        # Detect if Riva reset its stream (word count > current transcript length)
        # This happens when Riva sends a new segment after a pause
        # Example: ["set","color"] (pause) → ["blue"]
        # We DON'T reset the matcher - just process new words as continuation
        # The filler budget system will handle garbage, and this allows
        # multi-part commands like "set color ... blue"
        if current_count > len(words):
            LOG.debug(f"[RIVA {stream_name}] New segment detected (count={current_count} > len={len(words)}), continuing with matcher state")
            # Just reset word count to process from beginning of new segment
            # Matcher state is preserved so "set color [pause] blue" still works
            current_count = 0
            if is_final:
                self.riva_final_word_count = 0
                self.prev_final_transcript = ""
            else:
                self.riva_interim_word_count = 0
                self.prev_interim_transcript = ""
            # Budget is NOT reset - continues from current value
        elif current_count > 0:
            # Additional check: Detect if Riva changed its mind about previous words
            # Example: 'h' (1 word) -> 'turn' (1 word) - count stays 1 but words changed!
            # This fixes the bug where we miss words when Riva corrects itself
            prev_transcript = self.prev_final_transcript if is_final else self.prev_interim_transcript
            prev_words = prev_transcript.split()[:current_count]
            curr_words = words[:current_count]

            if prev_words != curr_words:
                LOG.debug(f"[RIVA {stream_name}] Transcript changed: {prev_words} → {curr_words}")
                LOG.debug(f"[RIVA {stream_name}] Resetting matcher and reprocessing from start")
                matcher.reset()
                current_count = 0
                if is_final:
                    self.riva_final_word_count = 0
                    self.prev_final_transcript = ""
                else:
                    self.riva_interim_word_count = 0
                    self.prev_interim_transcript = ""

        # Extract only NEW words beyond what we've already processed
        # Example: processed ["turn"], new transcript ["turn","lights"] → new_words = ["lights"]
        new_words = words[current_count:]

        if new_words:
            LOG.debug(f"[RIVA {stream_name}] New words: {new_words} (processed {current_count}/{len(words)})")

        # Update previous transcript for next comparison
        if is_final:
            self.prev_final_transcript = transcript
        else:
            self.prev_interim_transcript = transcript

        # Process NEW words one at a time through THIS stream's matcher
        # Multi-path evolutionary matcher handles word changes via path competition
        for word in new_words:
            # Mark as seen BEFORE processing (in case of errors/breaks)
            current_count += 1
            if is_final:
                self.riva_final_word_count = current_count
            else:
                self.riva_interim_word_count = current_count

            LOG.info(f"[RIVA {stream_name}] {word}")

            # Update last word time
            self.last_word_time = now

            # Emit for CLI display
            if self.debug:
                event_name = 'mycroft.debug.riva.final' if is_final else 'mycroft.debug.riva.partial'
                self.emit(event_name, {'utterance': word})

            # Add word to THIS stream's matcher
            # Only creates paths for valid next words (or valid first words)
            # Returns match if command completes
            match = matcher.add_word(word)

            if match:
                # Complete command matched in THIS stream!
                utterance = match['utterance']
                LOG.info(f"✓ COMMAND MATCHED ({stream_name}): '{utterance}' (intent: {match['intent']})")

                # Deduplication: Check if we recently executed this utterance
                # Checks against recent history to handle interleaved commands
                # Example: INTERIM "pause" → INTERIM "dim lights" → delayed FINAL "pause"
                is_duplicate = False
                for prev_utterance, prev_time in self.executed_utterances_history:
                    if utterance == prev_utterance and (now - prev_time) < self.dedup_window_seconds:
                        LOG.info(f"⏭️  Skipping duplicate: '{utterance}' (executed {now - prev_time:.1f}s ago)")
                        is_duplicate = True
                        break

                if is_duplicate:
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

                # BUDGET COORDINATION: Sync shared budget from winning matcher's budget
                # The winning path's local_budget already synced to matcher.global_budget
                self.shared_global_budget = matcher.global_budget
                LOG.debug(f"Global budget synced: {self.shared_global_budget}")

                # Reset BOTH matchers - command executed, start fresh
                # Both matchers will inherit the updated shared_global_budget on next word
                self.interim_matcher.reset()
                self.final_matcher.reset()

                # Mark executed to prevent re-execution
                # Add to history and maintain size limit
                self.executed_utterances_history.append((utterance, now))
                if len(self.executed_utterances_history) > self.dedup_history_size:
                    self.executed_utterances_history.pop(0)  # Remove oldest

                LOG.debug(f"Both matchers reset after {stream_name} match")

                # Break out of word loop after execution
                # Next callback will process any new words
                break

        # BUDGET COORDINATION: After processing all words, sync shared budget back
        # The matcher we just used has the authoritative budget (fillers decrease it)
        # This is the "CDN edge node reporting back to origin" step
        self.shared_global_budget = matcher.global_budget

        # Check if we should timeout (shared budget exhausted)
        if self.shared_global_budget <= 0:
            LOG.info(f"Global budget exhausted (budget: {self.shared_global_budget}) - ending session")
            self.session_active = False
            if self.stt_backend == 'riva' and self.riva_stream_thread:
                self.riva_stream_thread.end_session()

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
