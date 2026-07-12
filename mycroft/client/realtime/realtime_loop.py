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

import os
import subprocess
import time
import threading
from collections import deque

import numpy as np

from mycroft.client.realtime.listener import RecognizerLoop
from mycroft.client.realtime.mic import ResponsiveRecognizer
from mycroft.client.realtime.command_matcher import StreamingCommandMatcher
from mycroft.client.realtime.riva_streaming import RivaStreamingThread, RIVA_AVAILABLE
from mycroft.client.realtime.free_text_stt import load_free_text_stt
from mycroft.client.realtime.pattern_loader import FREE_TEXT_TAG, REPEAT_TAG, MANAGE_TAG, QA_TAG
from mycroft.configuration import Configuration
from mycroft.util.log import LOG
from mycroft.util.signal import get_ipc_directory
from mycroft.util import resolve_resource_file

# Non-terminal words: structural words that can't end a query.
# Silence after these gets the full session timeout rather than stop_silence_seconds.
# Configurable via free_text.non_terminal_words_extend / non_terminal_words_suppress.
_DEFAULT_NON_TERMINAL_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'of', 'to', 'for', 'at', 'in', 'on',
    'with', 'from', 'into', 'by', 'about', 'like'
}

# Words that cannot end a QA phrase — if the last transcribed word is one of these
# when the silence deadline fires, the commit is deferred and the deadline is extended.
# Kept intentionally narrow: only articles and conjunctions that essentially never
# end a natural sentence. Prepositions, possessives, intensifiers etc. are excluded
# because questions like "what is it about" / "is there more" end with them legitimately.
_QA_NON_COMMIT_TAIL_WORDS = {
    # articles
    'a', 'an', 'the',
    # coordinating conjunctions
    'and', 'or', 'but', 'nor', 'yet', 'so',
    # subordinating conjunctions
    'when', 'because', 'if', 'since', 'while', 'although', 'unless', 'where',
    'which', 'whether', 'though', 'whereas', 'until', 'whenever', 'wherever',
    'than', 'as',
}

# Operating modes
MODE_DETERMINISTIC = 'deterministic'
MODE_FREE_TEXT = 'free_text'
MODE_REPEAT = 'repeat'
MODE_QA = 'qa'


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

    def _record_phrase_qa(self, source, sec_per_buffer, max_wait_seconds=None):
        """VAD recording for QA follow-up, abortable by _qa_abort flag.

        Calls the grandparent ResponsiveRecognizer._record_phrase logic
        but checks realtime_loop._qa_abort each chunk to allow Riva's
        control-word detection to cut the recording short.

        max_wait_seconds: hard cap before any speech is detected (follow_up_timeout).
        Once speech starts, recording_timeout_with_silence controls the silence cutoff.
        """
        from mycroft.client.realtime.mic import NoiseTracker, get_silence
        from mycroft.util import check_for_signal

        loop = self.realtime_loop
        # silence_time_limit = max_wait_seconds: how long to wait for speech onset.
        # silence_after_loud = recording_timeout_with_silence: silence cutoff after speech ends.
        onset_wait = max_wait_seconds if max_wait_seconds is not None else self.recording_timeout_with_silence
        noise_tracker = NoiseTracker(0, 25, sec_per_buffer,
                                     self.MIN_LOUD_SEC_PER_PHRASE,
                                     onset_wait,
                                     self.recording_timeout_with_silence)
        timeout = max_wait_seconds if max_wait_seconds is not None else self.recording_timeout
        max_chunks = int(timeout / sec_per_buffer)
        num_chunks = 0
        byte_data = get_silence(source.SAMPLE_WIDTH)
        phrase_complete = False

        while num_chunks < max_chunks and not phrase_complete:
            if loop._qa_abort:
                break
            chunk = self.record_sound_chunk(source)
            byte_data += chunk
            num_chunks += 1

            # Feed to Riva so sentinel can detect control words in real time
            if loop.stt_backend == 'riva' and loop.riva_stream_thread:
                loop.riva_stream_thread.feed_audio(chunk)

            energy = self.calc_energy(chunk, source.SAMPLE_WIDTH)
            test_threshold = self.energy_threshold * self.multiplier
            is_loud = energy > test_threshold
            noise_tracker.update(is_loud)
            if not is_loud:
                self._adjust_threshold(energy, sec_per_buffer)

            phrase_complete = (noise_tracker.recording_complete() or
                               check_for_signal('buttonPress'))

            if num_chunks % 10 == 0:
                self._watchdog()
                self.write_mic_level(energy, source)

        return byte_data


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
        self._repeat_exempt_prefixes = set()  # skill prefixes that never open repeat window
        self._utterance_alias_map = {}        # word → canonical, merged across all skills

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

        # QA mode state
        self.qa_intent = None                   # intent name that triggered QA mode
        self.qa_config = {}                     # entity config (exit_words, interrupt_words, etc.)
        self._pending_qa = None                 # (intent_name, qa_config) set by Riva callback
        self._qa_exit_time = None               # time.time() when last QA session ended
        self._qa_tts_event = threading.Event()  # set when tts_done fires from skill
        self._qa_stt_loaded_by_mode = False     # True if QA mode called spawn/load (not preloaded)
        self._qa_config_map = {}                # tagged_intent -> qa_config, populated at pattern load
        self._qa_abort = False                  # set by Riva callback when exit/interrupt word detected
        self._qa_abort_reason = None            # 'exit' or 'interrupt'
        self._qa_tts_playing = False            # True while TTS is active — sentinel suppressed during this
        # Riva FINAL accumulator for silence-based phrase commit
        self._qa_phrase = ""                    # growing transcript for current question
        self._qa_last_final = ""               # last FINAL seen, for diffing
        self._qa_committed_event = threading.Event()  # set when phrase is committed
        self._qa_committed_text = None         # the committed phrase text
        self._qa_trigger_words = []            # entry phrase words to strip from first FINAL
        self._qa_listen_deadline = 0.0         # reset on each FINAL so silence gap triggers commit
        self._qa_tts_grace_until = 0.0         # epoch time until which sentinel+accumulation are suppressed

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
        self.qa_intent = None
        self.qa_config = {}
        self._pending_qa = None
        self._qa_tts_event.clear()
        self._qa_abort = False
        self._qa_abort_reason = None
        self._qa_tts_playing = False
        self._qa_phrase = ""
        self._qa_last_final = ""
        self._qa_committed_event.clear()
        self._qa_committed_text = None

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
            self._open_repeat_window(intent=self.last_executed_command[1])

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

            # If Riva callback requested QA mode, handle it here on the loop thread.
            if self._pending_qa is not None:
                intent_name, qa_config, trigger_utterance = self._pending_qa
                self._pending_qa = None
                self._do_qa_session(intent_name, qa_config, source, sec_per_buffer, trigger_utterance=trigger_utterance)
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

    def _open_repeat_window(self, intent=None):
        """Open the repeat window unless the triggering intent is from an exempt skill."""
        if not self.repeat_enabled:
            return
        if intent and self._repeat_exempt_prefixes:
            skill_prefix = intent.partition(':')[0]
            if skill_prefix in self._repeat_exempt_prefixes:
                LOG.info(f"Repeat window suppressed — '{skill_prefix}' is repeat-exempt")
                return
        self.repeat_window_open = True
        self.interim_matcher.repeat_window_open = True
        self.final_matcher.repeat_window_open = True
        LOG.info(f"Repeat window opened ({self.repeat_window_seconds}s)")

    def _close_repeat_window(self):
        """Remove quantifier first-words from the transient valid-first-word set."""
        self.repeat_window_open = False
        self.interim_matcher.repeat_window_open = False
        self.final_matcher.repeat_window_open = False
        LOG.info("Repeat window closed")

    # ── Command group management ──────────────────────────────────────────────

    def _normalize_utterance(self, key):
        """Rewrite any skill-declared alias words to their canonical form."""
        if not self._utterance_alias_map:
            return key
        return ' '.join(self._utterance_alias_map.get(w, w) for w in key.split())

    def _is_duplicate(self, key, now):
        """Check executed_utterances_history for a recent exact or alias match."""
        key = self._normalize_utterance(key)
        for entry in self.executed_utterances_history:
            if len(entry) < 3:
                continue
            prev_key = self._normalize_utterance(entry[0])
            prev_time = entry[1]
            elapsed = now - prev_time
            if key == prev_key and elapsed < self.dedup_window_seconds:
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
        # ── Audio device wake (play ding non-blocking) ────────────────────────
        if action == 'audio' and target == 'wake':
            snd = resolve_resource_file('snd/start_listening.wav')
            if snd:
                subprocess.Popen(['paplay', snd])
                LOG.info("[MANAGE] audio:wake — played ding")
            else:
                LOG.warning("[MANAGE] audio:wake — start_listening.wav not found")
            return

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

        LOG.info(f"🎙️  FREE-TEXT MODE: intent='{intent_name}' prefix='{str(len(trigger_utterance))}'")
        self.emit('mycroft.realtime.mode_changed', {
            'mode': 'free_text',
            'trigger': str(len(trigger_utterance)),
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
                 f"(intent: {str(len(self.free_text_intent))})")

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
        LOG.info(f"✓ FREE-TEXT COMMAND: '{str(len(full_utterance))}' (intent: {str(len(self.free_text_intent))})")

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

    # ── QA mode ───────────────────────────────────────────────────────────────

    def _enter_qa_mode(self, intent_name, qa_config, trigger_utterance=""):
        """Request QA mode from the Riva callback thread.

        Same deferred pattern as _enter_free_text_mode — sets a flag and stops
        Riva; the loop thread picks it up and calls _do_qa_session exclusively.
        """
        if self.mode == MODE_QA or self._pending_qa is not None:
            return

        self.mode = MODE_QA
        self.qa_intent = intent_name
        self.qa_config = qa_config
        self._qa_tts_event.clear()
        self._qa_abort = False
        self._qa_abort_reason = None

        LOG.info(f"🗣️  QA MODE: intent='{intent_name}' trigger='{trigger_utterance}'")
        self.emit('mycroft.realtime.mode_changed', {'mode': 'qa', 'intent': intent_name})

        # Keep Riva running — it acts as sentinel for exit/interrupt words.
        # Reset it so it starts fresh for QA mode word detection.
        if self.stt_backend == 'riva' and self.riva_stream_thread:
            self.riva_stream_thread.reset()
            self.riva_interim_word_count = 0
            self.riva_final_word_count = 0

        # Pre-spawn secondary STT if not already loaded
        self._qa_stt_loaded_by_mode = False
        if self.free_text_stt:
            if not self.free_text_stt.is_loaded:
                self._qa_stt_loaded_by_mode = True
            self.free_text_stt.spawn(self._stt_sample_rate)

        self._pending_qa = (intent_name, qa_config, trigger_utterance)

    def _handle_qa_riva_words(self, words, is_final):
        """Riva sentinel handler during QA mode.

        Single exit/interrupt words fire on INTERIM for instant response.
        Phrases require FINAL for accuracy.
        Sets _qa_abort / _qa_abort_reason and unblocks the TTS wait event
        so _do_qa_session can react immediately.
        """
        if not words:
            return
        # Suppress sentinel while TTS is active — Riva hears TTS output through mic.
        # Use isSpeaking IPC signal: backend-agnostic, set by TTS.execute(), cleared by end_audio().
        speaking_path = os.path.join(get_ipc_directory(), 'signal', 'isSpeaking')
        if os.path.isfile(speaking_path):
            return
        if self._qa_tts_grace_until and time.time() < self._qa_tts_grace_until:
            return  # TTS tail grace period — discard mic pickup of speaker output
        if self._qa_abort:
            return  # already aborted, don't double-fire

        cfg = self.qa_config
        exit_words = set(cfg.get('exit_words', ['stop', 'done']))
        exit_phrases = [p.lower() for p in cfg.get('exit_phrases', ["that's all"])]
        interrupt_words = set(cfg.get('interrupt_words', ['wait']))
        interrupt_phrases = [p.lower() for p in cfg.get('interrupt_phrases', ['hang on'])]
        max_pos = cfg.get('control_word_positions', 3)

        transcript = ' '.join(words).lower()
        check_words = [w.lower().rstrip('.,!?') for w in words[:max_pos]]

        # Single words: fire on INTERIM for instant response (distinctive enough to be safe)
        for w in check_words:
            if w in exit_words:
                LOG.info(f"QA sentinel: exit word '{w}' detected ({'INTERIM' if not is_final else 'FINAL'})")
                self.emit('mycroft.realtime.qa_debug', {'msg': f"exit word: '{w}' ({'interim' if not is_final else 'final'})"})
                self._qa_abort = True
                self._qa_abort_reason = 'exit'
                self.emit('mycroft.audio.speech.stop', {})
                self._qa_tts_event.set()
                self._qa_committed_event.set()
                return
        for w in check_words:
            if w in interrupt_words:
                LOG.info(f"QA sentinel: interrupt word '{w}' detected ({'INTERIM' if not is_final else 'FINAL'})")
                self.emit('mycroft.realtime.qa_debug', {'msg': f"interrupt word: '{w}' ({'interim' if not is_final else 'final'})"})
                self._qa_abort = True
                self._qa_abort_reason = 'interrupt'
                self.emit('mycroft.audio.speech.stop', {})
                self._qa_tts_event.set()
                self._qa_committed_event.set()
                return

        # Phrases: FINAL only for reliability
        if not is_final:
            return
        for phrase in exit_phrases:
            if transcript.startswith(phrase):
                LOG.info(f"QA sentinel: exit phrase '{phrase}' detected by Riva")
                self.emit('mycroft.realtime.qa_debug', {'msg': f"exit phrase: '{phrase}'"})
                self._qa_abort = True
                self._qa_abort_reason = 'exit'
                self.emit('mycroft.audio.speech.stop', {})
                self._qa_tts_event.set()
                self._qa_committed_event.set()
                return
        for phrase in interrupt_phrases:
            if transcript.startswith(phrase):
                LOG.info(f"QA sentinel: interrupt phrase '{phrase}' detected by Riva")
                self._qa_abort = True
                self._qa_abort_reason = 'interrupt'
                self.emit('mycroft.audio.speech.stop', {})
                self._qa_tts_event.set()
                self._qa_committed_event.set()
                return

    def _qa_accumulate_final(self, transcript):
        """Called from Riva callback on each FINAL during QA listening phase.

        Diffs against last FINAL to find new words, appends to _qa_phrase,
        and resets the silence deadline so the commit only fires after the user
        has actually stopped speaking. Commit decision is made in _do_qa_session
        when the deadline expires with a complete-looking tail word.
        """
        new_words = transcript.split()

        # Strip everything up to and including the trigger phrase from the FINAL.
        # Handles wakeword tail ("puter" from "computer") + trigger phrase in one pass.
        # Keeps trying on each FINAL until the trigger words appear — once found,
        # everything before and including them is discarded and accumulation starts
        # from what follows. Only applies once per QA session.
        if self._qa_trigger_words:
            trigger_len = len(self._qa_trigger_words)
            lower_words = [w.lower().rstrip('.,!?') for w in new_words]
            for i in range(len(lower_words) - trigger_len + 1):
                if lower_words[i:i + trigger_len] == self._qa_trigger_words:
                    new_words = new_words[i + trigger_len:]
                    LOG.info(f"QA accumulator: stripped trigger at pos {i}, remaining: {new_words}")
                    self._qa_trigger_words = []
                    break
            else:
                # Trigger phrase not found yet — discard this FINAL entirely
                LOG.info(f"QA accumulator: trigger not found yet, discarding: {new_words}")
                return

        # Each FINAL means the user is still talking — reset the silence deadline
        if self._qa_listen_deadline:
            self._qa_listen_deadline = time.time() + self.qa_config.get('follow_up_timeout_seconds', 15)

        old_words = self._qa_last_final.split()
        if new_words[:len(old_words)] == old_words:
            added = " ".join(new_words[len(old_words):])
        else:
            added = " ".join(new_words)  # Riva session reset — treat all as new

        self._qa_last_final = " ".join(new_words)

        if not added:
            return

        self._qa_phrase = (self._qa_phrase + " " + added).strip()
        LOG.info(f"QA accumulator: phrase='{self._qa_phrase}' ({len(self._qa_phrase.split())} words)")

        cfg = self.qa_config

        # Check for override commit phrases — user explicitly signals they're done.
        # Strip the commit phrase from the end before dispatching.
        commit_phrases = [p.lower() for p in cfg.get('commit_phrases', [])]
        phrase_lower = self._qa_phrase.lower()
        for cp in commit_phrases:
            if phrase_lower.endswith(cp):
                cleaned = self._qa_phrase[:-(len(cp))].strip().rstrip(',')
                if cleaned:
                    LOG.info(f"QA override commit: '{cleaned}' (trigger: '{cp}')")
                    self.emit('mycroft.realtime.qa_debug', {'msg': f"override commit: '{cleaned}'"})
                    self._qa_committed_text = cleaned
                    self._qa_phrase = ""
                    self._qa_last_final = ""
                    self._qa_committed_event.set()
                return

        # Commit on FINAL if the last word is not a non-commit tail word.
        # The silence deadline in _do_qa_session is a fallback only — the primary
        # commit signal is each FINAL arriving with a complete-looking tail word.
        last_word = self._qa_phrase.split()[-1].lower().rstrip('.,!?')
        non_commit_extra = cfg.get('non_commit_tail_words', [])
        non_commit_set = _QA_NON_COMMIT_TAIL_WORDS | set(non_commit_extra)
        if last_word not in non_commit_set:
            LOG.info(f"QA accumulator: FINAL commit on '{last_word}' — '{self._qa_phrase}'")
            self.emit('mycroft.realtime.qa_debug', {'msg': f"final commit: '{self._qa_phrase}'"})
            self._qa_committed_text = self._qa_phrase
            self._qa_phrase = ""
            self._qa_last_final = ""
            self._qa_committed_event.set()
        else:
            LOG.info(f"QA accumulator: tail '{last_word}' is non-commit — waiting for more")

    def _do_qa_session(self, intent_name, qa_config, source, sec_per_buffer, trigger_utterance=""):
        """Run the QA follow-up loop on the loop thread.

        Flow per question:
          1. Accumulate Riva FINALs; commit when silence deadline fires with a complete tail word
          2. Dispatch committed phrase to skill
          3. Skill speaks via blocking subprocess, then emits question-answerer-skill:tts_done
          4. Wait for tts_done — that's the hard signal audio is finished
          5. Repeat from 1 until exit word or follow-up timeout
        """
        follow_up_timeout = qa_config.get('follow_up_timeout_seconds', 15)
        tts_safety_cap = qa_config.get('tts_safety_cap_seconds', 60)
        tts_grace_seconds = qa_config.get('tts_grace_seconds', 1.5)

        # Store trigger words so _qa_accumulate_final can strip them from the first FINAL
        self._qa_trigger_words = trigger_utterance.lower().split()

        # Reset accumulator state for this session
        self._qa_phrase = ""
        self._qa_last_final = ""
        self._qa_committed_event.clear()
        self._qa_committed_text = None
        self._qa_tts_event.clear()

        if self.stt_backend == 'riva' and self.riva_stream_thread:
            self.riva_stream_thread.reset()
            self.riva_interim_word_count = 0
            self.riva_final_word_count = 0

        LOG.info(f"QA: session started — listening via Riva FINAL silence (trigger: '{trigger_utterance}')")
        snd = resolve_resource_file('snd/start_listening.wav')
        if snd:
            subprocess.Popen(['paplay', snd])
        os.system(r"/home/joseph/.local/bin/polybar-flash dunst FFA500 &")

        while self.mode == MODE_QA and self.session_active:
            self._qa_abort = False
            self._qa_abort_reason = None
            self._qa_committed_event.clear()
            self._qa_committed_text = None
            self._qa_phrase = ""
            self._qa_last_final = ""

            LOG.info(f"QA: waiting for phrase (timeout={follow_up_timeout}s)")

            # Feed audio to Riva while waiting for the user to finish speaking.
            # _qa_listen_deadline resets on each FINAL so the timeout only fires
            # after follow_up_timeout seconds of silence (no new FINALs arriving).
            self._qa_listen_deadline = time.time() + follow_up_timeout
            while not self._qa_committed_event.is_set() and not self._qa_abort:
                if time.time() > self._qa_listen_deadline:
                    # Silence deadline fired — decide whether to commit or defer.
                    phrase = self._qa_phrase.strip()
                    if not phrase:
                        # No speech at all — exit QA
                        break
                    last_word = phrase.split()[-1].lower().rstrip('.,!?')
                    non_commit_extra = self.qa_config.get('non_commit_tail_words', [])
                    non_commit_set = _QA_NON_COMMIT_TAIL_WORDS | set(non_commit_extra)
                    if last_word in non_commit_set:
                        # Phrase ends with an incomplete-sentence word — extend deadline
                        # and wait for more speech rather than committing a fragment.
                        LOG.info(f"QA: silence deadline fired but tail word '{last_word}' is non-commit — extending")
                        self.emit('mycroft.realtime.qa_debug', {'msg': f"tail '{last_word}' non-commit, extending"})
                        self._qa_listen_deadline = time.time() + follow_up_timeout
                        continue
                    # Good tail word — commit on silence
                    LOG.info(f"QA: silence deadline — committing '{phrase}'")
                    self.emit('mycroft.realtime.qa_debug', {'msg': f"silence commit: '{phrase}'"})
                    self._qa_committed_text = phrase
                    self._qa_phrase = ""
                    self._qa_last_final = ""
                    self._qa_committed_event.set()
                    break
                chunk = self.responsive_recognizer.record_sound_chunk(source)
                if self.stt_backend == 'riva' and self.riva_stream_thread:
                    self.riva_stream_thread.feed_audio(chunk)

            committed = self._qa_committed_event.is_set()

            if not committed:
                # Timeout — no phrase received in follow_up_timeout seconds
                LOG.info("QA: follow-up timeout — no speech committed, leaving QA mode")
                break

            if self._qa_abort:
                reason = self._qa_abort_reason
                self._qa_abort = False
                self._qa_abort_reason = None
                if reason == 'exit':
                    LOG.info("QA: exit word detected — leaving QA mode")
                    self.emit('mycroft.realtime.mode_changed', {'mode': 'qa_exit', 'reason': 'riva_exit_word'})
                    break
                elif reason == 'interrupt':
                    LOG.info("QA: interrupt word detected — reopening follow-up")
                    continue

            text = self._qa_committed_text
            if not text:
                continue

            LOG.info(f"QA committed: '{text}'")

            # Mark TTS as playing so sentinel suppresses while skill is responding
            self._qa_tts_playing = True
            self._qa_tts_event.clear()

            # Dispatch to skill — skill will call _speak_blocking then emit tts_done
            self.emit('recognizer_loop:utterance', {
                'utterances': [text],
                'lang': self.lang,
                'intent': intent_name
            })
            self.emit('mycroft.realtime.command_matched', {
                'utterance': text,
                'intent': intent_name,
                'stream': 'QA'
            })

            # Feed audio to Riva while waiting for TTS to finish so the sentinel
            # can detect exit/interrupt words during playback.
            LOG.info("QA: waiting for TTS to complete (feeding Riva for sentinel)")
            tts_deadline = time.time() + tts_safety_cap
            while not self._qa_tts_event.is_set() and not self._qa_abort:
                if time.time() > tts_deadline:
                    LOG.warning("QA: TTS safety cap exceeded")
                    break
                chunk = self.responsive_recognizer.record_sound_chunk(source)
                if self.stt_backend == 'riva' and self.riva_stream_thread:
                    self.riva_stream_thread.feed_audio(chunk)
            tts_done = self._qa_tts_event.is_set()
            self._qa_tts_playing = False

            if not tts_done:
                LOG.warning("QA: TTS safety cap exceeded — continuing anyway")

            # Handle abort that arrived during TTS.
            # "stop" during TTS = kill playback (already done by _stop_tts) then
            # open a fresh follow-up window — the user wants a different answer, not to exit QA.
            if self._qa_abort:
                reason = self._qa_abort_reason
                if reason == 'exit':
                    LOG.info("QA: exit word during TTS — killing TTS, opening next follow-up")
                    self.emit('mycroft.realtime.qa_debug', {'msg': 'exit during TTS → next follow-up'})
                    # Reset Riva BEFORE clearing _qa_abort so the sentinel's early-return
                    # guard stays active while the reset flushes any in-flight words.
                    if self.stt_backend == 'riva' and self.riva_stream_thread:
                        self.riva_stream_thread.reset()
                        self.riva_interim_word_count = 0
                        self.riva_final_word_count = 0
                    self._qa_abort = False
                    self._qa_abort_reason = None
                    self.last_word_time = time.time()
                    self._qa_tts_grace_until = time.time() + tts_grace_seconds
                    continue
                self._qa_abort = False
                self._qa_abort_reason = None

            LOG.info("QA: TTS done — grace period then opening next follow-up window")
            self.last_word_time = time.time()
            self._qa_tts_grace_until = time.time() + tts_grace_seconds

            # Drain Riva during grace period — keeps stream healthy, discards any
            # mic pickup of TTS tail before the listening window opens
            grace_end = self._qa_tts_grace_until
            if self.stt_backend == 'riva' and self.riva_stream_thread:
                self.riva_stream_thread.reset()
                self.riva_interim_word_count = 0
                self.riva_final_word_count = 0
            while time.time() < grace_end:
                chunk = self.responsive_recognizer.record_sound_chunk(source)
                if self.stt_backend == 'riva' and self.riva_stream_thread:
                    self.riva_stream_thread.feed_audio(chunk)
            self._qa_tts_grace_until = 0.0

        # Exit QA mode — reset everything and resume DETERMINISTIC
        snd = resolve_resource_file('snd/start_listening.wav')
        if snd:
            subprocess.Popen(['paplay', snd])
        os.system(r"/home/joseph/.local/bin/polybar-flash dunst FFA500 &")
        self.mode = MODE_DETERMINISTIC
        self.qa_intent = None
        self.qa_config = {}
        self._qa_tts_event.clear()
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

        self._qa_exit_time = time.time()
        LOG.info("QA mode complete — resumed DETERMINISTIC mode, Riva restarted")
        self.emit('mycroft.realtime.mode_changed', {'mode': 'deterministic'})

    def _on_qa_tts_done(self):
        """Called when question-answerer-skill:tts_done fires — subprocess paplay has returned."""
        self._qa_tts_playing = False
        self._qa_tts_event.set()
        self.emit('mycroft.realtime.qa_debug', {'msg': 'TTS done — mic open'})

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

        LOG.info(f"🔁 REPEAT MODE: '{str(len(utterance))}' × {additional} more time(s) (n={n})")
        self.emit('mycroft.realtime.mode_changed', {
            'mode': 'repeat',
            'utterance': utterance,
            'n': additional
        })

        now = time.time()
        for i in range(additional):
            LOG.info(f"  🔁 Repeat {i+1}/{additional}: '{str(len(utterance))}'")
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
            LOG.info(f"[WHISPER] {str(len(word))} (latency: {latency*1000:.0f}ms from last batch)")
        else:
            LOG.info(f"[WHISPER] {str(len(word))}")

        self.last_word_time = now

        if self.debug:
            self.emit('mycroft.debug.whisper.partial', {'utterance': word})

        match, _ = self.interim_matcher.add_word(word)

        if match:
            utterance = match['utterance']
            LOG.info(f"✓ COMMAND MATCHED: '{str(len(utterance))}' (intent: {match['intent']})")
            self.emit('recognizer_loop:utterance', {
                'utterances': [utterance],
                'lang': self.lang
            })
            if self.debug:
                self.emit('mycroft.debug.whisper.final', {'utterance': utterance})
            self.interim_matcher.reset()
            self.shared_global_budget = self.interim_matcher.global_budget

        if self.interim_matcher.global_budget <= 0 and self.mode != MODE_QA:
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

        # During FREE_TEXT mode Riva is stopped — ignore any words that slip through.
        if self.mode == MODE_FREE_TEXT:
            return

        # During QA mode: check exit/interrupt words AND accumulate FINALs for
        # phrase accumulation and silence-based commit.
        if self.mode == MODE_QA:
            self._handle_qa_riva_words(words, is_final)
            grace_active = self._qa_tts_grace_until and time.time() < self._qa_tts_grace_until
            if not self._qa_abort and not self._qa_tts_playing and not grace_active:
                # INTERIM: keep the silence deadline alive while the user is speaking
                if not is_final and self._qa_listen_deadline and words:
                    self._qa_listen_deadline = time.time() + self.qa_config.get('follow_up_timeout_seconds', 15)
                # FINAL: accumulate and potentially commit
                if is_final:
                    self._qa_accumulate_final(" ".join(words))
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
            is_identical = (transcript == prev_transcript)

            if is_identical:
                LOG.info(f"[RIVA {stream_name}] Spurious re-delivery (identical transcript) — ignoring")
                return
            elif is_refinement:
                LOG.info(f"[RIVA {stream_name}] Refinement: "
                         f"prev='{str(len(prev_transcript))}' new='{str(len(transcript))}' — resetting matcher")
                matcher.reset()
            else:
                LOG.info(f"[RIVA {stream_name}] True segment boundary: "
                         f"prev='{str(len(prev_transcript))}' new='{str(len(transcript))}' — "
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
                         f"{str(len(prev_words))} → {str(len(curr_words))} | "
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
                                     f"execution — removed stale dedup entry: '{str(len(last_utterance))}' "
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
                                            LOG.debug(f"Replacement word '{str(len(replacement_word))}' "
                                                      f"not valid — keeping dedup protection")
                                            break
                                    if replacement_all_valid:
                                        should_undo = True
                                        LOG.info(f"⚠️  Matched words changed {time_since_last:.1f}s "
                                                 f"after execution at position {last_match_position}: "
                                                 f"{str(len(last_matched_words))} → {str(len(current_matched_words))} — "
                                                 f"removed stale dedup entry: '{str(len(last_utterance))}'")
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

                # Slot buffers on surviving paths may contain words from the changed
                # region — clear them so replay re-enters slots cleanly.
                matcher.clear_slot_state_all_paths()

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
            LOG.debug(f"[RIVA {stream_name}] New words: {str(len(new_words))} (processed {current_count}/{len(words)})")

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

            LOG.debug(f"[RIVA {stream_name}] {str(len(word))}")

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
                LOG.info(f"✓ COMMAND MATCHED ({stream_name}): '{str(len(utterance))}' (intent: {str(len(intent))})")

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

                # ── QA trigger ────────────────────────────────────────────────
                if intent.startswith(QA_TAG):
                    original_intent = intent[len(QA_TAG):]
                    qa_cfg = self._qa_config_map.get(intent, {})
                    self._enter_qa_mode(original_intent, qa_cfg, utterance)
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
                        LOG.warning(f"Invalid repeat intent tag: {str(len(intent))}")
                        continue
                    # Bare quantifier outside window — skip without resetting matchers
                    # so other active paths (e.g. calc slots containing "times") survive
                    if is_bare and not self.repeat_window_open:
                        LOG.info(f"⏩ Bare repeat '{utterance}' outside window — skipping, leaving paths active")
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
                    if action in ('stt', 'audio'):
                        # STT load/unload and audio:wake — pass target_name directly
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
                            LOG.info(f"⛔ FINAL cross-command stitch rejected: '{str(len(utterance))}' "
                                     f"skipped over command-starting word(s) {str(len(_stitch_words))}")
                            continue

                # ── Deduplication ─────────────────────────────────────────────
                if self._is_duplicate(utterance, now):
                    LOG.info(f"⏭️  Skipping duplicate: '{str(len(utterance))}'")
                    continue

                # ── Dispatch ──────────────────────────────────────────────────
                self.emit('recognizer_loop:utterance', {
                    'utterances': [utterance],
                    'lang': self.lang,
                    'intent': intent,
                    'entities': match.get('entities', {}),
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
                    self._open_repeat_window(intent=intent)

                LOG.debug(f"Both matchers reset after {stream_name} match")
                break

        # FINAL boundary: close any open number slots and check for completion.
        # Number slots stay open during word processing to accumulate greedily;
        # when the FINAL stream ends we know the user has stopped speaking.
        if is_final:
            for path in list(matcher.active_paths):
                if (path._number_slot_name is not None or path._calc_slot_name is not None
                        or (path._had_slot and not path._slot_closed_by_word)):
                    match = path.check_completion()
                    if match:
                        utterance = match['utterance']
                        intent = match['intent']
                        LOG.info(f"✓ COMMAND MATCHED (FINAL number close): '{utterance}' (intent: {intent})")
                        if not self._is_duplicate(utterance, now):
                            self.emit('recognizer_loop:utterance', {
                                'utterances': [utterance],
                                'lang': self.lang,
                                'intent': intent,
                                'entities': match.get('entities', {}),
                            })
                            self.emit('mycroft.realtime.command_matched', {
                                'utterance': utterance,
                                'intent': intent,
                                'stream': 'FINAL'
                            })
                            self._record_execution(utterance, now, 'FINAL',
                                                   utterance.split(), None, intent)
                            if self.repeat_enabled:
                                self._open_repeat_window(intent=intent)
                        self.shared_global_budget = path.local_budget
                        matcher.global_budget = path.local_budget
                        self.interim_matcher.reset()
                        self.final_matcher.reset()
                        break

        # Budget sync after processing all words
        self.shared_global_budget = matcher.global_budget

        # Budget exhaustion ends the session in DETERMINISTIC mode only —
        # in QA mode Riva runs as sentinel and words accumulating here should
        # not kill the session.
        if self.shared_global_budget <= 0 and self.mode != MODE_QA:
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
            LOG.info(f"✓ WHISPER result: {str(len(whisper_text))}")
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
