"""
End-to-end tests for decimal_list entity dispatch through StreamingCommandMatcher.

Verifies the full pipeline: spoken words → MatcherPath → check_completion →
entity values as list[float], including:
  - Even-count lists complete and dispatch correctly
  - Odd-count lists hold open (incomplete → no dispatch on FINAL boundary)
  - Hold-tail words keep slot open pending more input
  - Post-slot word closes the slot on INTERIM
  - 'then' separator works inside the slot
  - Implicit boundaries (sign word, second 'point') work without 'then'
  - Unrecognised words charge budget, not slot words

Run: .venv/bin/python -m pytest mycroft/client/realtime/tests/test_command_matcher_decimal_list.py -v
"""

import pytest
from mycroft.client.realtime.command_matcher import StreamingCommandMatcher


FILLER_CONFIG = {'base_max': 8, 'increment_per_word': 1}


def make_matcher(pattern='dot product {number_decimal_list}'):
    m = StreamingCommandMatcher(FILLER_CONFIG)
    m.register_intent('test:dot_product', [pattern])
    return m


def feed(matcher, words, stream_type='FINAL'):
    """Feed words then simulate FINAL boundary. Returns first match or None."""
    result = None
    for i, word in enumerate(words):
        match, _ = matcher.add_word(word, stream_type=stream_type, transcript_position=i)
        if match and result is None:
            result = match
    if result is None:
        result = matcher.close_slots_and_check()
    return result


# ── basic dispatch ────────────────────────────────────────────────────────────

class TestDecimalListDispatch:
    def test_two_values_then_separated(self):
        m = make_matcher()
        r = feed(m, 'dot product point two three then one point five'.split())
        assert r is not None
        assert r['entities']['number_decimal_list'] == pytest.approx([0.23, 1.5])

    def test_four_values_then_separated(self):
        m = make_matcher()
        words = 'dot product point two then one point five then negative point four then two point one'.split()
        r = feed(m, words)
        assert r is not None
        assert r['entities']['number_decimal_list'] == pytest.approx([0.2, 1.5, -0.4, 2.1])

    def test_sign_boundary_no_then(self):
        m = make_matcher()
        r = feed(m, 'dot product one point five negative two point three'.split())
        assert r is not None
        assert r['entities']['number_decimal_list'] == pytest.approx([1.5, -2.3])

    def test_bare_point_boundary_no_then(self):
        m = make_matcher()
        r = feed(m, 'dot product point two point five'.split())
        assert r is not None
        assert r['entities']['number_decimal_list'] == pytest.approx([0.2, 0.5])

    def test_whole_number_with_then(self):
        m = make_matcher()
        r = feed(m, 'dot product five then point three'.split())
        assert r is not None
        assert r['entities']['number_decimal_list'] == pytest.approx([5.0, 0.3])


# ── incomplete / hold ─────────────────────────────────────────────────────────

class TestDecimalListHold:
    def test_odd_count_holds_open(self):
        # Single value — odd count → should NOT dispatch
        m = make_matcher()
        r = feed(m, 'dot product point two three'.split())
        assert r is None

    def test_three_values_holds_open(self):
        m = make_matcher()
        words = 'dot product point two then one point five then negative point four'.split()
        r = feed(m, words)
        assert r is None

    def test_trailing_point_holds(self):
        # Ends with 'point' — hold-tail, slot stays open
        m = make_matcher()
        r = feed(m, 'dot product one point five point'.split())
        assert r is None

    def test_trailing_negative_holds(self):
        m = make_matcher()
        r = feed(m, 'dot product one point five negative'.split())
        assert r is None

    def test_trailing_then_holds(self):
        m = make_matcher()
        r = feed(m, 'dot product point two then'.split())
        assert r is None


# ── post-slot word closes on INTERIM ─────────────────────────────────────────

class TestDecimalListInterimClose:
    def test_post_slot_word_closes_on_interim(self):
        # Pattern: "dot product {number_decimal_list} [now|]"
        # Even list + 'now' post-slot word should close on INTERIM
        m = StreamingCommandMatcher(FILLER_CONFIG)
        m.register_intent('test:dp', ['dot product {number_decimal_list} [now|]'])

        words = 'dot product point two point five now'.split()
        result = None
        for i, word in enumerate(words):
            match, _ = m.add_word(word, stream_type='INTERIM', transcript_position=i)
            if match and result is None:
                result = match
        assert result is not None
        assert result['entities']['number_decimal_list'] == pytest.approx([0.2, 0.5])


# ── utterance and entity keys ─────────────────────────────────────────────────

class TestDecimalListEntityKeys:
    def test_entity_key_is_list(self):
        m = make_matcher()
        r = feed(m, 'dot product point two then point five'.split())
        assert r is not None
        val = r['entities']['number_decimal_list']
        assert isinstance(val, list)
        assert all(isinstance(v, float) for v in val)

    def test_utterance_contains_spoken_words(self):
        m = make_matcher()
        r = feed(m, 'dot product point two then point five'.split())
        assert r is not None
        assert 'dot product' in r['utterance']
        assert '__decimal_list__' not in r['utterance']

    def test_intent_name(self):
        m = make_matcher()
        r = feed(m, 'dot product point two then point five'.split())
        assert r is not None
        assert r['intent'] == 'test:dot_product'
