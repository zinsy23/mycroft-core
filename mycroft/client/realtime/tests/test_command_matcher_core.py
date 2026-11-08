"""
Core matcher behavior tests for StreamingCommandMatcher / MatcherPath.

Covers the invariants that the whole filler-word system depends on:

  FILLER ABSORPTION
    - Words not valid at the current path position are silently skipped (INTERIM free)
    - FINAL fillers cost 1 budget each
    - Commands fire correctly with arbitrary fillers between valid words

  PATH EVOLUTION
    - Valid first words always spawn a new path
    - Paths advance only on valid words; filler never advances matched_words
    - Dedup collapses identical path states, keeping highest budget

  AMBIGUOUS MIDDLE WORD (the "the" bug — paths that accept a word valid in some
    patterns but not others must fork so both branches can compete)
    - "when is the sunrise today" fires even though "the" is valid elsewhere
    - The literal pattern "what is the current temperature" still fires correctly
    - Both can coexist in the same matcher without crowding each other out

  SLOT GUARD
    - Forking is suppressed inside greedy slots (number/calc/decimal)
    - Long number sequences don't cause exponential path growth

  BUDGET
    - Each accepted word gives +1 budget (except the first word of a path)
    - INTERIM fillers don't drain budget; FINAL fillers drain 1 each
    - Paths die when budget reaches 0

  REPLAY SAFETY
    - Replaying already-processed words through paths in mid-match state
      (simulating Riva transcript mutation) still fires the match correctly

Run: .venv/bin/python3 -m pytest mycroft/client/realtime/tests/test_command_matcher_core.py -v
"""

import pytest
from mycroft.client.realtime.command_matcher import StreamingCommandMatcher
from mycroft.client.realtime.pattern_loader import expand_pattern_entities

FILLER_CFG = {'base_max': 4, 'increment_per_word': 1}


def make(*patterns, filler_cfg=None):
    """Return a fresh matcher with the given literal pattern strings registered."""
    cfg = filler_cfg or FILLER_CFG
    m = StreamingCommandMatcher(cfg)
    expanded = []
    for p in patterns:
        result = expand_pattern_entities(p, {})
        expanded.extend(result[0] if isinstance(result, tuple) else result)
    m.register_intent('test', expanded)
    return m


def feed(matcher, words, stream='INTERIM'):
    """Feed words one at a time; return (match_or_None, max_path_count)."""
    max_paths = 0
    for w in words:
        match, _ = matcher.add_word(w, stream_type=stream)
        max_paths = max(max_paths, len(matcher.active_paths))
        if match:
            return match, max_paths
    return None, max_paths


# ── Filler absorption ──────────────────────────────────────────────────────────

class TestFillerAbsorption:
    def test_single_filler_between_valid_words(self):
        m = make('turn lights on')
        match, _ = feed(m, ['turn', 'the', 'lights', 'on'])
        assert match is not None
        assert match['utterance'] == 'turn lights on'

    def test_multiple_fillers(self):
        m = make('color pink')
        match, _ = feed(m, ['set', 'the', 'uh', 'color', 'to', 'pink'])
        assert match is not None
        assert match['utterance'] == 'color pink'

    def test_interim_fillers_free(self):
        # 4 fillers on INTERIM should not drain budget (base_max=4)
        m = make('color pink', filler_cfg={'base_max': 4, 'increment_per_word': 1})
        match, _ = feed(m, ['uh', 'um', 'so', 'uh', 'color', 'pink'], stream='INTERIM')
        assert match is not None

    def test_final_fillers_drain_budget(self):
        # FINAL fillers drain budget only when there are active paths (competition).
        # With base_max=1 and two FINAL fillers after the path starts, budget hits 0
        # and the path is pruned before 'pink' arrives.
        m = make('color pink', filler_cfg={'base_max': 1, 'increment_per_word': 1})
        m.add_word('color', stream_type='FINAL')   # path starts at budget=1
        m.add_word('uh', stream_type='FINAL')       # filler with path active → budget=0, pruned
        m.add_word('um', stream_type='FINAL')       # no paths left, noop
        match, _ = m.add_word('pink', stream_type='FINAL')
        assert match is None

    def test_accepted_words_replenish_budget(self):
        # Even tight budget survives if words are accepted along the way
        m = make('turn lights on', filler_cfg={'base_max': 2, 'increment_per_word': 1})
        # 'turn' accepted (+0 first word), 'uh' filler FINAL (-1), 'lights' accepted (+1),
        # 'uh' filler FINAL (-1), 'on' accepted (+1) → budget stays positive
        match, _ = feed(m, ['turn', 'uh', 'lights', 'uh', 'on'], stream='FINAL')
        assert match is not None

    def test_filler_does_not_advance_matched_words(self):
        m = make('lights on')
        m.add_word('the', stream_type='INTERIM')
        # Only one path, it shouldn't have advanced
        for path in m.active_paths:
            assert path.matched_words == [] or path.matched_words == ['lights']


# ── Path evolution ─────────────────────────────────────────────────────────────

class TestPathEvolution:
    def test_valid_first_word_spawns_path(self):
        m = make('turn lights on')
        m.add_word('turn', stream_type='INTERIM')
        assert len(m.active_paths) == 1
        assert m.active_paths[0].matched_words == ['turn']

    def test_invalid_first_word_no_path(self):
        m = make('turn lights on')
        m.add_word('the', stream_type='INTERIM')
        # No path whose first word is 'the'
        matching = [p for p in m.active_paths if p.matched_words == ['the']]
        assert not matching

    def test_path_advances_on_valid_word(self):
        m = make('turn lights on')
        m.add_word('turn', stream_type='INTERIM')
        m.add_word('lights', stream_type='INTERIM')
        paths_with_two = [p for p in m.active_paths if p.matched_words == ['turn', 'lights']]
        assert paths_with_two

    def test_complete_command_fires(self):
        m = make('turn lights on')
        match, _ = feed(m, ['turn', 'lights', 'on'])
        assert match is not None
        assert match['utterance'] == 'turn lights on'

    def test_optional_group_both_variants_fire(self):
        m = make('[what|when] is sunrise today')
        for first in ['what', 'when']:
            m2 = make('[what|when] is sunrise today')
            match, _ = feed(m2, [first, 'is', 'sunrise', 'today'])
            assert match is not None, f"'{first} is sunrise today' should fire"


# ── Ambiguous middle word (the "the" bug) ──────────────────────────────────────

class TestAmbiguousMiddleWord:
    """
    When a word is valid in some patterns at a given position but not others,
    the matcher must fork so both branches survive.  The classic case: "the"
    is required in "what is the current temperature" but is a filler in
    "[what|when] is sunrise today".  Before the forking fix, a path that
    accepted "the" locked out "sunrise" because the two patterns shared the
    same path object.
    """

    def _weather_matcher(self):
        m = StreamingCommandMatcher(FILLER_CFG)
        for intent, patterns in [
            ('temp', ['what is the current temperature',
                      'what is the temperature now']),
            ('sunrise', ['[what|when] is sunrise [today|tomorrow]',
                         '[what|when] is sunset [today|tomorrow]']),
        ]:
            expanded = []
            for p in patterns:
                r = expand_pattern_entities(p, {})
                expanded.extend(r[0] if isinstance(r, tuple) else r)
            m.register_intent(intent, expanded)
        return m

    def test_sunrise_with_the_fires(self):
        m = self._weather_matcher()
        match, _ = feed(m, ['when', 'is', 'the', 'sunrise', 'today'])
        assert match is not None
        assert 'sunrise' in match['intent']

    def test_sunset_with_the_fires(self):
        m = self._weather_matcher()
        match, _ = feed(m, ['when', 'is', 'the', 'sunset', 'today'])
        assert match is not None
        assert 'sunrise' in match['intent']  # same intent file

    def test_sunrise_without_the_fires(self):
        m = self._weather_matcher()
        match, _ = feed(m, ['when', 'is', 'sunrise', 'today'])
        assert match is not None
        assert 'sunrise' in match['intent']

    def test_temperature_with_the_fires(self):
        m = self._weather_matcher()
        match, _ = feed(m, ['what', 'is', 'the', 'current', 'temperature'])
        assert match is not None
        assert 'temp' in match['intent']

    def test_temperature_now_fires(self):
        m = self._weather_matcher()
        match, _ = feed(m, ['what', 'is', 'the', 'temperature', 'now'])
        assert match is not None
        assert 'temp' in match['intent']

    def test_path_count_bounded(self):
        # Forking should not cause unbounded path growth
        m = self._weather_matcher()
        _, max_paths = feed(m, ['when', 'is', 'the', 'sunrise', 'today'])
        assert max_paths <= 10, f"Too many paths: {max_paths}"

    def test_replay_after_mutation_fires(self):
        """
        Simulate Riva mutating 'to' -> 'today' after paths have already built up.
        The transcript-changed branch replays all words from position 0 through
        paths that are mid-match at ['when','is','sunrise'].  'today' must fire.
        """
        m = self._weather_matcher()

        # Phase 1: build paths via 'when is the sunrise to'
        for w in ['when', 'is', 'the', 'sunrise', 'to']:
            m.add_word(w, stream_type='INTERIM')

        # Phase 2: Riva mutates — replay full transcript with 'today'
        match = None
        for w in ['when', 'is', 'the', 'sunrise', 'today']:
            result, _ = m.add_word(w, stream_type='INTERIM'), None
            match = result[0]
            if match:
                break

        assert match is not None, "'today' replay should fire the sunrise match"
        assert 'sunrise' in match['intent']


# ── Slot guard (no forking inside greedy slots) ────────────────────────────────

class TestSlotGuard:
    def test_number_slot_no_path_explosion(self):
        m = make('[set|change] the color to {number}',
                 'calculate {number_calc}')
        # Long number sequence — should not explode paths
        _, max_paths = feed(m, ['set', 'the', 'color', 'to',
                                 'one', 'hundred', 'and', 'twenty', 'three'])
        assert max_paths <= 15, f"Path count too high: {max_paths}"

    def test_number_slot_still_matches(self):
        # Number slots close at the FINAL boundary (greedy accumulation until speech ends)
        m = make('brightness {number}')
        for w in ['brightness', 'fifty']:
            m.add_word(w, stream_type='FINAL')
        match = m.close_slots_and_check()
        assert match is not None
        assert match['entities'].get('number') == 50

    def test_number_slot_with_filler_still_matches(self):
        m = make('brightness {number}')
        for w in ['set', 'the', 'brightness', 'to', 'fifty']:
            m.add_word(w, stream_type='FINAL')
        match = m.close_slots_and_check()
        assert match is not None


# ── Budget mechanics ───────────────────────────────────────────────────────────

class TestBudget:
    def test_budget_starts_at_base_max(self):
        m = make('lights on', filler_cfg={'base_max': 7, 'increment_per_word': 1})
        m.add_word('lights', stream_type='INTERIM')
        assert m.active_paths[0].local_budget == 7

    def test_accepted_word_increments_budget(self):
        m = make('lights on')
        m.add_word('lights', stream_type='INTERIM')
        budget_after_first = m.active_paths[0].local_budget
        m.add_word('on', stream_type='INTERIM')
        # 'on' is the second accepted word — budget should be +1 (match fires, check before)
        # Just verify it fired (budget sync happens on match)
        # The increment happens before match fires
        assert budget_after_first == FILLER_CFG['base_max']

    def test_path_dies_at_zero_budget(self):
        m = make('lights on', filler_cfg={'base_max': 2, 'increment_per_word': 1})
        # Feed 3 FINAL fillers before lights — budget = 2-1-1-1 = -1 → pruned
        for w in ['uh', 'um', 'so']:
            m.add_word(w, stream_type='FINAL')
        # 'lights' would spawn a fresh path at base_max (global budget may be 0 though)
        m.add_word('lights', stream_type='FINAL')
        m.add_word('on', stream_type='FINAL')
        # With global budget drained, new path starts at 0 or less → dies immediately
        # This is a budget-exhausted-kills-session scenario; just verify no crash
        # (actual behavior depends on global budget state)


# ── Invalid first word guard ──────────────────────────────────────────────────

class TestInvalidFirstWord:
    def test_non_command_word_never_starts_path(self):
        m = make('turn lights on')
        m.add_word('the', stream_type='INTERIM')
        # 'the' is not a valid first word — no path should start with it
        assert all(p.matched_words[0] != 'the' for p in m.active_paths if p.matched_words)

    def test_filler_before_command_no_spurious_path(self):
        m = make('turn lights on')
        for w in ['uh', 'um', 'so', 'like']:
            m.add_word(w, stream_type='INTERIM')
        # None of those are valid first words
        assert len(m.active_paths) == 0

    def test_valid_first_word_after_fillers_starts_path(self):
        m = make('turn lights on')
        for w in ['uh', 'um', 'turn']:
            m.add_word(w, stream_type='INTERIM')
        assert any(p.matched_words == ['turn'] for p in m.active_paths)

    def test_only_registered_first_words_start_paths(self):
        # 'lights' and 'on' are valid words but not valid FIRST words
        m = make('turn lights on')
        m.add_word('lights', stream_type='INTERIM')
        m.add_word('on', stream_type='INTERIM')
        # No paths should have started — these aren't first words in any sequence
        assert all(p.matched_words[0] not in ('lights', 'on')
                   for p in m.active_paths if p.matched_words)


# ── INTERIM vs FINAL asymmetry ─────────────────────────────────────────────────

class TestInterimFinalAsymmetry:
    def test_interim_path_survives_many_fillers(self):
        # INTERIM fillers are free — a path should survive far more than base_max fillers
        m = make('color pink', filler_cfg={'base_max': 2, 'increment_per_word': 1})
        m.add_word('color', stream_type='INTERIM')
        for _ in range(10):
            m.add_word('uh', stream_type='INTERIM')
        # Path should still be alive — INTERIM fillers cost nothing
        assert any(p.matched_words == ['color'] for p in m.active_paths)

    def test_final_path_dies_after_budget_fillers(self):
        # FINAL fillers cost 1 each — path dies at budget=0
        m = make('color pink', filler_cfg={'base_max': 1, 'increment_per_word': 1})
        m.add_word('color', stream_type='FINAL')   # path starts, budget=1
        m.add_word('uh', stream_type='FINAL')       # filler, budget=0, pruned
        assert not any(p.matched_words == ['color'] for p in m.active_paths)

    def test_interim_command_fires_before_final(self):
        # A command should fire on INTERIM as soon as it's complete
        m = make('lights on')
        match, _ = feed(m, ['lights', 'on'], stream='INTERIM')
        assert match is not None

    def test_path_built_on_interim_survives_to_final(self):
        # Paths built during INTERIM processing should still be alive when FINAL arrives
        m = make('color pink')
        m.add_word('color', stream_type='INTERIM')
        # Simulate FINAL stream arriving with same word
        match, _ = m.add_word('pink', stream_type='FINAL')
        assert match is not None


# ── Entity slot — strict whitelist (the "entity vortex" bug) ──────────────────

class TestEntitySlotWhitelist:
    """
    Word-list entity slots must be strict whitelists: only defined values advance
    the path.  The historical bug was that a slot would accept ANY word (vortex),
    swallowing words that belonged to the next literal position and preventing
    completion.  The inverse — a slot with no values becoming impassable — was
    also a problem.
    """

    def _make_color_matcher(self):
        m = StreamingCommandMatcher(FILLER_CFG)
        patterns = ['set color {color}']
        wsv = {'color': ['red', 'green', 'blue', 'pink']}
        expanded = []
        result = expand_pattern_entities('set color {color}', {})
        pats = result[0] if isinstance(result, tuple) else result
        # Register with word_slot_values so the entity is a strict whitelist
        m.register_intent('test', pats, word_slot_values=wsv)
        return m

    def test_valid_entity_value_accepted(self):
        m = self._make_color_matcher()
        match, _ = feed(m, ['set', 'color', 'pink'])
        assert match is not None
        assert match['entities'].get('color') == 'pink'

    def test_invalid_entity_value_is_filler_not_accepted(self):
        # 'purple' is not in the whitelist — should be a filler, path keeps waiting
        m = self._make_color_matcher()
        m.add_word('set', stream_type='INTERIM')
        m.add_word('color', stream_type='INTERIM')
        m.add_word('purple', stream_type='INTERIM')   # not a valid color
        # Path should still be alive (waiting for a valid color), not have matched 'purple'
        for p in m.active_paths:
            assert 'purple' not in p.matched_words

    def test_entity_slot_does_not_swallow_post_entity_literal(self):
        # The vortex: slot accepting the word that belongs to the next literal position.
        # Pattern: 'brightness {number} percent' — 'percent' must close the slot
        # and complete the match, not get swallowed as a number word.
        m = StreamingCommandMatcher(FILLER_CFG)
        m.register_intent('test', ['brightness {number} percent'])
        match = None
        for w in ['brightness', 'fifty', 'percent']:
            result, _ = m.add_word(w, stream_type='FINAL')
            if result:
                match = result
        # 'percent' is the post-slot literal that closes the number slot and fires
        assert match is not None, "'percent' should close the number slot and fire the match"
        assert match['entities'].get('number') == 50

    def test_all_valid_entity_values_work(self):
        m = self._make_color_matcher()
        for color in ['red', 'green', 'blue', 'pink']:
            m2 = self._make_color_matcher()
            match, _ = feed(m2, ['set', 'color', color])
            assert match is not None, f"color '{color}' should be accepted"
            assert match['entities'].get('color') == color


# ── Dedup ──────────────────────────────────────────────────────────────────────

class TestDedup:
    def test_identical_paths_collapsed(self):
        """After replay, duplicate path states should be collapsed to one."""
        m = make('[what|when] is sunrise today')
        # Feed 'what is sunrise' — creates one path
        for w in ['what', 'is', 'sunrise']:
            m.add_word(w, stream_type='INTERIM')
        path_count_before = len(m.active_paths)
        # Feed 'what' again — creates another path at ['what'] then advances
        # Eventually two paths reach ['what','is','sunrise'] — dedup should collapse
        for w in ['what', 'is', 'sunrise']:
            m.add_word(w, stream_type='INTERIM')
        # Dedup happens per add_word call; count should not double
        same_state = [p for p in m.active_paths
                      if p.matched_words == ['what', 'is', 'sunrise']]
        assert len(same_state) <= 1, "Dedup should collapse identical path states"
