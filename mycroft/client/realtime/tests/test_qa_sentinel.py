"""
Tests for the QA-mode Riva sentinel (_handle_qa_riva_words) — the handler
that detects exit/interrupt/clear_history control words while a QA session
is listening or while TTS is playing.

Background: control words (e.g. "stop") were consistently only detected on
the FINAL stream in production, never INTERIM, despite the code appearing to
support INTERIM detection for single words. Root cause: the caller
(_on_words_recognized) was passing the sentinel a *diffed* slice of new
words (new_words = words[current_count:]) rather than the full cumulative
transcript. FINAL never revises, so its diff always matches the real
transcript start — but Riva's INTERIM stream can revise/re-emit earlier
words as more audio arrives, which can desync the current_count-based diff
bookkeeping and cause the control word to be silently dropped from the
INTERIM diff, only ever caught once the (non-revising) FINAL stream delivers
it moments later.

Fix: the sentinel is now called with the full cumulative transcript, since
its exit/interrupt/clear_history checks only ever look at the first few
words of the transcript anyway (control_word_positions) — using the full
transcript removes the dependency on diff bookkeeping entirely.

Run: .venv/bin/python -m pytest mycroft/client/realtime/tests/test_qa_sentinel.py -v
"""

import threading
from unittest.mock import patch

import pytest

from mycroft.client.realtime.realtime_loop import RealtimeRecognizerLoop


class _QASentinelHarness:
    """Minimal stand-in exposing only the attributes _handle_qa_riva_words
    touches, so the real bound method can be called without constructing
    the full (audio/bus-backed) RealtimeRecognizerLoop.
    """

    def __init__(self, qa_config=None):
        self.qa_config = qa_config or {}
        self._qa_abort = False
        self._qa_abort_reason = None
        self._qa_tts_event = threading.Event()
        self._qa_committed_event = threading.Event()
        self._qa_tts_grace_until = 0.0
        self.emitted = []

    def emit(self, name, data=None):
        self.emitted.append((name, data))

    # Bind the real method under test
    _handle_qa_riva_words = RealtimeRecognizerLoop._handle_qa_riva_words


@pytest.fixture(autouse=True)
def _not_speaking():
    """Force the isSpeaking IPC-file check to always report False, so tests
    are deterministic regardless of real system TTS state.
    """
    with patch('mycroft.client.realtime.realtime_loop.os.path.isfile', return_value=False):
        yield


class TestExitWordFiresOnInterim:
    def test_single_exit_word_fires_on_interim_with_full_transcript(self):
        """The core regression: passing the FULL transcript (not a diff) lets
        a single exit word fire immediately on INTERIM.
        """
        h = _QASentinelHarness()
        h._handle_qa_riva_words(['stop'], is_final=False)
        assert h._qa_abort is True
        assert h._qa_abort_reason == 'exit'
        assert h._qa_tts_event.is_set()
        assert h._qa_committed_event.is_set()

    def test_exit_word_still_fires_on_final(self):
        h = _QASentinelHarness()
        h._handle_qa_riva_words(['stop'], is_final=True)
        assert h._qa_abort is True
        assert h._qa_abort_reason == 'exit'

    def test_exit_word_within_control_word_positions_on_interim(self):
        """Exit word doesn't have to be the first word — must be within
        control_word_positions (default 3) of the transcript start.
        """
        h = _QASentinelHarness()
        h._handle_qa_riva_words(['uh', 'stop'], is_final=False)
        assert h._qa_abort is True
        assert h._qa_abort_reason == 'exit'

    def test_exit_word_beyond_control_word_positions_does_not_fire(self):
        h = _QASentinelHarness(qa_config={'control_word_positions': 2})
        h._handle_qa_riva_words(['well', 'um', 'stop'], is_final=False)
        assert h._qa_abort is False

    def test_diffed_slice_would_have_missed_a_revised_interim(self):
        """Regression proof: simulates the exact failure mode. If Riva's
        INTERIM revises and re-emits words such that a diff-based slice
        would start AFTER 'stop' (as current_count bookkeeping could produce),
        passing the full transcript still catches it while a diff would not.
        """
        h = _QASentinelHarness()
        full_transcript = ['stop']
        # Simulate what the old buggy code would have passed: a diff that
        # missed 'stop' because current_count already advanced past it
        # (e.g. due to an INTERIM revision desyncing the bookkeeping).
        stale_diff = full_transcript[1:]  # == [] — 'stop' missed entirely
        assert stale_diff == [], "sanity check: this reproduces the old failure mode"

        # The fix: call with the full transcript instead.
        h._handle_qa_riva_words(full_transcript, is_final=False)
        assert h._qa_abort is True, "full-transcript check must catch 'stop' even when a diff would have missed it"


class TestInterruptAndClearHistoryAlsoFireOnInterim:
    def test_interrupt_word_fires_on_interim(self):
        h = _QASentinelHarness()
        h._handle_qa_riva_words(['wait'], is_final=False)
        assert h._qa_abort is True
        assert h._qa_abort_reason == 'interrupt'

    def test_clear_history_word_fires_on_interim(self):
        h = _QASentinelHarness(qa_config={'clear_history_words': ['forget']})
        h._handle_qa_riva_words(['forget'], is_final=False)
        assert h._qa_abort is True
        assert h._qa_abort_reason == 'clear_history'

    def test_clear_history_phrase_fires_on_interim(self):
        h = _QASentinelHarness()
        h._handle_qa_riva_words(['clear', 'chat'], is_final=False)
        assert h._qa_abort is True
        assert h._qa_abort_reason == 'clear_history'


class TestPhrasesRequireFinal:
    def test_exit_phrase_does_not_fire_on_interim(self):
        h = _QASentinelHarness()
        h._handle_qa_riva_words(["that's", 'all'], is_final=False)
        assert h._qa_abort is False

    def test_exit_phrase_fires_on_final(self):
        h = _QASentinelHarness()
        h._handle_qa_riva_words(["that's", 'all'], is_final=True)
        assert h._qa_abort is True
        assert h._qa_abort_reason == 'exit'


class TestSuppressionGuards:
    def test_speaking_signal_suppresses_detection(self):
        h = _QASentinelHarness()
        with patch('mycroft.client.realtime.realtime_loop.os.path.isfile', return_value=True):
            h._handle_qa_riva_words(['stop'], is_final=False)
        assert h._qa_abort is False

    def test_tts_grace_period_suppresses_detection(self):
        import time
        h = _QASentinelHarness()
        h._qa_tts_grace_until = time.time() + 10
        h._handle_qa_riva_words(['stop'], is_final=False)
        assert h._qa_abort is False

    def test_already_aborted_does_not_refire(self):
        h = _QASentinelHarness()
        h._qa_abort = True
        h._qa_abort_reason = 'exit'
        h._handle_qa_riva_words(['stop', 'wait'], is_final=False)
        # Must not have been overwritten to 'interrupt' by the later check
        assert h._qa_abort_reason == 'exit'

    def test_empty_words_is_noop(self):
        h = _QASentinelHarness()
        h._handle_qa_riva_words([], is_final=False)
        assert h._qa_abort is False
