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
import threading
from collections import deque

import numpy as np

from mycroft.client.realtime.listener import RecognizerLoop
from mycroft.client.realtime.mic import ResponsiveRecognizer
from mycroft.client.realtime.command_matcher import StreamingCommandMatcher
from mycroft.client.realtime.riva_streaming import RivaStreamingThread, RIVA_AVAILABLE
from mycroft.client.realtime.free_text_stt import load_free_text_stt
from mycroft.client.realtime.pattern_loader import FREE_TEXT_TAG, REPEAT_TAG, MANAGE_TAG
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

        # Free-text secondary STT (batch inference for nondeterministic commands)
        self.free_text_stt = load_free_text_stt(self.realtime_config)
        _stt_cfg = self.realtime_config.get('free_text_stt', {})
        self._stt_sample_rate = self.realtime_config.get('sample_rate', 16000)
        self._stt_label = _stt_cfg.get('manage_commands', {}).get('label', 'secondary STT')
        if self.free_text_stt and _stt_cfg.get('preload', False):
            LOG.info("free_text_stt preload=true — loading persistent worker at startup")
            self.free_text_stt.load(self._stt_sample_rate)

        # Free-text config
        _ft_cfg = self.realtime_config.get('free_text', {})
        self.free_text_entity_name = _ft_cfg.get('entity_name', 'free_text')
        self.free_text_stop_silence = _ft_cfg.get('stop_silence_seconds', 1.0)
        # null → inherit session_timeout (set after session_timeout is loaded)
        self._ft_stop_nonterminal_cfg = _ft_cfg.get('stop_silence_non_terminal_seconds')
        self._ft_stop_filler_cfg = _ft_cfg.get('stop_silence_filler_seconds')
        self._ft_hard_timeout_cfg = _ft_cfg.get('hard_timeout_seconds')
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

        # Command group management
        self.command_groups = self.realtime_config.get('command_groups', [])
        self.skill_pattern_groups = {}   # skill_prefix → list of CommandPattern objects
        self.protected_patterns = []     # always-active: management commands
        self.manually_disabled = set()   # skill_prefixes manually disabled
        self.global_mute = False         # True when "disable commands" fired

        # Audio batching buffer for whisper streaming (legacy path)
        self.whisper_chunk_buffer = []
        self.whisper_chunk_size = 8000  # 0.5s at 16kHz
        self.last_batch_time = None

        # ── Operating mode ────────────────────────────────────────────────────
        self.mode = MODE_DETERMINISTIC

        # Free-text mode state
        self.free_text_intent = None            # e.g. 'websites.search'
        self.free_text_trigger_utterance = None # deterministic prefix that fired
        self.free_text_audio_start_idx = 0      # index into audio_buffer at trigger (legacy path)
        # Accumulate FINAL words seen in free-text mode for cancel detection
        self.free_text_final_words_seen = []
        # Pending free-text request set by Riva callback thread; executed by loop thread
        # to avoid both threads competing for the audio source simultaneously.
        self._pending_free_text = None          # (intent_name, trigger_utterance) or None

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
        from mycroft.client.realtime.whisper_streaming_wrapper import WhisperStreamingThread
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
        self.emit('mycroft.realtime.session_start', {})

        self.session_active = True
        self.last_word_time = time.time()
        self.session_start_time = self.last_word_time
        self.previous_partial_text = ""
        self._current_source = source
        self._current_sec_per_buffer = sec_per_buffer

        # Reset mode to deterministic
        self.mode = MODE_DETERMINISTIC
        self.free_text_intent = None
        self.free_text_final_words_seen = []
        self._pending_free_text = None

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

            # Check session timeout
            if now - self.last_word_time > self.session_timeout:
                LOG.info("Session timeout - ending")
                self.session_active = False
                break

            # Close repeat window if it has expired
            if self.repeat_window_open and self.last_command_dispatch_time:
                if now - self.last_command_dispatch_time > self.repeat_window_seconds:
                    self._close_repeat_window()

            # If Riva callback requested free-text mode, handle it here on the
            # loop thread so we have exclusive access to source (no competing reads).
            if self._pending_free_text is not None:
                pending = self._pending_free_text
                self._pending_free_text = None
                self._do_free_text_recording(pending[0], pending[1], source, sec_per_buffer)
                continue

            # Get audio chunk
            chunk = self.responsive_recognizer.record_sound_chunk(source)

            # Buffer audio
            chunk_float = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            for sample in chunk_float:
                self.audio_buffer.append(sample)

            if stream:
                stream.stream_chunk(chunk)

            self._process_audio_chunk(chunk)

        self.session_active = False

        if self.stt_backend == 'riva' and self.riva_stream_thread:
            self.riva_stream_thread.end_session()

        LOG.info("Session ended")
        self.emit('mycroft.realtime.session_end', {})

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

    # ── Command group management ──────────────────────────────────────────────

    def _is_duplicate(self, key, now):
        """Check executed_utterances_history for a recent exact or alias match."""
        for entry in self.executed_utterances_history:
            if len(entry) < 3:
                continue
            prev_key, prev_time = entry[0], entry[1]
            elapsed = now - prev_time
            if key == prev_key and elapsed < self.dedup_window_seconds:
                return True
            curr_words_list = key.split()
            prev_words_list = prev_key.split()
            if (len(curr_words_list) > 1 and len(prev_words_list) > 1
                    and curr_words_list[1:] == prev_words_list[1:]
                    and curr_words_list[0] != prev_words_list[0]
                    and elapsed < 1.5):
                return True
        return False

    def _record_execution(self, key, now, stream_name, matched_words=None,
                          match_position=None, intent=None):
        """Append an entry to executed_utterances_history and cap its size."""
        self.executed_utterances_history.append(
            (key, now, stream_name, matched_words or [], match_position, intent or key)
        )
        if len(self.executed_utterances_history) > self.dedup_history_size:
            self.executed_utterances_history.pop(0)

    def _rebuild_shared_patterns(self):
        """Repopulate shared_patterns from protected + enabled skill groups.

        Called after any enable/disable command. shared_patterns is the same
        list object both matchers reference, so clearing+extending it is enough.
        all_sequences is then rebuilt on both matchers to stay in sync.
        """
        self.shared_patterns.clear()
        self.shared_patterns.extend(self.protected_patterns)
        if not self.global_mute:
            for prefix, patterns in self.skill_pattern_groups.items():
                if prefix not in self.manually_disabled:
                    self.shared_patterns.extend(patterns)
        self.interim_matcher._rebuild_all_sequences()
        self.final_matcher._rebuild_all_sequences()
        LOG.info(f"[MANAGE] Rebuilt shared_patterns: {len(self.shared_patterns)} patterns "
                 f"(mute={self.global_mute}, disabled={self.manually_disabled})")

    def _handle_manage_command(self, action, target):
        """Handle an enable/disable or STT load/unload voice command.

        Args:
            action: 'enable', 'disable', or 'stt'
            target: skill_prefix, 'commands', or 'load'/'unload' for stt action
        """
        # ── Secondary STT load/unload ─────────────────────────────────────────
        if action == 'stt':
            if not self.free_text_stt:
                LOG.warning("[MANAGE] STT command received but free_text_stt not available")
                return
            label = self._stt_label
            if target == 'load':
                if self.free_text_stt.is_loaded:
                    LOG.info(f"[MANAGE] {label} already loaded")
                    self.emit('mycroft.realtime.manage', {
                        'action': 'stt:load', 'target': 'stt',
                        'stt_state': 'already_loaded', 'stt_label': label
                    })
                else:
                    self.emit('mycroft.realtime.manage', {
                        'action': 'stt:load', 'target': 'stt',
                        'stt_state': 'loading', 'stt_label': label
                    })
                    self.free_text_stt.load(self._stt_sample_rate)
                    self.emit('mycroft.realtime.manage', {
                        'action': 'stt:load', 'target': 'stt',
                        'stt_state': 'loaded' if self.free_text_stt.is_loaded else 'failed',
                        'stt_label': label
                    })
            elif target == 'unload':
                self.emit('mycroft.realtime.manage', {
                    'action': 'stt:unload', 'target': 'stt',
                    'stt_state': 'unloading', 'stt_label': label
                })
                self.free_text_stt.unload()
                self.emit('mycroft.realtime.manage', {
                    'action': 'stt:unload', 'target': 'stt',
                    'stt_state': 'unloaded', 'stt_label': label
                })
            return

        # ── Enable/disable command groups ─────────────────────────────────────
        if action == 'disable' and target is None:
            self.global_mute = True
        elif action == 'enable' and target is None:
            self.global_mute = False
        elif action == 'disable' and target:
            self.manually_disabled.add(target)
        elif action == 'enable' and target:
            self.manually_disabled.discard(target)
        self._rebuild_shared_patterns()
        LOG.info(f"[MANAGE] {action} '{target or 'commands'}' — "
                 f"mute={self.global_mute} disabled={self.manually_disabled}")
        target_label = target or 'commands'
        prefix_to_name = {g['skill_prefix']: g['name'] for g in self.command_groups}
        for g in self.command_groups:
            if g.get('skill_prefix') == target:
                target_label = g['name']
                break
        disabled_names = [prefix_to_name.get(p, p) for p in self.manually_disabled]
        self.emit('mycroft.realtime.manage', {
            'action': action,
            'target': target_label,
            'muted': self.global_mute,
            'disabled': disabled_names
        })

    # ── Free-text mode ────────────────────────────────────────────────────────

    def _enter_free_text_mode(self, intent_name, trigger_utterance, trigger_word_end_time=None):
        """Request free-text recording from the session loop thread.

        Called from the Riva callback thread — must not block or read from source
        here, because the session loop is concurrently reading from the same source
        on its own thread. Instead, set a flag and stop Riva; the session loop will
        detect _pending_free_text and call _do_free_text_recording from its thread.

        Args:
            intent_name: Original intent (without __FREE_TEXT__: prefix)
            trigger_utterance: The matched deterministic prefix words
            trigger_word_end_time: unused, kept for call-site compatibility
        """
        if self.mode == MODE_FREE_TEXT or self._pending_free_text is not None:
            return  # guard against duplicate callbacks

        self.mode = MODE_FREE_TEXT
        self.free_text_intent = intent_name
        self.free_text_trigger_utterance = trigger_utterance
        self.free_text_final_words_seen = []

        LOG.info(f"🎙️  FREE-TEXT MODE: intent='{intent_name}' prefix='{trigger_utterance}'")
        self.emit('mycroft.realtime.mode_changed', {
            'mode': 'free_text',
            'trigger': trigger_utterance,
            'intent': intent_name
        })

        # Stop Riva so no more callbacks arrive during the recording window
        if self.stt_backend == 'riva' and self.riva_stream_thread:
            self.riva_stream_thread.end_session()

        # Pre-spawn Whisper subprocess now so model loads during VAD recording
        if self.free_text_stt and hasattr(self.free_text_stt, 'spawn'):
            sample_rate = self.realtime_config.get('sample_rate', 16000)
            self.free_text_stt.spawn(sample_rate)

        # Signal the loop thread to take over recording from source exclusively
        self._pending_free_text = (intent_name, trigger_utterance)

    def _do_free_text_recording(self, intent_name, trigger_utterance, source, sec_per_buffer):
        """Execute free-text VAD and Whisper transcription on the loop thread.

        Called exclusively from the session loop, so it has sole access to source.
        Uses Mycroft's built-in NoiseTracker VAD, then resets all state so the
        session loop continues normally (Riva restarts, no wake word required).
        """
        # Use Mycroft's built-in NoiseTracker VAD — blocks until silence detected.
        # Running here on the loop thread means we own source exclusively.
        old_timeout = self.responsive_recognizer.recording_timeout_with_silence
        silence_timeout = self.free_text_stop_silence
        if silence_timeout is not None:
            self.responsive_recognizer.recording_timeout_with_silence = silence_timeout

        byte_data = ResponsiveRecognizer._record_phrase(
            self.responsive_recognizer, source, sec_per_buffer
        )
        self.responsive_recognizer.recording_timeout_with_silence = old_timeout

        # Transcribe and dispatch
        sample_rate = self.realtime_config.get('sample_rate', 16000)
        audio = np.frombuffer(byte_data, dtype=np.int16).astype(np.float32) / 32768.0
        self._execute_free_text(audio=audio, sample_rate=sample_rate)

        # Resume streaming — reset all state, restart Riva, continue session loop
        self.mode = MODE_DETERMINISTIC
        self.free_text_intent = None
        self.free_text_trigger_utterance = None
        self.free_text_final_words_seen = []
        self.previous_partial_text = ""
        self.audio_buffer.clear()
        self.session_start_buffer_len = 0
        self.interim_matcher.reset_session()
        self.final_matcher.reset_session()
        self.shared_global_budget = self.interim_matcher.global_budget
        self.interim_min_replay_pos = 0
        self.final_min_replay_pos = 0
        self.last_word_time = time.time()

        if self.stt_backend == 'riva' and self.riva_stream_thread:
            self.riva_stream_thread.reset()
            self.riva_interim_word_count = 0
            self.riva_final_word_count = 0

        LOG.info("Free-text complete — resumed DETERMINISTIC mode, Riva restarted")

    def _execute_free_text(self, audio=None, sample_rate=None):
        """Run Whisper batch inference on audio and dispatch intent.

        Args:
            audio: float32 numpy array of audio samples. When provided (standard
                   VAD path), used directly. When None, falls back to slicing the
                   internal audio_buffer (legacy path, unused by current VAD).
            sample_rate: sample rate of audio. Defaults to config value.
        """
        if not self.free_text_stt:
            LOG.warning("Free-text STT not available — cannot execute free-text command")
            return

        if sample_rate is None:
            sample_rate = self.realtime_config.get('sample_rate', 16000)

        if audio is None:
            # Legacy buffer-slice path (kept for future entity types that may need it)
            buf = list(self.audio_buffer)
            buf_array = np.array(buf, dtype=np.float32)
            buf_total_since_session = len(buf) - self.session_start_buffer_len
            start_relative = self.free_text_audio_start_idx - self.session_start_buffer_len
            start_in_buf = len(buf) - buf_total_since_session + start_relative
            start_in_buf = max(0, int(start_in_buf))
            audio = buf_array[start_in_buf:]

        if len(audio) < int(sample_rate * 0.3):
            LOG.warning("Free-text audio too short — skipping")
            return

        LOG.info(f"🎵 Running Whisper on {len(audio)/sample_rate:.1f}s of audio "
                 f"(intent: {self.free_text_intent})")

        text = self.free_text_stt.transcribe(audio, sample_rate)

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
            'lang': self.lang,
            'intent': self.free_text_intent
        })
        self.emit('mycroft.realtime.command_matched', {
            'utterance': full_utterance,
            'intent': self.free_text_intent,
            'stream': 'FREE_TEXT'
        })

        if self.debug:
            self.emit('mycroft.debug.riva.matched', {'utterance': full_utterance})

        # Record in history (free-text commands don't update last_executed_command
        # since they're not repeatable in the same way)
        now = time.time()
        self._record_execution(full_utterance, now, 'FREE_TEXT',
                               intent=self.free_text_intent)

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
        self.emit('mycroft.realtime.mode_changed', {
            'mode': 'repeat',
            'utterance': utterance,
            'n': additional
        })

        now = time.time()
        for i in range(additional):
            LOG.info(f"  🔁 Repeat {i+1}/{additional}: '{utterance}'")
            self.emit('recognizer_loop:utterance', {
                'utterances': [utterance],
                'lang': self.lang,
                'intent': intent
            })
            if self.debug:
                self.emit('mycroft.debug.riva.matched', {'utterance': utterance})

        # Add to dedup history (repeats themselves don't update last_executed_command)
        self._record_execution(f"[repeat×{additional}] {utterance}", now, 'REPEAT',
                               intent=intent)

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

        In REPEAT mode: words are processed normally (mode resets quickly).
        In DETERMINISTIC mode: full dual-matcher logic applies.

        Args:
            words: Full cumulative transcript as word list
            is_final: True if FINAL stream
            word_timings: List of (start_sec, end_sec) parallel to words, or None
        """
        now = time.time()
        stream_name = "FINAL" if is_final else "INTERIM"

        # During FREE_TEXT mode Riva is stopped, so no words arrive — ignore if any slip through.
        if self.mode == MODE_FREE_TEXT:
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

                    if (last_stream == "INTERIM" and time_since_last < 1.0
                            and not last_utterance.startswith(MANAGE_TAG)
                            and not last_utterance.startswith(REPEAT_TAG)):
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
            LOG.debug(f"[RIVA {stream_name}] New words: {new_words} (processed {current_count}/{len(words)})")

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

            LOG.debug(f"[RIVA {stream_name}] {word}")

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
                    if self._is_duplicate(intent, now):
                        continue
                    self.interim_matcher.reset()
                    self.final_matcher.reset()
                    self._enter_repeat_mode(n, is_bare)
                    self._record_execution(intent, now, stream_name)
                    break

                # ── MANAGE trigger ────────────────────────────────────────────
                if intent.startswith(MANAGE_TAG):
                    tag_body = intent[len(MANAGE_TAG):]  # e.g. 'enable:search'
                    action, _, target_name = tag_body.partition(':')
                    if action == 'stt':
                        # STT load/unload — pass target_name ('load'/'unload') directly
                        manage_target = target_name
                    elif target_name == 'commands':
                        manage_target = None  # global mute/unmute
                    else:
                        manage_target = None
                        for g in self.command_groups:
                            if g['name'] == target_name:
                                manage_target = g['skill_prefix']
                                break
                    if self._is_duplicate(intent, now):
                        continue
                    self.interim_matcher.reset()
                    self.final_matcher.reset()
                    self._handle_manage_command(action, manage_target)
                    self._record_execution(intent, now, stream_name)
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
                if self._is_duplicate(utterance, now):
                    LOG.info(f"⏭️  Skipping duplicate: '{utterance}'")
                    continue

                # ── Dispatch ──────────────────────────────────────────────────
                self.emit('recognizer_loop:utterance', {
                    'utterances': [utterance],
                    'lang': self.lang,
                    'intent': intent
                })
                self.emit('mycroft.realtime.command_matched', {
                    'utterance': utterance,
                    'intent': intent,
                    'stream': stream_name
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
                self._record_execution(utterance, now, stream_name, matched_words,
                                       match_position, intent)

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
        """Override to skip STTFactory.create() — realtime uses Riva, not the configured STT module.

        The parent's start_async() calls STTFactory.create() which loads the plugin
        (ovos-stt-plugin-fasterwhisper) into VRAM immediately, even though realtime
        never uses it. We replicate the producer/consumer setup with a no-op STT.
        """
        from queue import Queue
        from mycroft.client.realtime.listener import AudioProducer, AudioConsumer, AudioStreamHandler

        LOG.info("RealtimeRecognizerLoop.start_async() called")
        try:
            self.state.running = True

            class _NoopSTT:
                lang = 'en-us'
                can_stream = False
                def execute(self, audio, language=None): return ''

            stt = _NoopSTT()
            queue = Queue()
            self.producer = AudioProducer(self.state, queue, self.microphone,
                                          self.responsive_recognizer, self, None)
            self.producer.start()
            self.consumer = AudioConsumer(self.state, queue, self,
                                          stt, self.wakeup_recognizer,
                                          self.wakeword_recognizer)
            self.consumer.start()
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
