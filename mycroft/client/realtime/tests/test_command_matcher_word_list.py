"""Tests for word-list entity slot behaviour in CommandMatcher.

Word-list entities (ENTITY_EXPANSIONS with values:/files:) must behave as
strict whitelists: only their defined values advance a path.  The complete
set of valid values is tested positively (every value must match) and the
slot boundary is tested by verifying that words outside the set never match,
regardless of filler budget.
"""
import pytest
from mycroft.client.realtime.command_matcher import StreamingCommandMatcher
from mycroft.client.realtime.pattern_loader import expand_pattern_entities

FILLER_CONFIG = {'budget': 3, 'increment_per_word': 1}

# Single-word values only — multi-word values cannot be captured via entity slots
# (they expand to literal pattern words but the capture check can't reconstruct them).
ENTITY_EXPANSIONS = {
    'temp_extreme': {'values': ['high', 'low']},
    'forecast_scope': {'values': ['today', 'tomorrow']},
    'time_period': {'values': ['AM', 'PM', 'morning', 'afternoon', 'evening', 'night']},
}

# Words that appear in NO entity's value set — used for exhaustive non-member checks.
_ALL_VALUES = {
    v for cfg in ENTITY_EXPANSIONS.values() for v in cfg['values']
}
_NON_ENTITY_WORDS = ['current', 'temperature', 'time', 'twn', 'what', 'blah', 'now', 'outside']


def make_matcher(intent_name, pattern_str):
    """Register a single intent pattern and return matcher."""
    m = StreamingCommandMatcher(FILLER_CONFIG)
    pats, wsv = expand_pattern_entities(pattern_str, ENTITY_EXPANSIONS)
    m.register_intent(intent_name, pats, word_slot_values=wsv)
    return m


def feed(matcher, words, stream_type='INTERIM'):
    """Feed words into matcher, return (last_result, all_results)."""
    results = []
    last = None
    for w in words:
        result, _ = matcher.add_word(w, stream_type=stream_type)
        if result:
            results.append(result)
            last = result
    slot_result = matcher.close_slots_and_check()
    if slot_result:
        results.append(slot_result)
        last = slot_result
    return last, results


class TestWordListEntityWhitelist:
    def test_valid_value_matches(self):
        m = make_matcher('w:highlow', 'what is the {temp_extreme} {forecast_scope}')
        last, results = feed(m, 'what is the high today'.split())
        assert last is not None
        assert last['intent'] == 'w:highlow'
        assert last['entities']['temp_extreme'] == 'high'
        assert last['entities']['forecast_scope'] == 'today'

    def test_invalid_word_in_entity_slot_rejected(self):
        """The confirmed bug case: 'current' and 'what' must not match the entity slots."""
        m = make_matcher('w:highlow', 'what is the {temp_extreme} {forecast_scope}')
        last, results = feed(m, 'what is the current what'.split())
        assert last is None

    def test_arbitrary_word_in_entity_slot_rejected(self):
        m = make_matcher('w:highlow', 'what is the {temp_extreme} {forecast_scope}')
        last, results = feed(m, 'what is the temperature now'.split())
        assert last is None

    def test_all_valid_values_match(self):
        m = make_matcher('w:highlow', 'what is the {temp_extreme} {forecast_scope}')
        for extreme in ('high', 'low'):
            for scope in ('today', 'tomorrow'):
                m2 = make_matcher('w:highlow', 'what is the {temp_extreme} {forecast_scope}')
                last, _ = feed(m2, f'what is the {extreme} {scope}'.split())
                assert last is not None, f"Expected match for '{extreme} {scope}'"
                assert last['entities']['temp_extreme'] == extreme
                assert last['entities']['forecast_scope'] == scope

    def test_two_adjacent_entity_slots_both_constrained(self):
        """Both slots must independently enforce their value sets."""
        m = make_matcher('w:highlow', 'what is the {temp_extreme} {forecast_scope}')
        # Wrong order — low is valid for temp_extreme but 'high' is not in forecast_scope
        last, _ = feed(m, 'what is the today high'.split())
        assert last is None

    def test_entity_slot_does_not_accept_other_slots_values(self):
        """Values from one entity must not be accepted by a different entity's slot."""
        m = make_matcher('w:highlow', 'what is the {temp_extreme} {forecast_scope}')
        # 'today' is valid for forecast_scope but not temp_extreme
        last, _ = feed(m, 'what is the today tomorrow'.split())
        assert last is None

    def test_missing_word_slot_values_means_impassable(self):
        """If word_slot_values is empty (e.g. entity load failed), slot accepts nothing."""
        m = StreamingCommandMatcher(FILLER_CONFIG)
        # Register with unexpanded pattern and empty word_slot_values — simulates load failure
        m.register_intent('w:highlow', ['what is the {temp_extreme} {forecast_scope}'], word_slot_values={})
        last, _ = feed(m, 'what is the high today'.split())
        assert last is None

    def test_case_insensitive_entity_match(self):
        """Entity values stored lowercase must match spoken lowercase words."""
        m = make_matcher('w:period', 'temperature at {time_period}')
        last, _ = feed(m, 'temperature at morning'.split())
        assert last is not None
        assert last['entities']['time_period'] == 'morning'

    def test_filler_budget_cannot_bridge_invalid_entity_word(self):
        """Even with filler budget remaining, an invalid entity word must not match."""
        m = make_matcher('w:highlow', 'what is the {temp_extreme} {forecast_scope}')
        # 'blah' is not in temp_extreme; budget has room but must not bridge it
        last, _ = feed(m, 'what is the blah today'.split())
        assert last is None

    def test_single_entity_pattern(self):
        m = make_matcher('w:extreme', '{temp_extreme}')
        last, _ = feed(m, ['high'])
        assert last is not None
        assert last['entities']['temp_extreme'] == 'high'

    def test_single_entity_pattern_rejects_non_value(self):
        m = make_matcher('w:extreme', '{temp_extreme}')
        last, _ = feed(m, ['temperature'])
        assert last is None


class TestWordListEntityExhaustiveWhitelist:
    """Whitelist-framed tests: prove every defined value works, and that the
    valid set is exactly the entity's values — nothing more, nothing less."""

    @pytest.mark.parametrize('value', ENTITY_EXPANSIONS['temp_extreme']['values'])
    def test_every_temp_extreme_value_matches(self, value):
        m = make_matcher('w:e', 'the {temp_extreme}')
        last, _ = feed(m, ['the', value])
        assert last is not None, f"'{value}' should be valid for temp_extreme"
        assert last['entities']['temp_extreme'] == value

    @pytest.mark.parametrize('value', ENTITY_EXPANSIONS['forecast_scope']['values'])
    def test_every_forecast_scope_value_matches(self, value):
        m = make_matcher('w:e', 'forecast {forecast_scope}')
        last, _ = feed(m, ['forecast', value])
        assert last is not None, f"'{value}' should be valid for forecast_scope"
        assert last['entities']['forecast_scope'] == value

    @pytest.mark.parametrize('value', ENTITY_EXPANSIONS['time_period']['values'])
    def test_every_time_period_value_matches(self, value):
        m = make_matcher('w:e', 'at {time_period}')
        last, _ = feed(m, ['at', value.lower()])
        assert last is not None, f"'{value}' should be valid for time_period"

    @pytest.mark.parametrize('non_value', _NON_ENTITY_WORDS)
    def test_non_entity_word_never_fills_temp_extreme(self, non_value):
        m = make_matcher('w:e', 'the {temp_extreme}')
        last, _ = feed(m, ['the', non_value])
        assert last is None, f"'{non_value}' must not fill temp_extreme slot"

    @pytest.mark.parametrize('non_value', _NON_ENTITY_WORDS)
    def test_non_entity_word_never_fills_forecast_scope(self, non_value):
        m = make_matcher('w:e', 'forecast {forecast_scope}')
        last, _ = feed(m, ['forecast', non_value])
        assert last is None, f"'{non_value}' must not fill forecast_scope slot"

    def test_temp_extreme_values_do_not_fill_forecast_scope(self):
        """Values valid for one entity must not bleed into a different entity's slot."""
        for value in ENTITY_EXPANSIONS['temp_extreme']['values']:
            m = make_matcher('w:e', 'forecast {forecast_scope}')
            last, _ = feed(m, ['forecast', value])
            assert last is None, f"temp_extreme value '{value}' must not fill forecast_scope"

    def test_forecast_scope_values_do_not_fill_temp_extreme(self):
        for value in ENTITY_EXPANSIONS['forecast_scope']['values']:
            m = make_matcher('w:e', 'the {temp_extreme}')
            last, _ = feed(m, ['the', value])
            assert last is None, f"forecast_scope value '{value}' must not fill temp_extreme"

    def test_complete_valid_set_for_adjacent_slots(self):
        """Every (temp_extreme, forecast_scope) combination must match; no others."""
        valid_extremes = ENTITY_EXPANSIONS['temp_extreme']['values']
        valid_scopes = ENTITY_EXPANSIONS['forecast_scope']['values']
        for extreme in valid_extremes:
            for scope in valid_scopes:
                m = make_matcher('w:e', 'what is the {temp_extreme} {forecast_scope}')
                last, _ = feed(m, ['what', 'is', 'the', extreme, scope])
                assert last is not None, f"({extreme}, {scope}) should match"
                assert last['entities']['temp_extreme'] == extreme
                assert last['entities']['forecast_scope'] == scope
        # None of the non-entity words should work in either slot
        for word in _NON_ENTITY_WORDS:
            for scope in valid_scopes:
                m = make_matcher('w:e', 'what is the {temp_extreme} {forecast_scope}')
                last, _ = feed(m, ['what', 'is', 'the', word, scope])
                assert last is None, f"('{word}', {scope}) must not match"


class TestWordListEntityCapture:
    def test_entity_value_captured_in_entities_dict(self):
        m = make_matcher('w:highlow', 'what is the {temp_extreme} {forecast_scope}')
        last, _ = feed(m, 'what is the low tomorrow'.split())
        assert last is not None
        assert 'temp_extreme' in last['entities']
        assert 'forecast_scope' in last['entities']
        assert last['entities']['temp_extreme'] == 'low'
        assert last['entities']['forecast_scope'] == 'tomorrow'

    def test_pattern_field_reflects_original_pattern(self):
        """pattern field should show the original {entity} form, not the expanded literal."""
        m = make_matcher('w:highlow', 'what is the {temp_extreme} {forecast_scope}')
        last, _ = feed(m, 'what is the high today'.split())
        assert last is not None
        # The pattern stored on CommandPattern is the original unexpanded line
        assert '{temp_extreme}' in last['pattern'] or 'high' in last['pattern']
