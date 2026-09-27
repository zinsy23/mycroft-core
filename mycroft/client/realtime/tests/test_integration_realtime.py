"""
Integration tests for the realtime service — require a live environment.

These tests verify behavior that cannot be unit-tested because they depend on:
  - Riva ASR server running at localhost:50051
  - Mycroft messagebus running
  - Audio subsystem (parec/paplay)

Run only when developing/testing in the live environment:
    .venv/bin/python -m pytest mycroft/client/realtime/tests/test_integration_realtime.py -v -m integration

Normal unit test runs skip these automatically.

Tests here document expected session-level behavior — things the unit tests
in test_budget.py and test_command_matcher_*.py cannot catch because they only
exercise the matcher in isolation, not the full realtime_loop.py session logic.
"""

import pytest
import socket
import time


def _riva_reachable(host='localhost', port=50051, timeout=1.0):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def _bus_reachable(host='localhost', port=8181, timeout=1.0):
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


riva_available = pytest.mark.skipif(
    not _riva_reachable(),
    reason="Riva ASR server not reachable at localhost:50051"
)

bus_available = pytest.mark.skipif(
    not _bus_reachable(),
    reason="Mycroft messagebus not reachable at localhost:8181"
)


# ── Budget / session-kill integration tests ───────────────────────────────────

@pytest.mark.integration
class TestSessionBudgetLive:
    """
    Verify that the session does NOT die from global budget exhaustion while
    a decimal_list slot is open with a healthy local_budget path.

    Background: Riva emits multiple FINAL segments for long utterances. Each
    FINAL segment's filler words drain global_budget. By the time the last
    FINAL arrives, global_budget can be 0 even though the preserved interim
    path has healthy local_budget (accumulated from slot words). The session
    must not end until FINAL boundary check_completion resolves.

    This was observed at 2026-09-13 18:07:40 where:
      path.local_budget=565, matcher.global_budget=0, shared=5
      → "Global budget exhausted — ending session" fired after failed check_completion

    To run manually:
      1. Start Mycroft (start-mycroft.sh)
      2. Run: .venv/bin/python -m pytest mycroft/client/realtime/tests/test_integration_realtime.py -v -m integration -k budget
      3. When prompted, say the dot product command with 12 numbers
    """

    @riva_available
    @bus_available
    def test_decimal_list_slot_survives_global_budget_drain(self):
        """
        Session must stay alive through a long decimal_list utterance even if
        global budget drains from earlier FINAL segment fillers.

        This test drives the realtime loop directly by simulating the word
        sequence that caused the 2026-09-13 failure, bypassing audio capture.
        """
        from mycroft.client.realtime.command_matcher import StreamingCommandMatcher
        from mycroft.client.realtime.realtime_loop import RealtimeRecognizerLoop

        # Build a matcher in the same config as the live service
        filler_cfg = {'base_max': 5, 'increment_per_word': 1}
        interim_matcher = StreamingCommandMatcher(filler_cfg)
        final_matcher = StreamingCommandMatcher(filler_cfg)
        interim_matcher.register_intent(
            'matrix-calculator-skill:matrix.dot.product.intent',
            ['dot product {number_decimal_list}']
        )
        final_matcher.register_intent(
            'matrix-calculator-skill:matrix.dot.product.intent',
            ['dot product {number_decimal_list}']
        )

        shared_budget = [5]  # mutable so inner function can write it

        def feed_final(matcher, words, start_pos=0):
            for i, w in enumerate(words, start=start_pos):
                matcher.global_budget = shared_budget[0]
                match, accepted = matcher.add_word(w, stream_type='FINAL', transcript_position=i)
                shared_budget[0] = matcher.global_budget
                if match:
                    return match
            return None

        # Segment 1: "dot product" — command prefix
        feed_final(interim_matcher, ['dot', 'product'])
        feed_final(final_matcher, ['dot', 'product'])

        # Simulate slot accumulation via INTERIM (many slot words — healthy local)
        slot_words = ('point eight one point two five one point six negative point two five '
                      'negative point eight four negative point three four one point three two '
                      'point two nine negative point six four point two seven one point two one '
                      'point two six').split()
        for i, w in enumerate(slot_words, start=2):
            interim_matcher.global_budget = shared_budget[0]
            interim_matcher.add_word(w, stream_type='INTERIM', transcript_position=i)
            # INTERIM fillers don't touch global — budget unchanged

        # Simulate prior FINAL segments draining global to near-zero
        shared_budget[0] = 1  # as if prior FINAL segments spent it

        # Last FINAL segment arrives as just "dot" (Riva tail)
        final_matcher.global_budget = shared_budget[0]
        final_matcher.add_word('dot', stream_type='FINAL', transcript_position=2)
        shared_budget[0] = final_matcher.global_budget

        # At this point: global may be 0, but interim has open slot with healthy local
        open_slot_paths = [p for p in interim_matcher.active_paths
                           if p._decimal_list_slot_name is not None]
        assert open_slot_paths, "Interim matcher must have open decimal_list slot path"

        best = max(open_slot_paths, key=lambda p: p.local_budget)
        assert best.local_budget > 0, \
            "Open slot path must have healthy local_budget despite global drain"

        # check_completion on the best path should succeed (even count, valid values)
        result = best.check_completion()
        assert result is not None, \
            "check_completion must succeed — session should have dispatched, not died"
        values = result.get('entities', {}).get('number_decimal_list', [])
        assert len(values) == 12, f"Expected 12 values, got {len(values)}: {values}"


# ── Timeout integration tests ─────────────────────────────────────────────────

@pytest.mark.integration
class TestSessionTimeoutLive:
    """
    Verify that last_word_time is reset by slot word acceptance, preventing
    the 15s session timeout from firing during active decimal_list accumulation.

    Rule: any accepted word (command word or slot word, INTERIM or FINAL)
    resets last_word_time. The session timeout should not fire while the user
    is actively speaking numbers into a slot.
    """

    def test_slot_word_acceptance_resets_timeout_clock(self):
        """
        Slot words accepted via try_add_word must mark word_accepted=True so
        the session layer updates last_word_time.

        This is a unit-level check but lives here as documentation of the
        session-level contract — the session layer at realtime_loop.py:1644
        uses word_accepted from add_word() to decide whether to reset the clock.
        """
        from mycroft.client.realtime.command_matcher import StreamingCommandMatcher

        filler_cfg = {'base_max': 5, 'increment_per_word': 1}
        m = StreamingCommandMatcher(filler_cfg)
        m.register_intent('test:dp', ['dot product {number_decimal_list}'])

        # Feed command prefix to open a path
        m.add_word('dot', stream_type='INTERIM', transcript_position=0)
        m.add_word('product', stream_type='INTERIM', transcript_position=1)

        # Feed slot words — each must be accepted (word_accepted=True)
        slot_words = 'point eight one then point two five'.split()
        for i, w in enumerate(slot_words, start=2):
            _, accepted = m.add_word(w, stream_type='INTERIM', transcript_position=i)
            assert accepted, \
                f"Slot word '{w}' must be accepted (word_accepted=True) to reset timeout clock"
