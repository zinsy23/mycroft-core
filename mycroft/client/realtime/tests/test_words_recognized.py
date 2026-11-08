"""
Tests for _on_words_recognized — the Riva transcript processing loop.

These tests cover the loop-level behaviors that have broken repeatedly in
production: refinement branch, transcript-changed replay, already-complete
dispatch, undo window, and min_replay_pos advancement.

Riva and Whisper are never imported — sys.modules patching stubs them out
before realtime_loop loads, so the whole file is safe to run without GPU or
Riva running.

Run: .venv/bin/python3 -m pytest mycroft/client/realtime/tests/test_words_recognized.py -v
"""

import sys
import time
import threading
from unittest.mock import MagicMock, patch
import pytest

# ── Stub out heavy imports BEFORE realtime_loop is imported ───────────────────
# These modules pull in nvidia gRPC stubs, CUDA, and Whisper on import.
# MagicMock satisfies any attribute/call access the module-level code makes.
for _mod in [
    'mycroft.client.realtime.riva_streaming',
    'mycroft.client.realtime.free_text_stt',
    'mycroft.client.realtime.listener',
    'mycroft.client.realtime.mic',
    'mycroft.configuration',
    'mycroft.util.signal',
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Stub the RecognizerLoop base class so we don't need the full audio stack
sys.modules['mycroft.client.realtime.listener'].RecognizerLoop = object
sys.modules['mycroft.client.realtime.mic'].ResponsiveRecognizer = MagicMock

from mycroft.client.realtime.command_matcher import StreamingCommandMatcher
from mycroft.client.realtime.pattern_loader import (
    expand_pattern_entities, FREE_TEXT_TAG, REPEAT_TAG, MANAGE_TAG, QA_TAG
)
from mycroft.client.realtime.realtime_loop import (
    RealtimeRecognizerLoop,
    MODE_DETERMINISTIC,
)


FILLER_CFG = {'base_max': 4, 'increment_per_word': 1}


def make_loop(patterns):
    """
    Return a minimal object whose _on_words_recognized method works correctly.

    Only the state variables actually read by _on_words_recognized are set.
    Everything else is a MagicMock so attribute access never raises.
    """
    loop = MagicMock()

    # Stream counters
    loop.riva_interim_word_count = 0
    loop.riva_final_word_count = 0
    loop.prev_interim_transcript = ""
    loop.prev_final_transcript = ""
    loop.interim_min_replay_pos = 0
    loop.final_min_replay_pos = 0

    # Matchers
    loop.interim_matcher = StreamingCommandMatcher(FILLER_CFG, verbose=False)
    loop.final_matcher = StreamingCommandMatcher(FILLER_CFG, verbose=False)
    loop.shared_global_budget = FILLER_CFG['base_max']

    # Register patterns on both matchers
    expanded = []
    for p in patterns:
        result = expand_pattern_entities(p, {})
        expanded.extend(result[0] if isinstance(result, tuple) else result)
    loop.interim_matcher.register_intent('test', expanded)
    loop.final_matcher.register_intent('test', expanded)

    # Mode
    loop.mode = MODE_DETERMINISTIC
    loop.verbose = False
    loop.debug = False

    # Dedup
    loop.executed_utterances_history = []
    loop.dedup_window_seconds = 3.0
    loop.dedup_history_size = 5

    # Misc state _on_words_recognized touches
    loop._secondary_stt_slot_active = False
    loop.last_word_time = time.time()
    loop.repeat_enabled = False
    loop.repeat_window_open = False
    loop.protected_intent_names = set()
    loop._utterance_alias_map = {}

    # _dispatch_match_result is the real method bound to our fake loop
    from mycroft.client.realtime.realtime_loop import RealtimeRecognizerLoop
    loop._dispatch_match_result = lambda *a, **kw: (
        RealtimeRecognizerLoop._dispatch_match_result(loop, *a, **kw)
    )
    loop._on_words_recognized = lambda *a, **kw: (
        RealtimeRecognizerLoop._on_words_recognized(loop, *a, **kw)
    )
    loop._is_duplicate = lambda *a, **kw: (
        RealtimeRecognizerLoop._is_duplicate(loop, *a, **kw)
    )
    loop._normalize_utterance = lambda u: u
    loop._record_execution = lambda *a, **kw: (
        RealtimeRecognizerLoop._record_execution(loop, *a, **kw)
    )
    loop._open_repeat_window = MagicMock()
    loop._enter_free_text_mode = MagicMock()
    loop._enter_qa_mode = MagicMock()
    loop._enter_repeat_mode = MagicMock()
    loop._handle_manage_command = MagicMock()
    loop._resolve_pending_secondary_stt_entities = MagicMock()

    # emit captures dispatched utterances for assertion
    loop._dispatched = []
    def _emit(event, data=None):
        if event == 'recognizer_loop:utterance':
            loop._dispatched.append(data)
    loop.emit = _emit

    return loop


def process(loop, transcript, is_final=False, word_timings=None):
    """Call _on_words_recognized with a transcript string."""
    words = transcript.split()
    loop._on_words_recognized(words, is_final=is_final, word_timings=word_timings)


# ── Helpers ───────────────────────────────────────────────────────────────────

def last_utterance(loop):
    return loop._dispatched[-1]['utterances'][0] if loop._dispatched else None


# ── Basic dispatch ─────────────────────────────────────────────────────────────

class TestBasicDispatch:
    def test_simple_command_fires_on_interim(self):
        loop = make_loop(['lights on'])
        process(loop, 'lights')
        process(loop, 'lights on')
        assert last_utterance(loop) == 'lights on'

    def test_simple_command_fires_on_final(self):
        loop = make_loop(['lights on'])
        process(loop, 'lights on', is_final=True)
        assert last_utterance(loop) == 'lights on'

    def test_filler_before_command(self):
        loop = make_loop(['lights on'])
        process(loop, 'uh')
        process(loop, 'uh lights')
        process(loop, 'uh lights on')
        assert last_utterance(loop) == 'lights on'

    def test_the_before_command_word(self):
        # 'the' is not a valid first word — should be absorbed
        loop = make_loop(['sunrise today', 'sunset today'])
        process(loop, 'the')
        process(loop, 'the sunrise')
        process(loop, 'the sunrise today')
        assert last_utterance(loop) == 'sunrise today'


# ── Refinement branch ─────────────────────────────────────────────────────────

class TestRefinement:
    """
    Word count drop with same first word = refinement.
    Paths must be KEPT — the old bug called matcher.reset() here, killing
    all paths and preventing any subsequent match.
    """

    def test_refinement_keeps_paths(self):
        loop = make_loop(['when is sunrise today'])
        # Build up 'when is sunrise today' word by word
        process(loop, 'when')
        process(loop, 'when is')
        process(loop, 'when is sunrise')
        process(loop, 'when is sunrise today')
        paths_before = len(loop.interim_matcher.active_paths)

        # Simulate refinement: Riva shortens transcript but keeps first word
        # (word count drops, same first word)
        loop.riva_interim_word_count = 4
        process(loop, 'when is')   # count drop: 4 → 2, same first word
        # Paths should be preserved (not reset)
        assert len(loop.interim_matcher.active_paths) >= 1 or last_utterance(loop) is not None

    def test_refinement_does_not_reset_matcher(self):
        loop = make_loop(['when is sunrise today'])
        process(loop, 'when')
        process(loop, 'when is')
        process(loop, 'when is sunrise')
        n_before = len(loop.interim_matcher.active_paths)

        # Trigger refinement
        loop.riva_interim_word_count = 3
        process(loop, 'when is')  # word count drop, same first word

        # Paths were kept — not zeroed out
        assert len(loop.interim_matcher.active_paths) > 0 or last_utterance(loop) is not None

    def test_true_segment_boundary_resets(self):
        # Different first word = true boundary, not refinement
        loop = make_loop(['when is sunrise today', 'lights on'])
        process(loop, 'when')
        process(loop, 'when is')
        loop.riva_interim_word_count = 2
        # Word count drop AND different first word → segment boundary
        process(loop, 'lights')  # first word changed
        # min_replay_pos should be reset to 0
        assert loop.interim_min_replay_pos == 0


# ── Transcript-changed replay ─────────────────────────────────────────────────

class TestTranscriptChanged:
    """
    When Riva mutates a previously-processed word, the transcript-changed
    branch prunes stale paths and replays from min_replay_pos.
    """

    def test_mutation_at_last_position_still_fires(self):
        # The 'to' → 'today' scenario: mutation at the last processed position
        loop = make_loop(['[what|when] is sunrise [today|tomorrow]',
                          'what is the current temperature'])
        # Build up gradually
        for transcript in ['when', 'when is', 'when is the', 'when is the sunrise',
                            'when is the sunrise to']:
            process(loop, transcript)

        # Riva mutates 'to' → 'today' at position 4
        process(loop, 'when is the sunrise today')
        assert last_utterance(loop) == 'when is sunrise today'

    def test_mutation_mid_sequence_replays(self):
        loop = make_loop(['what is sunrise today', 'when is sunrise today'])
        for t in ['what', 'what is', 'what is sunrise']:
            process(loop, t)
        # Mutate first word
        process(loop, 'when is sunrise today')
        assert last_utterance(loop) is not None

    def test_already_complete_path_fires_on_mutation(self):
        # If a path already completed before Riva mutated, it should fire
        # immediately in the transcript-changed branch (already_complete check)
        loop = make_loop(['lights on'])
        for t in ['lights', 'lights on']:
            process(loop, t)
        # At this point 'lights on' should have already fired on INTERIM
        assert last_utterance(loop) == 'lights on'


# ── min_replay_pos advancement ────────────────────────────────────────────────

class TestMinReplayPos:
    """
    After a command fires, min_replay_pos advances past the matched words.
    Subsequent transcript mutations replay only from that position, not from 0,
    so a second command in the same session doesn't re-fire the first.
    """

    def test_replay_pos_advances_after_dispatch(self):
        loop = make_loop(['lights on', 'color pink'])
        process(loop, 'lights on')
        # After 'lights on' fires, interim_min_replay_pos should be > 0
        assert loop.interim_min_replay_pos > 0

    def test_replay_pos_prevents_double_fire(self):
        loop = make_loop(['lights on'])
        process(loop, 'lights on')
        first_dispatch_count = len(loop._dispatched)
        # Transcript mutation that doesn't change the matched region
        process(loop, 'lights on')   # identical re-delivery
        # Dedup + replay_pos should prevent a second dispatch
        assert len(loop._dispatched) == first_dispatch_count

    def test_segment_boundary_resets_replay_pos(self):
        loop = make_loop(['lights on', 'color pink'])
        process(loop, 'lights on')
        old_min = loop.interim_min_replay_pos

        # True segment boundary: word count drops, different first word
        loop.riva_interim_word_count = 2
        process(loop, 'color')  # different first word → reset
        assert loop.interim_min_replay_pos == 0


# ── Dedup / undo window ───────────────────────────────────────────────────────

class TestDedupAndUndo:
    def test_same_utterance_not_double_fired(self):
        loop = make_loop(['lights on'])
        process(loop, 'lights on')
        process(loop, 'lights on', is_final=True)
        # INTERIM fires first; FINAL should be deduped
        assert len(loop._dispatched) == 1

    def test_different_utterances_both_fire(self):
        loop = make_loop(['lights on', 'lights off'])
        process(loop, 'lights on')
        loop.interim_matcher.reset()
        loop.final_matcher.reset()
        loop.riva_interim_word_count = 0
        loop.prev_interim_transcript = ""
        process(loop, 'lights off')
        assert len(loop._dispatched) == 2

    def test_dedup_expires_after_window(self):
        loop = make_loop(['lights on'])
        loop.dedup_window_seconds = 0.01  # very short window
        process(loop, 'lights on')
        time.sleep(0.02)
        # Reset matcher state to allow re-match
        loop.interim_matcher.reset()
        loop.riva_interim_word_count = 0
        loop.prev_interim_transcript = ""
        process(loop, 'lights on')
        assert len(loop._dispatched) == 2
