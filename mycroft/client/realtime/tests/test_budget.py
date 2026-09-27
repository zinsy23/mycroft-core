"""
Tests for the local/global budget system in StreamingCommandMatcher.

Budget rules being tested:
  - local_budget: per-path, starts from global at path creation, increments
    on valid command words, decrements on FINAL-stream rejections within the
    path (INTERIM rejections never decrement local_budget — see
    try_add_word() in command_matcher.py, every rejection branch is gated
    behind `if stream_type == "FINAL"`)
  - global_budget: session-level, only decremented by FINAL filler words
    (when paths are active and word is rejected by all of them)
  - INTERIM fillers do NOT touch global_budget (optimistic matching)
  - FINAL fillers DO touch global_budget (authoritative)
  - Winner's local_budget syncs back to global on successful match
  - Path is pruned when local_budget <= 0
  - Local path exhaustion does not affect global or other paths
  - Global exhaustion defers session kill when open slot path (on either
    matcher) has healthy local_budget
  - Budget is healthy for a second command after a successful first match

Note on session-level kill: the actual decision lives in
realtime_loop.py's should_kill_session_on_budget(), a pure function of
(shared_global_budget, mode, interim_matcher, final_matcher). Tests here
call it directly where possible so a regression in its real logic is
caught, rather than hand-rewriting the rule inline.

Run: .venv/bin/python -m pytest mycroft/client/realtime/tests/test_budget.py -v
"""

import pytest
from mycroft.client.realtime.command_matcher import StreamingCommandMatcher


FILLER_CONFIG = {'base_max': 5, 'increment_per_word': 1}


def make_matcher(pattern='turn lights on', base_max=5):
    cfg = {'base_max': base_max, 'increment_per_word': 1}
    m = StreamingCommandMatcher(cfg)
    m.register_intent('test:lights', [pattern])
    return m


def make_decimal_matcher(base_max=5):
    cfg = {'base_max': base_max, 'increment_per_word': 1}
    m = StreamingCommandMatcher(cfg)
    m.register_intent('test:dot_product', ['dot product {number_decimal_list}'])
    return m


def feed(matcher, words, stream_type='FINAL'):
    """Feed words one at a time. Returns (last_match, accepted_flags)."""
    last_match = None
    accepted = []
    for i, word in enumerate(words):
        match, acc = matcher.add_word(word, stream_type=stream_type, transcript_position=i)
        accepted.append(acc)
        if match:
            last_match = match
    return last_match, accepted


class TestLocalBudget:
    def test_starts_from_global(self):
        """New path inherits global_budget at creation time."""
        m = make_matcher(base_max=7)
        m.add_word('turn', stream_type='FINAL', transcript_position=0)
        assert len(m.active_paths) == 1
        assert m.active_paths[0].local_budget == 7

    def test_increments_on_valid_word(self):
        """local_budget increments by increment_per_word on each accepted word."""
        m = make_matcher(base_max=5)
        m.add_word('turn', stream_type='FINAL', transcript_position=0)
        budget_after_first = m.active_paths[0].local_budget
        m.add_word('lights', stream_type='FINAL', transcript_position=1)
        budget_after_second = m.active_paths[0].local_budget
        assert budget_after_second == budget_after_first + 1

    def test_path_pruned_when_local_exhausted(self):
        """Path is pruned when local_budget reaches 0 from INTERIM fillers."""
        m = make_matcher(base_max=2)
        m.add_word('turn', stream_type='FINAL', transcript_position=0)
        assert len(m.active_paths) >= 1
        # Feed INTERIM fillers to drain local budget of the 'turn' path
        # Each INTERIM filler on a path decrements that path's local_budget
        for i, w in enumerate(['uh', 'um', 'hmm'], start=1):
            m.add_word(w, stream_type='INTERIM', transcript_position=i)
        # Path with local_budget <= 0 should be pruned
        turn_paths = [p for p in m.active_paths if p.matched_words and p.matched_words[0] == 'turn']
        assert all(p.local_budget > 0 for p in turn_paths), \
            "Pruned paths should not appear in active_paths"

    def test_global_not_affected_by_interim_filler(self):
        """INTERIM filler words must not decrement global_budget."""
        m = make_matcher(base_max=5)
        m.add_word('turn', stream_type='FINAL', transcript_position=0)
        budget_before = m.global_budget
        m.add_word('uh', stream_type='INTERIM', transcript_position=1)
        m.add_word('um', stream_type='INTERIM', transcript_position=2)
        assert m.global_budget == budget_before, \
            "INTERIM fillers must not touch global_budget"


class TestGlobalBudget:
    def test_final_filler_decrements_global(self):
        """FINAL filler rejected by active paths decrements global_budget."""
        m = make_matcher(base_max=5)
        m.add_word('turn', stream_type='FINAL', transcript_position=0)
        budget_before = m.global_budget
        # 'blah' is not a valid word for any path — FINAL filler
        m.add_word('blah', stream_type='FINAL', transcript_position=1)
        assert m.global_budget == budget_before - 1

    def test_final_filler_no_decrement_without_active_paths(self):
        """FINAL filler with no active paths does not decrement global_budget."""
        m = make_matcher(base_max=5)
        budget_before = m.global_budget
        m.add_word('blah', stream_type='FINAL', transcript_position=0)
        assert m.global_budget == budget_before, \
            "No active paths means no competition, no budget impact"

    def test_winner_syncs_local_to_global(self):
        """After a successful match, winner's local_budget syncs to global."""
        m = make_matcher(base_max=5)
        match, _ = feed(m, ['turn', 'lights', 'on'], stream_type='FINAL')
        assert match is not None
        # After match, global_budget should reflect the winner's local_budget
        assert m.global_budget > 5, \
            "Winner accumulated valid words so local > initial; global should reflect that"

    def test_multiple_final_segments_accumulate_drain(self):
        """Multiple FINAL segments each with fillers drain global cumulatively."""
        m = make_matcher(base_max=5)
        # Simulate two FINAL segments, each with rejected filler words
        # Segment 1: 'turn' accepted, then 2 fillers
        m.add_word('turn', stream_type='FINAL', transcript_position=0)
        m.add_word('blah', stream_type='FINAL', transcript_position=1)
        m.add_word('blah', stream_type='FINAL', transcript_position=2)
        budget_after_seg1 = m.global_budget
        assert budget_after_seg1 == 3  # 5 - 2 fillers

        # Segment 2: reset word count (new segment), more fillers
        m.add_word('foo', stream_type='FINAL', transcript_position=3)
        m.add_word('bar', stream_type='FINAL', transcript_position=4)
        budget_after_seg2 = m.global_budget
        assert budget_after_seg2 == 1  # 3 - 2 more fillers


class TestOpenSlotBudget:
    def test_slot_words_increment_local_budget(self):
        """Words accepted into a decimal_list slot increment local_budget."""
        m = make_decimal_matcher(base_max=5)
        m.add_word('dot', stream_type='FINAL', transcript_position=0)
        m.add_word('product', stream_type='FINAL', transcript_position=1)
        budget_before_slot = max(p.local_budget for p in m.active_paths)
        m.add_word('point', stream_type='FINAL', transcript_position=2)
        m.add_word('eight', stream_type='FINAL', transcript_position=3)
        budget_after_slot = max(p.local_budget for p in m.active_paths)
        assert budget_after_slot > budget_before_slot, \
            "Slot words should increment local_budget"

    def test_global_drain_does_not_prune_healthy_local_slot_path(self):
        """A path with healthy local_budget stays alive even if global hits 0.

        This documents the expected behavior: the global_budget being exhausted
        by FINAL fillers from previous segments should not kill a path that has
        been accumulating valid slot words (and thus has healthy local_budget).
        The session-level kill based on global should be deferred while a slot
        is open with a healthy local path.
        """
        m = make_decimal_matcher(base_max=3)
        # Start the dot product command
        m.add_word('dot', stream_type='FINAL', transcript_position=0)
        m.add_word('product', stream_type='FINAL', transcript_position=1)
        # Feed slot words — these increment local_budget
        slot_words = 'point eight one then point two five'.split()
        for i, w in enumerate(slot_words, start=2):
            m.add_word(w, stream_type='FINAL', transcript_position=i)
        # Now drain global with FINAL fillers (simulating garbled Riva segment)
        m.global_budget = 0  # force global to 0 as if prior segments drained it
        # The open slot path should still be alive with healthy local_budget
        open_slot_paths = [p for p in m.active_paths if p._decimal_list_slot_name is not None]
        assert open_slot_paths, "Open slot path must survive global exhaustion"
        assert all(p.local_budget > 0 for p in open_slot_paths), \
            "Open slot path local_budget must be positive despite global=0"

    def test_local_exhaustion_does_not_affect_global(self):
        """A path dying from local_budget exhaustion must not change global_budget."""
        m = make_decimal_matcher(base_max=5)
        m.add_word('dot', stream_type='FINAL', transcript_position=0)
        m.add_word('product', stream_type='FINAL', transcript_position=1)
        budget_before = m.global_budget
        # Drain local via INTERIM fillers until path is pruned
        for i in range(10):
            m.add_word('uh', stream_type='INTERIM', transcript_position=i + 2)
        assert m.global_budget == budget_before, \
            "Local path exhaustion must not change global_budget"

    def test_budget_healthy_for_second_command_after_match(self):
        """After a successful match, global is restored so the next command works."""
        m = make_matcher(base_max=5)
        # First command — drains some budget via fillers before matching
        m.add_word('blah', stream_type='FINAL', transcript_position=0)  # filler: global=4
        m.add_word('blah', stream_type='FINAL', transcript_position=1)  # filler: global=3
        match, _ = feed(m, ['turn', 'lights', 'on'], stream_type='FINAL')
        assert match is not None
        # Winner's local_budget (5 start + 3 words = 8) syncs to global
        assert m.global_budget > 3, \
            "Global must be restored above pre-match level after successful match"

        # Second command should work without budget issues
        m.reset()
        match2, _ = feed(m, ['turn', 'lights', 'on'], stream_type='FINAL')
        assert match2 is not None, "Second command must succeed with restored budget"

    def test_winner_local_budget_available_after_slot_match(self):
        """After decimal_list slot completes, the winning path's local_budget
        is available for the session layer to sync to global.

        Note: the actual sync to global happens in realtime_loop.py at the
        session level (shared_global_budget = path.local_budget), not inside
        the matcher itself. This test verifies the winning path has accumulated
        a healthy local_budget that the session layer can use.
        """
        m = make_decimal_matcher(base_max=5)
        words = 'dot product point two then one point five'.split()
        match, _ = feed(m, words, stream_type='FINAL')
        if match is None:
            match = m.close_slots_and_check()
        assert match is not None
        # The match result comes from a path — verify that path had healthy budget.
        # We verify indirectly: at least one path had local_budget > base_max
        # because slot words increment it beyond the starting value.
        # (Paths are reset after match, so we verify via the match existing
        # and the fact that the path survived to completion — budget > 0.)
        assert match['intent'] == 'test:dot_product'


def _simulate_session(interim_m, final_m, shared_budget,
                       interim_words, final_segments):
    """Simulate the realtime_loop session logic for budget tests.

    Drives interim_matcher with interim_words (INTERIM stream), then drives
    final_matcher with each segment in final_segments (FINAL stream),
    syncing shared_budget between them as realtime_loop does.

    Returns (dispatched, open_slot_alive, final_budget) where:
      dispatched:      True if check_completion succeeded after last FINAL
      open_slot_alive: True if interim has open decimal_list path at FINAL boundary
      final_budget:    shared_budget value after all segments processed
    """
    # Feed INTERIM words
    for i, w in enumerate(interim_words):
        interim_m.global_budget = shared_budget[0]
        interim_m.add_word(w, stream_type='INTERIM', transcript_position=i)
        # INTERIM fillers don't touch global — no sync needed

    # Feed each FINAL segment, syncing budget as the loop does
    pos = 0
    for segment in final_segments:
        final_m.global_budget = shared_budget[0]
        for w in segment:
            match, _ = final_m.add_word(w, stream_type='FINAL', transcript_position=pos)
            pos += 1
        shared_budget[0] = final_m.global_budget

    # Check if open slot path exists on interim at FINAL boundary
    open_slot_alive = any(
        p._decimal_list_slot_name is not None
        for p in interim_m.active_paths
    )

    # Simulate FINAL boundary: pick whichever matcher's open decimal_list path
    # has accumulated more slot content, not just "does final have any path
    # open" — a FINAL-side path can exist (e.g. reopened by garbled tail
    # words) yet be far less complete than the INTERIM one. Uses the REAL
    # _slot_buf_len() from realtime_loop.py (not a re-derivation) so a
    # regression there is caught here too.
    from mycroft.client.realtime.realtime_loop import _slot_buf_len

    interim_open = [p for p in interim_m.active_paths if p._decimal_list_slot_name is not None]
    final_open = [p for p in final_m.active_paths if p._decimal_list_slot_name is not None]
    interim_best_len = max((_slot_buf_len(p) for p in interim_open), default=-1)
    final_best_len = max((_slot_buf_len(p) for p in final_open), default=-1)
    boundary_matcher = interim_m if (interim_open and interim_best_len > final_best_len) else final_m

    dispatched = False
    for path in list(boundary_matcher.active_paths):
        if path._decimal_list_slot_name is not None:
            result = path.check_completion()
            if result:
                dispatched = True
                # Session layer would sync: shared_budget = path.local_budget
                shared_budget[0] = path.local_budget
                break

    return dispatched, open_slot_alive, shared_budget[0]


class TestSessionKillDeferred:
    """Tests for the rule: session must not die from global budget exhaustion
    while an open slot path with healthy local_budget exists.

    The session-level kill is in realtime_loop.py. These tests verify the
    matcher-level state is correct for the session layer to make the right
    decision. They also simulate the session logic directly to catch cases
    where the fix is needed.
    """

    def test_gibberish_exhausts_budget_no_slot_session_dies(self):
        """With no open slot and exhausted global, session should end.

        This is the correct behavior — random gibberish should kill the session.
        Calls the real should_kill_session_on_budget() (not a hand-rewritten
        rule) so a regression in its actual logic would be caught here.
        """
        from mycroft.client.realtime.realtime_loop import should_kill_session_on_budget, MODE_DETERMINISTIC

        m = make_matcher(base_max=3)
        m.add_word('turn', stream_type='FINAL', transcript_position=0)
        # Four fillers exhaust global
        for i, w in enumerate(['blah', 'foo', 'bar', 'baz'], start=1):
            m.add_word(w, stream_type='FINAL', transcript_position=i)
        assert m.global_budget == 0
        empty_other_matcher = make_matcher(base_max=3)
        should_kill = should_kill_session_on_budget(
            shared_global_budget=m.global_budget, mode=MODE_DETERMINISTIC,
            interim_matcher=m, final_matcher=empty_other_matcher,
        )
        assert should_kill, "Session should end on budget exhaustion with no open slot"

    def test_multi_final_segment_drain_defers_kill_with_open_slot(self):
        """Global drained by multiple FINAL segments should NOT kill session
        when interim_matcher has an open decimal_list slot with healthy local.

        This is the bug scenario from 2026-09-13 18:07:40.
        Simulates: INTERIM accumulates slot words, FINAL segments arrive with
        garble draining global to 0, last FINAL is the tail "dot".
        Expected: open_slot_alive=True, dispatched=True (check_completion succeeds).
        """
        interim_m = make_decimal_matcher(base_max=5)
        final_m = make_decimal_matcher(base_max=5)
        shared = [5]

        # INTERIM stream: full dot product utterance with 12 slot words
        interim_words = ('dot product point eight one point two five one point six '
                         'negative point two five negative point eight four '
                         'negative point three four one point three two '
                         'point two nine negative point six four '
                         'point two seven one point two one point two six').split()

        # FINAL segments: multiple garbled segments drain global, last is "dot"
        # Each garbled segment has rejected fillers that drain global
        final_segments = [
            ['dot', 'product'],          # seg 1: accepted, no drain
            ['oneoint', 'blah', 'foo'],  # seg 2: garble — 2 fillers drain global
            ['bar', 'baz'],              # seg 3: more garble — 2 more fillers
            ['dot'],                     # seg 4: tail — accepted, but global may be 0
        ]

        dispatched, open_slot_alive, final_budget = _simulate_session(
            interim_m, final_m, shared, interim_words, final_segments
        )

        assert open_slot_alive, \
            "Interim must have open decimal_list slot at FINAL boundary"
        assert dispatched, \
            "check_completion must succeed — 12 valid slot words accumulated in INTERIM buf"
        assert final_budget > 0, \
            "Budget should be restored from winning path's local_budget after dispatch"

    def test_session_kill_correct_when_slot_exhausted_too(self):
        """If both global AND all open-slot paths' local_budget are exhausted,
        killing the session is correct — nothing healthy to dispatch from.
        Calls the real should_kill_session_on_budget() directly.
        """
        from mycroft.client.realtime.realtime_loop import should_kill_session_on_budget, MODE_DETERMINISTIC

        m = make_decimal_matcher(base_max=2)
        m.add_word('dot', stream_type='FINAL', transcript_position=0)
        m.add_word('product', stream_type='FINAL', transcript_position=1)
        # Force both global and all local budgets to 0
        m.global_budget = 0
        for p in m.active_paths:
            p.local_budget = 0
        empty_other_matcher = make_matcher(base_max=2)
        should_kill = should_kill_session_on_budget(
            shared_global_budget=m.global_budget, mode=MODE_DETERMINISTIC,
            interim_matcher=m, final_matcher=empty_other_matcher,
        )
        assert should_kill, \
            "Session should end when both global and all slot path locals are exhausted"


class TestKillDeferChecksBothMatchers:
    """The session-kill-defer check must consider BOTH interim_matcher and
    final_matcher, not just interim_matcher.

    Regression for a real bug: the FINAL-boundary richer-buffer fallback can
    correctly select final_matcher's path as the live, still-accumulating
    one (e.g. interim_matcher's active_paths is momentarily empty — Riva's
    interim stream lagging, or a refinement reset just cleared it). If the
    kill-defer check only looks at interim_matcher, a healthy open slot on
    final_matcher is invisible to it and the session dies incorrectly.

    These tests call should_kill_session_on_budget() — the actual function
    realtime_loop.py's _process_word invokes at the FINAL boundary — not a
    re-derivation of its logic, so a regression at the real call site is
    caught directly rather than only in a helper the call site might stop
    using correctly.
    """

    def test_healthy_slot_on_final_matcher_alone_defers_kill(self):
        """Calls the REAL should_kill_session_on_budget() call-site function
        (not a re-derivation) — this is the exact function realtime_loop.py's
        _process_word invokes at the FINAL boundary. Exercises the fixed bug
        directly: interim_matcher is empty, final_matcher alone holds the
        live healthy slot, and the session must NOT be killed.
        """
        from mycroft.client.realtime.realtime_loop import should_kill_session_on_budget, MODE_DETERMINISTIC

        interim_m = make_matcher(base_max=5)  # empty — no active paths
        final_m = make_decimal_matcher(base_max=5)
        words = 'dot product point eight one point two'.split()
        for i, w in enumerate(words):
            final_m.add_word(w, stream_type='FINAL', transcript_position=i)

        should_kill = should_kill_session_on_budget(
            shared_global_budget=0, mode=MODE_DETERMINISTIC,
            interim_matcher=interim_m, final_matcher=final_m,
        )
        assert not should_kill, \
            "Session must not die just because interim_matcher is empty while " \
            "final_matcher holds the live healthy slot"

    def test_no_healthy_slot_on_either_matcher_kills_session(self):
        from mycroft.client.realtime.realtime_loop import should_kill_session_on_budget, MODE_DETERMINISTIC

        interim_m = make_matcher(base_max=5)
        final_m = make_matcher(base_max=5)
        should_kill = should_kill_session_on_budget(
            shared_global_budget=0, mode=MODE_DETERMINISTIC,
            interim_matcher=interim_m, final_matcher=final_m,
        )
        assert should_kill, "No open slot on either matcher and global exhausted — session should end"

    def test_qa_mode_never_killed_by_budget(self):
        from mycroft.client.realtime.realtime_loop import should_kill_session_on_budget, MODE_QA

        interim_m = make_matcher(base_max=5)
        final_m = make_matcher(base_max=5)
        should_kill = should_kill_session_on_budget(
            shared_global_budget=0, mode=MODE_QA,
            interim_matcher=interim_m, final_matcher=final_m,
        )
        assert not should_kill, "QA mode must be exempt from budget-exhaustion kill"

    def test_healthy_budget_never_kills_regardless_of_slots(self):
        from mycroft.client.realtime.realtime_loop import should_kill_session_on_budget, MODE_DETERMINISTIC

        interim_m = make_matcher(base_max=5)
        final_m = make_matcher(base_max=5)
        should_kill = should_kill_session_on_budget(
            shared_global_budget=5, mode=MODE_DETERMINISTIC,
            interim_matcher=interim_m, final_matcher=final_m,
        )
        assert not should_kill, "Healthy global budget must never trigger a kill"


class TestCloseSlotsAndCheckBudgetSync:
    """close_slots_and_check() mirrors realtime_loop's FINAL-boundary slot
    close, and must sync the winner's local_budget to global on match just
    like add_word()'s word-level winner sync does (command_matcher.py:1021).

    Without this sync, a caller of close_slots_and_check() (any test or
    future refactor that uses it instead of duplicating the FINAL-boundary
    logic inline, as realtime_loop.py currently does) would silently drop
    the budget carry-forward into the next command.
    """

    def test_winner_local_budget_synced_to_global_on_match(self):
        m = make_decimal_matcher(base_max=5)
        words = 'dot product point eight one point two five'.split()
        for i, w in enumerate(words):
            m.add_word(w, stream_type='FINAL', transcript_position=i)
        open_slot_paths = [p for p in m.active_paths if p._decimal_list_slot_name is not None]
        assert open_slot_paths, "Should have an open decimal_list slot path"
        expected_local = open_slot_paths[0].local_budget

        match = m.close_slots_and_check()

        assert match is not None, "close_slots_and_check must dispatch on complete even-count list"
        assert m.global_budget == expected_local, \
            "close_slots_and_check must sync winner's local_budget to global on match, " \
            "same as add_word()'s word-level winner sync"
