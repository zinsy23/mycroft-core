"""
End-to-end tests for calc entity dispatch through StreamingCommandMatcher.

These tests exercise the full pipeline from spoken words → MatcherPath →
check_completion → entity values, including:
  - Rounding applied via calc_entity_configs (not just number_parser in isolation)
  - calc_entity_configs correctly threaded to MatcherPath at construction
  - Filler words don't corrupt slot buffer or entity output
  - Incomplete expressions hold the path alive (no premature dispatch)
  - Correct entity keys present in match result

Run: .venv/bin/python -m pytest mycroft/client/realtime/tests/test_command_matcher_calc.py -v
"""

import math
import pytest
from mycroft.client.realtime.command_matcher import StreamingCommandMatcher


FILLER_CONFIG = {'base_max': 6, 'increment_per_word': 1}


def make_matcher(pattern='what is {number_calc}', calc_cfg=None):
    """Create a matcher with a single calc intent and optional entity config."""
    m = StreamingCommandMatcher(FILLER_CONFIG)
    if calc_cfg is not None:
        m.calc_entity_configs['number_calc'] = calc_cfg
    m.register_intent('test:calc', [pattern])
    return m


def feed(matcher, words, stream_type='FINAL'):
    """Feed a list of words, then simulate the FINAL boundary slot-close.

    Returns the first match from word processing, or from the slot-close boundary.
    """
    result = None
    for i, word in enumerate(words):
        match, _ = matcher.add_word(word, stream_type=stream_type, transcript_position=i)
        if match and result is None:
            result = match
    if result is None:
        result = matcher.close_slots_and_check()
    return result


# ── basic dispatch ────────────────────────────────────────────────────────────

class TestCalcEntityDispatch:
    def test_simple_expression_matches(self):
        m = make_matcher()
        r = feed(m, ['what', 'is', 'five', 'plus', 'three'])
        assert r is not None
        assert r['intent'] == 'test:calc'
        assert r['entities']['number_calc'] == 8

    def test_entity_keys_present(self):
        m = make_matcher()
        r = feed(m, ['what', 'is', 'five', 'plus', 'three'])
        assert r is not None
        ents = r['entities']
        assert 'number_calc' in ents
        assert 'number_calc_expr' in ents
        assert 'number_calc_tokens' in ents

    def test_integer_result_is_int(self):
        m = make_matcher()
        r = feed(m, ['what', 'is', 'ten', 'divided', 'by', 'two'])
        assert r is not None
        assert r['entities']['number_calc'] == 5
        assert isinstance(r['entities']['number_calc'], int)

    def test_multiplication(self):
        m = make_matcher()
        r = feed(m, ['what', 'is', 'three', 'times', 'seven'])
        assert r is not None
        assert r['entities']['number_calc'] == 21

    def test_power(self):
        m = make_matcher()
        r = feed(m, ['what', 'is', 'two', 'squared'])
        assert r is not None
        assert r['entities']['number_calc'] == 4


# ── rounding via calc_entity_configs ─────────────────────────────────────────

class TestCalcEntityRounding:
    def test_no_config_returns_full_precision(self):
        m = make_matcher(calc_cfg=None)
        r = feed(m, ['what', 'is', 'ten', 'divided', 'by', 'three'])
        assert r is not None
        # Without config, format_calc_result uses default of 2 decimal places
        assert r['entities']['number_calc'] == round(10/3, 2)

    def test_global_decimal_places(self):
        m = make_matcher(calc_cfg={'decimal_places': 3})
        r = feed(m, ['what', 'is', 'ten', 'divided', 'by', 'three'])
        assert r is not None
        assert r['entities']['number_calc'] == round(10/3, 3)

    def test_root_uses_override(self):
        m = make_matcher(calc_cfg={'decimal_places': 2, 'decimal_places_root': 4})
        r = feed(m, ['what', 'is', 'square', 'root', 'two'])
        assert r is not None
        assert r['entities']['number_calc'] == round(math.sqrt(2), 4)

    def test_root_override_doesnt_affect_division(self):
        m = make_matcher(calc_cfg={'decimal_places': 2, 'decimal_places_root': 4})
        r = feed(m, ['what', 'is', 'ten', 'divided', 'by', 'three'])
        assert r is not None
        assert r['entities']['number_calc'] == round(10/3, 2)

    def test_division_override(self):
        m = make_matcher(calc_cfg={'decimal_places': 2, 'decimal_places_root': 4})
        # No 'decimal_places_divide' key — falls back to global
        r = feed(m, ['what', 'is', 'one', 'divided', 'by', 'seven'])
        assert r is not None
        assert r['entities']['number_calc'] == round(1/7, 2)

    def test_integer_result_never_rounded(self):
        m = make_matcher(calc_cfg={'decimal_places': 2})
        r = feed(m, ['what', 'is', 'three', 'times', 'four'])
        assert r is not None
        assert r['entities']['number_calc'] == 12
        assert isinstance(r['entities']['number_calc'], int)

    def test_config_threads_to_new_paths(self):
        # Ensure calc_entity_configs is passed to paths created mid-stream.
        # Feed a filler before the trigger to force a new path creation.
        m = make_matcher(calc_cfg={'decimal_places': 2, 'decimal_places_root': 4})
        r = feed(m, ['um', 'what', 'is', 'square', 'root', 'two'])
        assert r is not None
        assert r['entities']['number_calc'] == round(math.sqrt(2), 4)


# ── filler words don't corrupt slot or entity ─────────────────────────────────

class TestCalcFillerWords:
    def test_filler_before_trigger_ok(self):
        m = make_matcher()
        r = feed(m, ['um', 'what', 'is', 'five', 'plus', 'three'])
        assert r is not None
        assert r['entities']['number_calc'] == 8

    def test_filler_of_not_in_slot(self):
        # 'of' is not in CALC_WORDS — treated as filler, never enters slot buffer.
        # 'square root sixty four' (without 'of') must still match.
        m = make_matcher()
        r = feed(m, ['what', 'is', 'square', 'root', 'sixty', 'four'])
        assert r is not None
        assert r['entities']['number_calc'] == pytest.approx(8.0, rel=1e-9)

    def test_power_bare(self):
        # 'power' alone between numbers — no surrounding words
        m = make_matcher()
        assert feed(m, ['what', 'is', 'two', 'power', 'eight'])['entities']['number_calc'] == 256

    def test_raised_bare(self):
        # 'raised' alone between numbers — no 'power' present
        m = make_matcher()
        assert feed(m, ['what', 'is', 'two', 'raised', 'eight'])['entities']['number_calc'] == 256

    def test_power_with_filler_words(self):
        # All common spoken variants — fillers (to, the, of, raised+power) handled by budget
        cases = [
            ['what', 'is', 'two', 'to', 'the', 'power', 'eight'],
            ['what', 'is', 'two', 'to', 'the', 'power', 'of', 'eight'],
            ['what', 'is', 'two', 'raised', 'to', 'the', 'power', 'eight'],
            ['what', 'is', 'two', 'raised', 'to', 'the', 'power', 'of', 'eight'],
            ['what', 'is', 'two', 'raised', 'to', 'eight'],       # raised alone, 'to' filler
            ['what', 'is', 'two', 'raised', 'power', 'eight'],    # both in buffer, raised defers
        ]
        for words in cases:
            m = make_matcher()
            r = feed(m, words)
            assert r is not None and r['entities']['number_calc'] == 256, \
                f"Failed for: {' '.join(words)}"

    def test_square_root_with_filler_of(self):
        # 'of' is a filler — 'square root of sixty four' still matches
        m = make_matcher()
        r = feed(m, ['what', 'is', 'square', 'root', 'of', 'sixty', 'four'])
        assert r is not None
        assert r['entities']['number_calc'] == pytest.approx(8.0, rel=1e-9)

    def test_absolute_value_with_filler_of(self):
        # 'of' is a filler — 'absolute value of negative five' still matches
        m = make_matcher()
        r = feed(m, ['what', 'is', 'absolute', 'value', 'of', 'negative', 'five'])
        assert r is not None
        assert r['entities']['number_calc'] == 5

    def test_absolute_value_without_of(self):
        m = make_matcher()
        r = feed(m, ['what', 'is', 'absolute', 'value', 'negative', 'five'])
        assert r is not None
        assert r['entities']['number_calc'] == 5


# ── incomplete expressions don't dispatch prematurely ────────────────────────

class TestCalcIncompleteNoDispatch:
    def test_trailing_operator_no_match(self):
        # "what is five times" — incomplete, should not dispatch
        m = make_matcher()
        r = feed(m, ['what', 'is', 'five', 'times'])
        assert r is None

    def test_bare_number_no_match(self):
        # "what is five" — no operation, should not dispatch
        m = make_matcher()
        r = feed(m, ['what', 'is', 'five'])
        assert r is None

    def test_completes_on_next_final(self):
        # Simulate two FINAL streams: first "five times" (incomplete),
        # then "five times three" (complete). Should dispatch on second.
        m = make_matcher()
        # First FINAL — words arrive but slot-close should find incomplete expr → None
        for i, w in enumerate(['what', 'is', 'five', 'times']):
            m.add_word(w, stream_type='FINAL', transcript_position=i)
        r1 = m.close_slots_and_check()
        assert r1 is None
        # Second FINAL adds 'three' — slot-close should now complete
        m.add_word('three', stream_type='FINAL', transcript_position=4)
        r2 = m.close_slots_and_check()
        assert r2 is not None
        assert r2['entities']['number_calc'] == 15
