"""
Realtime Recognizer Loop - extends the voice service RecognizerLoop.

Only overrides the STT behavior to use Riva conformer for streaming word-by-word
command matching. Supports three operating modes:

  DETERMINISTIC — normal streaming command matching via conformer + dual matchers
  FREE_TEXT     — triggered by {free_text} entity prefix match; conformer monitors
                  for stop, Whisper batch-transcribes the recorded audio
  REPEAT        — triggered by quantifier words; replays last executed command N-1
                  additional times without re-transcribing
"""

import time
from collections import deque

import numpy as np

from mycroft.client.realtime.listener import RecognizerLoop
from mycroft.client.realtime.mic import ResponsiveRecognizer
from mycroft.client.realtime.command_matcher import StreamingCommandMatcher
from mycroft.client.realtime.whisper_streaming_wrapper import WhisperStreamingThread
from mycroft.client.realtime.riva_streaming import RivaStreamingThread, RIVA_AVAILABLE
from mycroft.client.realtime.free_text_stt import load_free_text_stt
from mycroft.client.realtime.pattern_loader import FREE_TEXT_TAG, REPEAT_TAG
from mycroft.configuration import Configuration
from mycroft.util.log import LOG

# Non-terminal words: structural words that can't end a query.
# Silence after these gets the full session timeout rather than stop_silence_seconds.
# Configurable via free_text.non_terminal_words_extend / non_terminal_words_suppress.
_DEFAULT_NON_TERMINAL_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'of', 'to', 'for', 'at', 'in', 'on',
    'with', 'from', 'into', 'by', 'about', 'like'
}

# Operating modes
MODE_DETERMINISTIC = 'deterministic'
MODE_FREE_TEXT = 'free_text'
MODE_REPEAT = 'repeat'


class RealtimeResponsiveRecognizer(ResponsiveRecognizer):
    """Extends ResponsiveRecognizer to use streaming instead of recording."""

    def __init__(self, wake_word_recognizer, watchdog, realtime_loop):
        super().__init__(wake_word_recognizer, watchdog)
        self.realtime_loop = realtime_loop

    def _record_phrase(self, source, sec_per_buffer, stream=None, ww_frames=None):
        """Override to use realtime streaming instead of recording."""
        return self.realtime_loop._stream_realtime_phrase(
            source, sec_per_buffer, stream, ww_frames
        )


class RealtimeRecognizerLoop(RecognizerLoop):
    """Extends RecognizerLoop to use conformer streaming for realtime intent matching.

    Three modes:
      DETERMINISTIC: conformer word-by-word path matching (always active at session start)
      FREE_TEXT:     entered when a __FREE_TEXT__:intent prefix is matched; Whisper
                     batch-transcribes the free-text portion
      REPEAT:        entered when a __REPEAT__:N intent is matched; replays last command
    """

    def __init__(self, watchdog=None):
        LOG.info("RealtimeRecognizerLoop.__init__ starting")
        super().__init__(watchdog)
        LOG.info("RealtimeRecognizerLoop parent init complete")

        # Load realtime-specific config
        self.realtime_config = Configuration.get().get('realtime', {})

        # STT backend selection - Riva is primary
        self.stt_backend = self.realtime_config.get('stt_backend', 'riva')

        # Riva for streaming
        self.riva_stream_thread = None
        self.riva_server_uri = self.realtime_config.get('riva', {}).get('server_uri')
        self.riva_model_name = self.realtime_config.get('riva', {}).get('model_name')

        # Whisper streaming wrapper (legacy whisper-streaming path, not used by default)
        self.whisper_stream_thread = None

        # Free-text secondary STT (batch Whisper for nondeterministic commands)
        self.free_text_stt = load_free_text_stt(self.realtime_config)

        # Free-text config
        _ft_cfg = self.realtime_config.get('free_text', {})
        self.free_text_entity_name = _ft_cfg.get('entity_name', 'free_text')
        self.free_text_stop_silence = _ft_cfg.get('stop_silence_seconds', 1.0)
        # null → inherit session_timeout (set after session_timeout is loaded)
        self._ft_stop_nonterminal_cfg = _ft_cfg.get('stop_silence_non_terminal_seconds')
        self._ft_stop_filler_cfg = _ft_cfg.get('stop_silence_filler_seconds')
        self._ft_hard_timeout_cfg = _ft_cfg.get('hard_timeout_seconds')
        # VAD debounce: consecutive loud chunks required before treating as speech again.
        # Uses two thresholds: start (speech→silence transition) and resume (resets stop timer).
        # Resume threshold is higher to avoid noise spikes resetting the timer.
        self.free_text_vad_min_loud_chunks = _ft_cfg.get('vad_min_loud_chunks', 3)
        self.free_text_vad_min_resume_chunks = _ft_cfg.get('vad_min_resume_chunks', 8)
        # Cancel words (multi-word phrases checked against last N FINAL words)
        self.free_text_cancel_words = [
            w.lower() for w in _ft_cfg.get('cancel_words',
                ['cancel', 'never mind', 'belay', 'belay that', 'belay that order'])
        ]
        # Non-terminal word set (built from defaults + extend - suppress)
        _extend = set(_ft_cfg.get('non_terminal_words_extend', []))
        _suppress = set(_ft_cfg.get('non_terminal_words_suppress', []))
        self.non_terminal_words = (_DEFAULT_NON_TERMINAL_WORDS | _extend) - _suppress

        # Filler words (same list used by budget system)
        _filler_cfg = self.realtime_config.get('filler_words', {})
        self.filler_words = set(_filler_cfg.get('words', ['uh', 'um', 'hmm', 'ah']))

        # Repeat config
        _rep_cfg = self.realtime_config.get('repeat', {})
        self.repeat_enabled = _rep_cfg.get('enabled', False)
        self.repeat_require_prefix = _rep_cfg.get('require_prefix', False)
        self.repeat_window_seconds = _rep_cfg.get('quantifier_only_window_seconds', 5)
        self.repeat_cross_session = _rep_cfg.get('cross_session_repeat', False)
        self.repeat_history_file = _rep_cfg.get('cross_session_history_file')

        # Session management
        self.session_timeout = self.realtime_config.get('session_timeout_seconds', 15)
        self.audio_buffer = deque(maxlen=16000 * 30)  # 30 seconds of float32
        self.session_active = False
        self.last_word_time = None
        self.session_start_time = None          # Wall time when session started
        self.session_start_buffer_len = 0       # audio_buffer length at session start

        # Resolve null timeouts to session_timeout
        self.free_text_stop_nonterminal = (
            self._ft_stop_nonterminal_cfg
            if self._ft_stop_nonterminal_cfg is not None
            else self.session_timeout
        )
        self.free_text_stop_filler = (
            self._ft_stop_filler_cfg
            if self._ft_stop_filler_cfg is not None
            else self.session_timeout
        )
        self.free_text_hard_timeout = (
            self._ft_hard_timeout_cfg
            if self._ft_hard_timeout_cfg is not None
            else self.session_timeout
        )

        # Word-by-word tracking
        self.previous_partial_text = ""

        # Track Riva word counts for BOTH streams separately
        self.riva_interim_word_count = 0
        self.riva_final_word_count = 0

        # Track previous transcripts to detect when Riva changes its mind
        self.prev_interim_transcript = ""
        self.prev_final_transcript = ""

        # Minimum replay position per stream
        self.interim_min_replay_pos = 0
        self.final_min_replay_pos = 0

        # Deduplication
        self.executed_utterances_history = []
        self.dedup_window_seconds = 3.0
        self.dedup_history_size = 5

        # Debug mode
        self.debug = self.realtime_config.get('debug', True)

        # DUAL command matchers for Riva's two streams
        self.interim_matcher = StreamingCommandMatcher(_filler_cfg)
        self.final_matcher = StreamingCommandMatcher(_filler_cfg)
        self.shared_global_budget = _filler_cfg.get('base_max', 4)

        # Pattern list shared between both matchers
        self.shared_patterns = []
        self.interim_matcher.patterns = self.shared_patterns
        self.final_matcher.patterns = self.shared_patterns

        # Audio batching buffer for whisper streaming (legacy path)
        self.whisper_chunk_buffer = []
        self.whisper_chunk_size = 8000  # 0.5s at 16kHz
        self.last_batch_time = None

        # ── Operating mode ────────────────────────────────────────────────────
        self.mode = MODE_DETERMINISTIC

        # Free-text mode state
        self.free_text_intent = None            # e.g. 'websites.search'
        self.free_text_trigger_utterance = None # deterministic prefix that fired
        self.free_text_audio_start_idx = 0      # index into audio_buffer at trigger
        self.free_text_mode_start_time = None   # wall time when mode was entered
        self.free_text_stop_timer = None        # wall time when silence started
        self.free_text_current_stop_silence = self.free_text_stop_silence
        self.free_text_last_nonfiller_word = None
        self.free_text_vad_speaking = False     # True while audio energy is above threshold
        self.free_text_vad_loud_chunks = 0      # consecutive loud chunks (debounce)
        self.free_text_vad_suppressed = False   # True after non-terminal/filler silence — waiting for new speech
        # Accumulate FINAL words seen in free-text mode for cancel detection
        self.free_text_final_words_seen = []

        # Repeat mode state
        self.last_executed_command = None       # (utterance, intent) of last non-repeat command
        self.last_command_dispatch_time = None  # when last_executed_command was set
        self.repeat_window_open = False         # whether quantifier-only words are active

        # ─────────────────────────────────────────────────────────────────────

        LOG.info("RealtimeRecognizerLoop initialized")
        LOG.info(f"  Session timeout: {self.session_timeout}s")
        LOG.info(f"  Free-text STT: {'enabled' if self.free_text_stt else 'disabled'}")
        LOG.info(f"  Repeat mode: {'enabled' if self.repeat_enabled else 'disabled'}")

        # Load STT backend
        if self.stt_backend == 'riva':
            self._load_riva_streaming()
        else:
            self._load_whisper_streaming()

        LOG.info("Intent patterns will be received from skills service via messagebus")

        # Replace responsive_recognizer with our custom one
        LOG.info("Creating RealtimeResponsiveRecognizer")
        self.responsive_recognizer = RealtimeResponsiveRecognizer(
            self.wakeword_recognizer,
            self._watchdog,
            self
        )
        LOG.info("RealtimeRecognizerLoop.__init__ complete")

    # ── STT backend loading ───────────────────────────────────────────────────

    def _load_whisper_streaming(self):
        """Load Whisper streaming model for word-by-word transcription (legacy path)."""
        ft_cfg = self.realtime_config.get('free_text_stt', {})
        fw_cfg = ft_cfg.get('faster_whisper', {})
        model_path = fw_cfg.get('model')
        device = fw_cfg.get('device', 'cuda')
        compute_type = fw_cfg.get('compute_type', 'float16')
        sample_rate = self.realtime_config.get('sample_rate', 16000)
        chunk_seconds = self.realtime_config.get('whisper_chunk_seconds', 0.5)
        window_seconds = self.realtime_config.get('whisper_window_seconds', 3.0)

        LOG.info(f"Loading Whisper streaming: {model_path}")
        self.whisper_stream_thread = WhisperStreamingThread(
            model_path=model_path,
            device=device,
            compute_type=compute_type,
            sample_rate=sample_rate,
            chunk_seconds=chunk_seconds,
            window_seconds=window_seconds,
            word_callback=self._on_word_recognized
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

        sample_rate = self.realtime_config.get('sample_rate', 16000)
        LOG.info(f"Loading Riva streaming: {self.riva_model_name} @ {self.riva_server_uri}")

        self.riva_stream_thread = RivaStreamingThread(
            server_uri=self.riva_server_uri,
            model_name=self.riva_model_name,
            sample_rate=sample_rate,
            word_callback=self._on_words_recognized
        )
        self.riva_stream_thread.start()
        LOG.info("Riva streaming thread started")

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def _stream_realtime_phrase(self, source, sec_per_buffer, stream=None, ww_frames=None):
        """Stream and process audio in realtime.

        Replaces _record_phrase to process chunks immediately instead of
        recording first then transcribing.
        """
        LOG.info("🎤 Starting realtime streaming session")

        self.session_active = True
        self.last_word_time = time.time()
        self.session_start_time = self.last_word_time
        self.previous_partial_text = ""

        # Reset mode to deterministic
        self.mode = MODE_DETERMINISTIC
        self.free_text_intent = None
        self.free_text_final_words_seen = []

        # Reset STT backend
        if self.stt_backend == 'riva' and self.riva_stream_thread:
            self.riva_stream_thread.reset()
            self.riva_interim_word_count = 0
            self.riva_final_word_count = 0
        elif self.whisper_stream_thread:
            self.whisper_stream_thread.reset()

        # Audio buffering
        self.audio_buffer.clear()
        self.session_start_buffer_len = 0

        # Reset BOTH matchers for new session
        self.interim_matcher.reset_session()
        self.final_matcher.reset_session()
        self.shared_global_budget = self.interim_matcher.global_budget

        # Reset min replay positions
        self.interim_min_replay_pos = 0
        self.final_min_replay_pos = 0

        # Open repeat window if cross-session repeat is enabled and we have a prior command
        if self.repeat_enabled and self.repeat_cross_session and self.last_executed_command:
            self._open_repeat_window()

        if stream:
            stream.stream_start()

        # Process wake word frames first
        if ww_frames:
            for chunk in ww_frames:
                chunk_float = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                for sample in chunk_float:
                    self.audio_buffer.append(sample)
                self._process_audio_chunk(chunk)

        self.session_start_buffer_len = len(self.audio_buffer)

        # Main loop
        while self.session_active:
            now = time.time()

            # Check session timeout (always applies regardless of mode)
            if now - self.last_word_time > self.session_timeout:
                LOG.info("Session timeout - ending")
                if self.mode == MODE_FREE_TEXT:
                    # Timeout while in free-text mode — execute what we have
                    LOG.info("Session timeout during free-text mode — executing with what was recorded")
                    self._execute_free_text()
                self.session_active = False
                break

            # Free-text hard timeout (separate from session timeout)
            if self.mode == MODE_FREE_TEXT:
                elapsed_ft = now - self.free_text_mode_start_time
                if elapsed_ft > self.free_text_hard_timeout:
                    LOG.info(f"Free-text hard timeout ({self.free_text_hard_timeout}s) — executing")
                    self._execute_free_text()
                    self.session_active = False
                    break

                # Free-text stop timer (silence after a content word)
                if self.free_text_stop_timer is not None:
                    silence_elapsed = now - self.free_text_stop_timer
                    if silence_elapsed > self.free_text_current_stop_silence:
                        LOG.info(f"Free-text stop silence ({silence_elapsed:.1f}s) — executing")
                        self._execute_free_text()
                        self.session_active = False
                        break

            # Close repeat window if it has expired
            if self.repeat_window_open and self.last_command_dispatch_time:
                if now - self.last_command_dispatch_time > self.repeat_window_seconds:
                    self._close_repeat_window()

            # Get audio chunk
            chunk = self.responsive_recognizer.record_sound_chunk(source)

            # Buffer audio
            chunk_float = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            for sample in chunk_float:
                self.audio_buffer.append(sample)

            # ── Free-text VAD: mirrors Mycroft's _record_phrase energy check ──
            if self.mode == MODE_FREE_TEXT:
                rr = self.responsive_recognizer
                energy = rr.calc_energy(chunk, source.SAMPLE_WIDTH)
                is_loud = energy > rr.energy_threshold * rr.multiplier
                if not is_loud:
                    rr._adjust_threshold(energy, sec_per_buffer)

                if is_loud:
                    self.free_text_vad_loud_chunks += 1
                    if self.free_text_vad_suppressed:
                        # Coming back from suppression — need min_loud_chunks to re-enter speech state
                        if self.free_text_vad_loud_chunks >= self.free_text_vad_min_loud_chunks:
                            self.free_text_vad_speaking = True
                            self.free_text_vad_suppressed = False
                            LOG.info(f"[VAD] Speech resumed after suppression")
                    elif not self.free_text_vad_speaking:
                        # Normal silence → speech transition
                        if self.free_text_vad_loud_chunks >= self.free_text_vad_min_loud_chunks:
                            self.free_text_vad_speaking = True
                            LOG.info(f"[VAD] Speech detected ({self.free_text_vad_loud_chunks} chunks)")
                    else:
                        # Already speaking — need min_resume_chunks to reset a running stop timer
                        if self.free_text_vad_loud_chunks >= self.free_text_vad_min_resume_chunks:
                            if self.free_text_stop_timer is not None:
                                LOG.info(f"[VAD] Sustained speech — stop timer reset")
                                self.free_text_stop_timer = None
                else:
                    # Silence chunk — reset loud counter
                    self.free_text_vad_loud_chunks = 0
                    if self.free_text_vad_speaking:
                        # Transition: speech → silence
                        last = self.free_text_last_nonfiller_word
                        if last is None or (last not in self.filler_words and last not in self.non_terminal_words):
                            # Content word (or no FINAL word yet) — start stop timer
                            if self.free_text_stop_timer is None:
                                self.free_text_stop_timer = now
                                LOG.info(f"[VAD] Silence after '{last}' — stop timer started")
                            self.free_text_vad_speaking = False
                        else:
                            # Non-terminal/filler — suppress, clear timer, wait for new speech
                            self.free_text_stop_timer = None
                            self.free_text_vad_speaking = False
                            self.free_text_vad_suppressed = True
                            LOG.info(f"[VAD] Silence after non-terminal/filler '{last}' — suppressed, waiting for speech")
                    # else: already in silence (timer ticking, or suppressed waiting for speech)

            if stream:
                stream.stream_chunk(chunk)

            self._process_audio_chunk(chunk)

        self.session_active = False

        if self.stt_backend == 'riva' and self.riva_stream_thread:
            self.riva_stream_thread.end_session()

        LOG.info("Session ended")

        if stream:
            stream.stream_stop()

        return b''

    # ── Repeat window helpers ─────────────────────────────────────────────────

    def _open_repeat_window(self):
        """Add quantifier first-words to the transient valid-first-word set."""
        if not self.repeat_enabled:
            return
        # Both matchers maintain a transient_first_words set; we populate it here.
        # pattern_loader registered __REPEAT__:N:bare patterns — the matcher already
        # knows those sequences. We just flag the window as open so the dispatch
        # handler applies the window check.
        self.repeat_window_open = True
        LOG.info(f"Repeat window opened ({self.repeat_window_seconds}s)")

    def _close_repeat_window(self):
        """Remove quantifier first-words from the transient valid-first-word set."""
        self.repeat_window_open = False
        LOG.info("Repeat window closed")

    # ── Free-text mode ────────────────────────────────────────────────────────

    def _enter_free_text_mode(self, intent_name, trigger_utterance, trigger_word_end_time=None):
        """Switch to free-text mode after a deterministic prefix matched.

        Args:
            intent_name: Original intent (without __FREE_TEXT__: prefix)
            trigger_utterance: The matched deterministic prefix words
            trigger_word_end_time: Riva end_time (session-relative seconds) of last
                                   deterministic word, for precise audio buffer slicing.
                                   If None, use current time estimate.
        """
        self.mode = MODE_FREE_TEXT
        self.free_text_intent = intent_name
        self.free_text_trigger_utterance = trigger_utterance
        self.free_text_mode_start_time = time.time()
        self.free_text_stop_timer = None  # No stop timer until first silence after content word
        self.free_text_final_words_seen = []
        self.free_text_last_nonfiller_word = None
        self.free_text_vad_speaking = True  # Assume speaking when mode is entered
        self.free_text_vad_loud_chunks = 0
        self.free_text_vad_suppressed = False

        # Calculate audio buffer start index for Whisper slice
        # audio_buffer is a rolling deque of float32 samples at 16kHz
        sample_rate = self.realtime_config.get('sample_rate', 16000)
        if trigger_word_end_time is not None:
            # Riva provides session-relative timestamps; convert to buffer index
            # session_start_buffer_len is where the session started in the buffer
            trigger_samples = int(trigger_word_end_time * sample_rate)
            self.free_text_audio_start_idx = self.session_start_buffer_len + trigger_samples
        else:
            # Estimate: use current buffer length minus a small offset (~150ms)
            offset_samples = int(0.15 * sample_rate)
            self.free_text_audio_start_idx = max(0, len(self.audio_buffer) - offset_samples)

        LOG.info(f"🎙️  FREE-TEXT MODE: intent='{intent_name}' prefix='{trigger_utterance}' "
                 f"audio_start={self.free_text_audio_start_idx}")

    def _on_free_text_final_word(self, word):
        """Process a FINAL stream word while in free-text mode.

        Classifies word and manages the stop timer. Does not affect the matcher.
        """
        word_lower = word.lower()
        self.free_text_final_words_seen.append(word_lower)

        # Check for cancel (multi-word phrases — check last N words)
        seen = self.free_text_final_words_seen
        for cancel_phrase in self.free_text_cancel_words:
            phrase_words = cancel_phrase.split()
            if len(seen) >= len(phrase_words):
                if seen[-len(phrase_words):] == phrase_words:
                    LOG.info(f"🚫 Free-text cancelled by '{cancel_phrase}'")
                    self.mode = MODE_DETERMINISTIC
                    self.free_text_intent = None
                    self.free_text_final_words_seen = []
                    # Reset both matchers — treat cancel as a session reset
                    self.interim_matcher.reset()
                    self.final_matcher.reset()
                    return

        # Classify word
        if word_lower in self.filler_words:
            # Filler — don't touch stop timer (full timeout still applies)
            LOG.debug(f"  [free-text] filler '{word}' — timer unchanged")
        elif word_lower in self.non_terminal_words:
            # Non-terminal — use full session timeout as silence budget
            self.free_text_current_stop_silence = self.free_text_stop_nonterminal
            self.free_text_stop_timer = time.time()
            self.free_text_last_nonfiller_word = word_lower
            LOG.debug(f"  [free-text] non-terminal '{word}' — extended timer "
                      f"({self.free_text_stop_nonterminal}s)")
        else:
            # Content word — start normal stop timer
            self.free_text_current_stop_silence = self.free_text_stop_silence
            self.free_text_stop_timer = time.time()
            self.free_text_last_nonfiller_word = word_lower
            LOG.debug(f"  [free-text] content '{word}' — stop timer "
                      f"({self.free_text_stop_silence}s)")

        self.last_word_time = time.time()

    def _execute_free_text(self):
        """Slice audio buffer, run Whisper batch inference, dispatch intent."""
        if not self.free_text_stt:
            LOG.warning("Free-text STT not available — cannot execute free-text command")
            return

        sample_rate = self.realtime_config.get('sample_rate', 16000)

        # Slice audio buffer from trigger point to now
        buf = list(self.audio_buffer)
        start = max(0, self.free_text_audio_start_idx - (len(self.audio_buffer) -
                    min(len(buf), len(self.audio_buffer))))
        # Simpler: work with current buffer length
        # audio_buffer is a deque with maxlen; oldest samples drop off the left.
        # We stored free_text_audio_start_idx as an absolute count from session start.
        # Current position = session_start_buffer_len + samples_since_session_start
        # Samples in buffer from start_idx onward:
        buf_array = np.array(buf, dtype=np.float32)
        buf_total_since_session = len(buf) - self.session_start_buffer_len
        start_relative = self.free_text_audio_start_idx - self.session_start_buffer_len
        start_in_buf = len(buf) - buf_total_since_session + start_relative
        start_in_buf = max(0, int(start_in_buf))

        audio_slice = buf_array[start_in_buf:]

        if len(audio_slice) < int(sample_rate * 0.3):
            LOG.warning("Free-text audio slice too short — skipping")
            return

        LOG.info(f"🎵 Running Whisper on {len(audio_slice)/sample_rate:.1f}s of audio "
                 f"(intent: {self.free_text_intent})")

        text = self.free_text_stt.transcribe(audio_slice, sample_rate)

        if not text:
            LOG.warning("Whisper returned empty transcription — not dispatching")
            return

        # Strip any leading words from Whisper output that duplicate the trigger prefix.
        # This happens when the audio slice starts slightly before the trigger word's
        # true end (INTERIM timestamps are less precise than FINAL).
        trigger_words = self.free_text_trigger_utterance.lower().split()
        text_words = text.split()
        i = 0
        for tw in trigger_words:
            if i < len(text_words) and text_words[i].lower().rstrip('.,!?') == tw:
                i += 1
        if i > 0:
            text = ' '.join(text_words[i:])
            LOG.info(f"Stripped {i} duplicate trigger word(s) from Whisper output")

        if not text:
            LOG.warning("Whisper output was only trigger words — not dispatching")
            return

        # Build the full utterance: deterministic prefix + whisper text
        full_utterance = f"{self.free_text_trigger_utterance} {text}".strip()
        LOG.info(f"✓ FREE-TEXT COMMAND: '{full_utterance}' (intent: {self.free_text_intent})")

        # Dispatch to skills
        self.emit('recognizer_loop:utterance', {
            'utterances': [full_utterance],
            'lang': self.lang
        })

        if self.debug:
            self.emit('mycroft.debug.riva.matched', {'utterance': full_utterance})

        # Record in history (free-text commands don't update last_executed_command
        # since they're not repeatable in the same way)
        now = time.time()
        self.executed_utterances_history.append(
            (full_utterance, now, 'FREE_TEXT', [], None, self.free_text_intent)
        )
        if len(self.executed_utterances_history) > self.dedup_history_size:
            self.executed_utterances_history.pop(0)

        # Reset matchers
        self.interim_matcher.reset()
        self.final_matcher.reset()

    # ── Repeat mode ───────────────────────────────────────────────────────────

    def _enter_repeat_mode(self, n, is_bare):
        """Execute repeat mode: replay last_executed_command (n-1) more times.

        Args:
            n: Total desired executions (already ran once, so replay n-1 times)
            is_bare: True if triggered by bare quantifier (no prefix words) —
                     only valid within the repeat window
        """
        if is_bare and not self.repeat_window_open:
            LOG.info(f"⏩ Repeat bare quantifier outside window — ignoring")
            return

        if not self.last_executed_command:
            LOG.info("⏩ Repeat triggered but no prior command this session — ignoring")
            return

        utterance, intent = self.last_executed_command
        additional = n - 1

        if additional <= 0:
            LOG.info(f"⏩ Repeat n={n} → already ran once, 0 additional — ignoring")
            return

        LOG.info(f"🔁 REPEAT MODE: '{utterance}' × {additional} more time(s) (n={n})")

        now = time.time()
        for i in range(additional):
            LOG.info(f"  🔁 Repeat {i+1}/{additional}: '{utterance}'")
            self.emit('recognizer_loop:utterance', {
                'utterances': [utterance],
                'lang': self.lang
            })
            if self.debug:
                self.emit('mycroft.debug.riva.matched', {'utterance': utterance})

        # Add to dedup history (repeats themselves don't update last_executed_command)
        repeat_entry = (f"[repeat×{additional}] {utterance}", now, 'REPEAT', [], None, intent)
        self.executed_utterances_history.append(repeat_entry)
        if len(self.executed_utterances_history) > self.dedup_history_size:
            self.executed_utterances_history.pop(0)

        # Reset matchers and close repeat window until next non-repeat command
        self.interim_matcher.reset()
        self.final_matcher.reset()
        self._close_repeat_window()

    # ── Word callbacks ────────────────────────────────────────────────────────

    def _on_word_recognized(self, word):
        """Callback when Whisper streaming recognizes a word (legacy path)."""
        now = time.time()

        if self.last_batch_time:
            latency = now - self.last_batch_time
            LOG.info(f"[WHISPER] {word} (latency: {latency*1000:.0f}ms from last batch)")
        else:
            LOG.info(f"[WHISPER] {word}")

        self.last_word_time = now

        if self.debug:
            self.emit('mycroft.debug.whisper.partial', {'utterance': word})

        match, _ = self.interim_matcher.add_word(word)

        if match:
            utterance = match['utterance']
            LOG.info(f"✓ COMMAND MATCHED: '{utterance}' (intent: {match['intent']})")
            self.emit('recognizer_loop:utterance', {
                'utterances': [utterance],
                'lang': self.lang
            })
            if self.debug:
                self.emit('mycroft.debug.whisper.final', {'utterance': utterance})
            self.interim_matcher.reset()
            self.shared_global_budget = self.interim_matcher.global_budget

        if self.interim_matcher.global_budget <= 0:
            LOG.info(f"Whisper matcher budget exhausted — ending session")
            self.session_active = False

    def _on_words_recognized(self, words, is_final, word_timings=None):
        """Callback when Riva recognizes words (interim or final).

        Riva sends TWO SEPARATE CUMULATIVE STREAMS:
          Interim: ["turn"] → ["turn","lights"] → ["turn","lights","on"]
          Final:   ["turn","lights","on"]

        In FREE_TEXT mode: only FINAL words are used for stop detection.
        In REPEAT mode: words are processed normally (mode resets quickly).
        In DETERMINISTIC mode: full dual-matcher logic applies.

        Args:
            words: Full cumulative transcript as word list
            is_final: True if FINAL stream
            word_timings: List of (start_sec, end_sec) parallel to words, or None
        """
        now = time.time()
        stream_name = "FINAL" if is_final else "INTERIM"

        # ── FREE_TEXT mode: only process FINAL for stop detection ────────────
        if self.mode == MODE_FREE_TEXT:
            if is_final:
                # Find new words since last FINAL callback
                current_count = self.riva_final_word_count
                new_words = words[current_count:]
                self.riva_final_word_count = len(words)
                self.prev_final_transcript = ' '.join(words)
                for word in new_words:
                    self._on_free_text_final_word(word)
                    if self.mode != MODE_FREE_TEXT:
                        # Cancelled mid-word-loop
                        break
            else:
                # INTERIM in free-text mode: ignored.
                # Stop detection is handled by audio VAD (RMS energy) in the session loop.
                # INTERIM hallucinations during silence would otherwise keep resetting the timer.
                pass
            return

        # ── DETERMINISTIC / REPEAT mode: full dual-matcher logic ─────────────

        if is_final:
            matcher = self.final_matcher
            current_count = self.riva_final_word_count
        else:
            matcher = self.interim_matcher
            current_count = self.riva_interim_word_count

        # Budget coordination
        matcher.global_budget = self.shared_global_budget

        transcript = ' '.join(words)

        # Detect word count drop (new segment or refinement)
        if current_count > len(words):
            LOG.info(f"[RIVA {stream_name}] Word count dropped "
                     f"(had {current_count}, now {len(words)}) | "
                     f"Active paths: {len(matcher.active_paths)}")

            prev_transcript = (self.prev_final_transcript if is_final
                               else self.prev_interim_transcript)
            prev_words = prev_transcript.split() if prev_transcript else []

            is_refinement = bool(prev_words and words and words[0] == prev_words[0])

            if is_refinement:
                LOG.info(f"[RIVA {stream_name}] Refinement: "
                         f"prev='{prev_transcript}' new='{transcript}' — resetting matcher")
                matcher.reset()
            else:
                LOG.info(f"[RIVA {stream_name}] True segment boundary: "
                         f"prev='{prev_transcript}' new='{transcript}' — "
                         f"preserving {len(matcher.active_paths)} paths")
                if is_final:
                    self.final_min_replay_pos = 0
                else:
                    self.interim_min_replay_pos = 0

            current_count = 0
            if is_final:
                self.riva_final_word_count = 0
                self.prev_final_transcript = ""
            else:
                self.riva_interim_word_count = 0
                self.prev_interim_transcript = ""

        elif current_count > 0:
            # Check if Riva changed previously processed words
            prev_transcript = (self.prev_final_transcript if is_final
                               else self.prev_interim_transcript)
            prev_words = prev_transcript.split()[:current_count]
            curr_words = words[:current_count]

            if prev_words != curr_words:
                LOG.info(f"[RIVA {stream_name}] Transcript changed: "
                         f"{prev_words} → {curr_words} | "
                         f"Active paths: {len(matcher.active_paths)}")

                # UNDO WINDOW: Only FINAL stream can undo a prior INTERIM execution
                if is_final and self.executed_utterances_history:
                    last_entry = self.executed_utterances_history[-1]
                    if len(last_entry) == 6:
                        last_utterance, last_time, last_stream, last_matched_words, last_match_position, _ = last_entry
                    elif len(last_entry) == 5:
                        last_utterance, last_time, last_stream, last_matched_words, last_match_position = last_entry
                    elif len(last_entry) == 4:
                        last_utterance, last_time, last_stream, last_matched_words = last_entry
                        last_match_position = None
                    else:
                        last_utterance, last_time, last_stream = last_entry
                        last_matched_words = None
                        last_match_position = None

                    time_since_last = now - last_time

                    if last_stream == "INTERIM" and time_since_last < 1.0:
                        should_undo = False

                        if last_matched_words is None or last_match_position is None:
                            should_undo = True
                            LOG.info(f"⚠️  Transcript changed {time_since_last:.1f}s after "
                                     f"execution — removed stale dedup entry: '{last_utterance}' "
                                     f"(no position tracked)")
                        else:
                            matched_word_count = len(last_matched_words)
                            if last_match_position + matched_word_count <= len(curr_words):
                                current_matched_words = curr_words[
                                    last_match_position:last_match_position + matched_word_count
                                ]
                                if current_matched_words != last_matched_words:
                                    words_before_match = curr_words[:last_match_position]
                                    replacement_all_valid = True
                                    for i, replacement_word in enumerate(current_matched_words):
                                        position_context = words_before_match + current_matched_words[:i]
                                        valid_at_position = set()
                                        for seq_info in self.final_matcher.all_sequences:
                                            seq = seq_info['sequence']
                                            pos = len(position_context)
                                            if pos < len(seq):
                                                context_matches = all(
                                                    seq[j].get('word') == position_context[j]
                                                    for j in range(pos)
                                                    if 'word' in seq[j]
                                                )
                                                if context_matches and 'word' in seq[pos]:
                                                    valid_at_position.add(seq[pos]['word'])
                                        if replacement_word not in valid_at_position:
                                            replacement_all_valid = False
                                            LOG.debug(f"Replacement word '{replacement_word}' "
                                                      f"not valid — keeping dedup protection")
                                            break
                                    if replacement_all_valid:
                                        should_undo = True
                                        LOG.info(f"⚠️  Matched words changed {time_since_last:.1f}s "
                                                 f"after execution at position {last_match_position}: "
                                                 f"{last_matched_words} → {current_matched_words} — "
                                                 f"removed stale dedup entry: '{last_utterance}'")
                                else:
                                    LOG.debug(f"Transcript changed but matched words unchanged "
                                              f"— keeping dedup protection")
                            else:
                                LOG.debug(f"Match position no longer in transcript — "
                                          f"keeping dedup protection")

                        if should_undo:
                            self.executed_utterances_history.pop()

                # Prune paths corrected to a different valid command-starting word
                changed_positions = {}
                for _i, (old, new) in enumerate(zip(prev_words, curr_words)):
                    if old != new:
                        changed_positions[_i] = (old, new)
                if changed_positions:
                    matcher.prune_corrected_paths(changed_positions, curr_words)

                min_replay_pos = (self.final_min_replay_pos if is_final
                                  else self.interim_min_replay_pos)
                current_count = min_replay_pos
                if is_final:
                    self.riva_final_word_count = min_replay_pos
                    self.prev_final_transcript = ""
                else:
                    self.riva_interim_word_count = min_replay_pos
                    self.prev_interim_transcript = ""

        # Extract new words
        new_words = words[current_count:]

        if new_words:
            LOG.debug(f"[RIVA {stream_name}] New words: {new_words} "
                      f"(processed {current_count}/{len(words)})")

        # Update previous transcript
        if is_final:
            self.prev_final_transcript = transcript
        else:
            self.prev_interim_transcript = transcript

        # Process new words through matcher
        for word in new_words:
            current_count += 1
            if is_final:
                self.riva_final_word_count = current_count
            else:
                self.riva_interim_word_count = current_count

            LOG.info(f"[RIVA {stream_name}] {word}")

            if self.debug:
                event_name = ('mycroft.debug.riva.final' if is_final
                              else 'mycroft.debug.riva.partial')
                self.emit(event_name, {'utterance': word})

            match, word_accepted = matcher.add_word(word, stream_type=stream_name,
                                                    transcript_position=current_count - 1)

            # Only update last_word_time when the word was genuinely accepted by a
            # matcher path — hallucinated garbage words that no path accepts should
            # not extend the session timeout.
            if word_accepted:
                self.last_word_time = now

            if match:
                utterance = match['utterance']
                intent = match['intent']
                LOG.info(f"✓ COMMAND MATCHED ({stream_name}): '{utterance}' (intent: {intent})")

                # ── FREE_TEXT trigger ─────────────────────────────────────────
                if intent.startswith(FREE_TEXT_TAG):
                    original_intent = intent[len(FREE_TEXT_TAG):]
                    # Get end_time of last deterministic word for precise audio cut
                    trigger_end_time = None
                    if is_final and word_timings and len(word_timings) >= current_count:
                        trigger_end_time = word_timings[current_count - 1][1]
                    self._enter_free_text_mode(original_intent, utterance, trigger_end_time)
                    # Reset matchers — deterministic matching pauses
                    self.interim_matcher.reset()
                    self.final_matcher.reset()
                    break

                # ── REPEAT trigger ────────────────────────────────────────────
                if intent.startswith(REPEAT_TAG):
                    tag_body = intent[len(REPEAT_TAG):]  # e.g. '3' or '3:bare'
                    is_bare = tag_body.endswith(':bare')
                    n_str = tag_body.replace(':bare', '')
                    try:
                        n = int(n_str)
                    except ValueError:
                        LOG.warning(f"Invalid repeat intent tag: {intent}")
                        continue
                    self.interim_matcher.reset()
                    self.final_matcher.reset()
                    self._enter_repeat_mode(n, is_bare)
                    break

                # ── FINAL cross-command stitch guard ──────────────────────────
                if is_final:
                    matched_words_list = utterance.split()
                    _match_pos = None
                    for _i in range(len(words) - len(matched_words_list) + 1):
                        if words[_i:_i + len(matched_words_list)] == matched_words_list:
                            _match_pos = _i
                    if _match_pos is not None:
                        _match_end = _match_pos + len(matched_words_list)
                        _matched_remaining = list(matched_words_list)
                        _skipped = []
                        for _seg_word in words[_match_pos:_match_end]:
                            if _matched_remaining and _seg_word == _matched_remaining[0]:
                                _matched_remaining.pop(0)
                            else:
                                _skipped.append(_seg_word)
                        _valid_first = set()
                        for _seq in matcher.all_sequences:
                            if _seq['sequence']:
                                _first = _seq['sequence'][0]
                                if 'word' in _first:
                                    _valid_first.add(_first['word'])
                        _stitch_words = [w for w in _skipped if w in _valid_first]
                        if _stitch_words:
                            LOG.info(f"⛔ FINAL cross-command stitch rejected: '{utterance}' "
                                     f"skipped over command-starting word(s) {_stitch_words}")
                            continue

                # ── Deduplication ─────────────────────────────────────────────
                LOG.info(f"[DEDUP CHECK] '{utterance}' | History: "
                         f"{[(e[0], f'{now - e[1]:.1f}s ago', e[2]) for e in self.executed_utterances_history]}")
                is_duplicate = False
                for entry in self.executed_utterances_history:
                    if len(entry) >= 3:
                        prev_utterance, prev_time = entry[0], entry[1]
                    else:
                        continue
                    elapsed = now - prev_time
                    if utterance == prev_utterance and elapsed < self.dedup_window_seconds:
                        LOG.info(f"⏭️  Skipping duplicate: '{utterance}' "
                                 f"({elapsed:.1f}s ago by {entry[2]})")
                        is_duplicate = True
                        break
                    curr_words_list = utterance.split()
                    prev_words_list = prev_utterance.split()
                    if (len(curr_words_list) > 1 and len(prev_words_list) > 1
                            and curr_words_list[1:] == prev_words_list[1:]
                            and curr_words_list[0] != prev_words_list[0]
                            and elapsed < 1.5):
                        LOG.info(f"⏭️  Skipping alias duplicate: '{utterance}' same as "
                                 f"'{prev_utterance}' ({elapsed:.2f}s ago by {entry[2]})")
                        is_duplicate = True
                        break

                if is_duplicate:
                    continue

                # ── Dispatch ──────────────────────────────────────────────────
                self.emit('recognizer_loop:utterance', {
                    'utterances': [utterance],
                    'lang': self.lang
                })

                if self.debug:
                    self.emit('mycroft.debug.riva.matched', {'utterance': utterance})

                # Budget sync
                self.shared_global_budget = matcher.global_budget
                LOG.debug(f"Global budget synced: {self.shared_global_budget}")

                # Reset both matchers
                self.interim_matcher.reset()
                self.final_matcher.reset()

                # Record execution
                matched_words = utterance.split()
                match_position = None
                for i in range(len(words) - len(matched_words) + 1):
                    if words[i:i + len(matched_words)] == matched_words:
                        match_position = i

                self.executed_utterances_history.append(
                    (utterance, now, stream_name, matched_words, match_position, intent)
                )
                if len(self.executed_utterances_history) > self.dedup_history_size:
                    self.executed_utterances_history.pop(0)

                # Advance min replay position
                if match_position is not None:
                    new_min = match_position + len(matched_words)
                else:
                    new_min = current_count
                if is_final:
                    self.final_min_replay_pos = max(self.final_min_replay_pos, new_min)
                else:
                    self.interim_min_replay_pos = max(self.interim_min_replay_pos, new_min)
                LOG.debug(f"Min replay pos for {stream_name} advanced to {new_min}")

                # Update last_executed_command for repeat mode (non-repeat commands only)
                self.last_executed_command = (utterance, intent)
                self.last_command_dispatch_time = now

                # Open repeat window for bare quantifier matching
                if self.repeat_enabled:
                    self._open_repeat_window()

                LOG.debug(f"Both matchers reset after {stream_name} match")
                break

        # Budget sync after processing all words
        self.shared_global_budget = matcher.global_budget

        if self.shared_global_budget <= 0:
            LOG.info(f"Global budget exhausted — ending session")
            self.session_active = False
            if self.stt_backend == 'riva' and self.riva_stream_thread:
                self.riva_stream_thread.end_session()

    # ── Audio routing ─────────────────────────────────────────────────────────

    def _process_audio_chunk(self, audio_chunk):
        """Route audio chunk to appropriate STT backend."""
        if self.stt_backend == 'riva':
            self._process_riva_chunk(audio_chunk)
        else:
            self._process_whisper_chunk(audio_chunk)

    def _process_riva_chunk(self, audio_chunk):
        """Feed audio chunk to Riva streaming."""
        if self.riva_stream_thread:
            self.riva_stream_thread.feed_audio(audio_chunk)

    def _process_whisper_chunk(self, audio_chunk):
        """Batch 64ms chunks into 0.5s chunks before feeding to Whisper streaming."""
        chunk_float = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        self.whisper_chunk_buffer.extend(chunk_float)

        if len(self.whisper_chunk_buffer) >= self.whisper_chunk_size:
            batch = np.array(self.whisper_chunk_buffer[:self.whisper_chunk_size], dtype=np.float32)
            self.whisper_chunk_buffer = self.whisper_chunk_buffer[self.whisper_chunk_size:]

            queue_size = self.whisper_stream_thread.queue.qsize()
            now = time.time()
            if self.last_batch_time:
                batch_interval = now - self.last_batch_time
                LOG.debug(f"[TIMING] Batch interval: {batch_interval*1000:.0f}ms (target: 500ms)")
            self.last_batch_time = now

            self.whisper_stream_thread.queue.put(batch)

            if queue_size > 0:
                LOG.warning(f"⚠️ [QUEUE] Size: {queue_size} batches ({queue_size * 0.5:.1f}s audio behind)")

    def _process_with_whisper(self):
        """Process buffered audio with Whisper for accurate transcription (legacy)."""
        if len(self.audio_buffer) < 16000 * 0.3:
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

    # ── Loop lifecycle ────────────────────────────────────────────────────────

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

    def transcribe(self, audio):
        """Override to use streaming instead of traditional STT."""
        LOG.debug("Realtime transcribe called - using streaming instead")
        return None
