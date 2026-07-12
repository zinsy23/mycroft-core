"""
Tests for number_parser.py — spoken number → integer/expression conversion.

Covers:
  - words_to_int: basic, magnitudes, implied-one, a→one, signed, edge cases
  - words_to_calc: all operators, precedence, power/root, absolute value,
                   decimals, modulo, sign disambiguation, incomplete expressions
                   (things that should return None — FINAL should not act yet)
  - CALC_WORDS / NUMBER_WORDS membership (slot next-word sets)

Run: .venv/bin/python -m pytest mycroft/client/realtime/tests/test_number_parser.py -v
"""

import math
import pytest
from mycroft.client.realtime.number_parser import (
    words_to_int,
    words_to_calc,
    NUMBER_WORDS,
    CALC_WORDS,
    SIGN_WORDS,
    DECIMAL_WORDS,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def calc(phrase):
    """Return the numeric result of words_to_calc, or None.
    Passes the phrase directly — no slot buffer filtering. Use for testing
    the tokenizer with already-filtered input (no filler words)."""
    r = words_to_calc(phrase)
    return r['result'] if r else None


def calc_via_slot(phrase):
    """Simulate what reaches words_to_calc after the matcher's slot buffer filter.
    Strips any word not in CALC_WORDS, mirroring what the slot accumulator does.
    Use for testing spoken phrases that include filler words (of, by, the, to...)."""
    filtered = ' '.join(w for w in phrase.lower().split() if w in CALC_WORDS)
    r = words_to_calc(filtered)
    return r['result'] if r else None


def approx(expected, rel=1e-9):
    return pytest.approx(expected, rel=rel)


# ── words_to_int ─────────────────────────────────────────────────────────────

class TestWordsToIntBasic:
    def test_zero(self):
        assert words_to_int('zero') == 0

    def test_ones(self):
        assert words_to_int('one') == 1
        assert words_to_int('nine') == 9
        assert words_to_int('nineteen') == 19

    def test_tens(self):
        assert words_to_int('twenty') == 20
        assert words_to_int('ninety') == 90

    def test_tens_and_ones(self):
        assert words_to_int('forty two') == 42
        assert words_to_int('seventy eight') == 78

    def test_hundreds(self):
        assert words_to_int('one hundred') == 100
        assert words_to_int('three hundred') == 300
        assert words_to_int('one hundred thirty eight') == 138
        assert words_to_int('nine hundred ninety nine') == 999

    def test_hundreds_with_and(self):
        assert words_to_int('one hundred and thirty eight') == 138
        assert words_to_int('five hundred and one') == 501

    def test_thousands(self):
        assert words_to_int('one thousand') == 1000
        assert words_to_int('two thousand') == 2000
        assert words_to_int('ten thousand') == 10_000
        assert words_to_int('thirteen thousand') == 13_000
        assert words_to_int('three thousand five hundred') == 3500
        assert words_to_int('twelve thousand three hundred forty five') == 12_345

    def test_millions(self):
        assert words_to_int('one million') == 1_000_000
        assert words_to_int('one million four hundred thousand') == 1_400_000
        assert words_to_int('two million five hundred thousand three hundred') == 2_500_300

    def test_billions(self):
        assert words_to_int('one billion') == 1_000_000_000
        assert words_to_int('three billion two hundred million') == 3_200_000_000


class TestWordsToIntImpliedOne:
    """Bare magnitude words with no explicit coefficient → implies 1."""

    def test_bare_hundred(self):
        assert words_to_int('hundred') == 100

    def test_hundred_with_remainder(self):
        assert words_to_int('hundred thirteen') == 113
        assert words_to_int('hundred and thirteen') == 113
        assert words_to_int('hundred fifty seven') == 157

    def test_bare_thousand(self):
        assert words_to_int('thousand') == 1000

    def test_thousand_with_remainder(self):
        assert words_to_int('thousand five hundred') == 1500
        assert words_to_int('thousand two hundred thirty four') == 1234

    def test_a_before_magnitude(self):
        """'a' before a magnitude word → treated as 'one'."""
        assert words_to_int('a hundred') == 100
        assert words_to_int('a hundred sixty nine') == 169
        assert words_to_int('a thousand') == 1000
        assert words_to_int('a thousand five hundred') == 1500
        assert words_to_int('a million') == 1_000_000

    def test_a_not_before_magnitude(self):
        """Bare 'a' not followed by a magnitude → None."""
        assert words_to_int('a') is None
        assert words_to_int('a five') is None


class TestWordsToIntSigned:
    def test_negative(self):
        assert words_to_int('negative five') == -5
        assert words_to_int('negative one hundred') == -100

    def test_minus(self):
        assert words_to_int('minus three') == -3

    def test_sign_only(self):
        assert words_to_int('negative') is None
        assert words_to_int('minus') is None


class TestWordsToIntInvalid:
    def test_empty(self):
        assert words_to_int('') is None

    def test_garbage(self):
        assert words_to_int('blah blah') is None

    def test_leftover_tokens(self):
        assert words_to_int('five plus three') is None

    def test_magnitude_only_no_valid_parse(self):
        # standard fails, concat handles: "hundred hundred" → 100100
        assert words_to_int('hundred hundred') == 100100
        assert words_to_int('hundred one hundred') == 101100


# ── positional concatenation ─────────────────────────────────────────────────

class TestWordsToIntConcat:
    """Positional (string) concatenation of spoken number groups.

    Groups that ascend in place value start a new concat chunk.
    Standard magnitude words (hundred/thousand/etc.) → standard parse first,
    concat as fallback. No magnitude words → concat first.
    """

    # year-style two-group concat
    def test_nineteen_forty_one(self):
        assert words_to_int('nineteen forty one') == 1941

    def test_eighteen_sixty_five(self):
        assert words_to_int('eighteen sixty five') == 1865

    def test_twenty_nineteen(self):
        # teens after tens → ascending place value → two groups
        assert words_to_int('twenty nineteen') == 2019

    def test_nineteen_ninety_nine(self):
        assert words_to_int('nineteen ninety nine') == 1999

    def test_nine_eleven(self):
        assert words_to_int('nine eleven') == 911

    # three-group concat
    def test_thirteen_six_fifty(self):
        assert words_to_int('thirteen six fifty') == 13650

    # standard two-word numbers still work (place descends → one group)
    def test_forty_two_stays_standard(self):
        assert words_to_int('forty two') == 42

    def test_twenty_nine_stays_standard(self):
        assert words_to_int('twenty nine') == 29

    def test_ninety_nine_stays_standard(self):
        assert words_to_int('ninety nine') == 99

    # colloquial hundred-based forms (standard wins, already works)
    def test_fourteen_hundred(self):
        assert words_to_int('fourteen hundred') == 1400

    def test_thirteen_hundred(self):
        assert words_to_int('thirteen hundred') == 1300

    def test_thirteen_hundred_fifty(self):
        assert words_to_int('thirteen hundred fifty') == 1350

    # signed concat
    def test_negative_concat(self):
        assert words_to_int('negative nineteen forty one') == -1941

    def test_minus_concat(self):
        assert words_to_int('minus twenty nineteen') == -2019

    # magnitude-word concat fallback
    def test_hundred_hundred(self):
        # standard fails → concat → 100100
        assert words_to_int('hundred hundred') == 100100

    def test_hundred_one_hundred(self):
        assert words_to_int('hundred one hundred') == 101100


# ── words_to_calc — basic arithmetic ─────────────────────────────────────────

class TestCalcAddition:
    def test_basic(self):
        assert calc('five plus three') == 8

    def test_add_alias(self):
        assert calc('five add three') == 8
        assert calc('five added three') == 8

    def test_large(self):
        assert calc('one hundred plus two hundred') == 300

    def test_chain(self):
        assert calc('one plus two plus three') == 6


class TestCalcSubtraction:
    def test_basic(self):
        assert calc('ten minus four') == 6

    def test_subtract_alias(self):
        assert calc('ten subtract four') == 6
        assert calc('ten subtracted four') == 6

    def test_result_negative(self):
        assert calc('three minus ten') == -7


class TestCalcMultiplication:
    def test_basic(self):
        assert calc('three times seven') == 21

    def test_multiplied_by(self):
        assert calc_via_slot('three multiplied by seven') == 21

    def test_multiply_alias(self):
        assert calc('three multiply seven') == 21


class TestCalcDivision:
    def test_basic(self):
        assert calc_via_slot('ten divided by two') == 5

    def test_divide_alias(self):
        assert calc('ten divide two') == 5

    def test_over_alias(self):
        assert calc('ten over two') == 5

    def test_fractional_result(self):
        assert calc_via_slot('five divided by two') == approx(2.5)

    def test_division_by_zero(self):
        assert calc_via_slot('five divided by zero') is None


class TestCalcModulo:
    def test_mod(self):
        assert calc('ten mod three') == 1

    def test_modulo(self):
        assert calc('ten modulo three') == 1

    def test_remainder(self):
        assert calc('seventeen remainder five') == 2

    def test_modulo_by_zero(self):
        assert calc('five mod zero') is None

    def test_exact_division(self):
        assert calc('nine mod three') == 0


# ── operator precedence ───────────────────────────────────────────────────────

class TestCalcPrecedence:
    def test_multiply_before_add(self):
        assert calc('two plus three times four') == 14  # 2 + (3*4)

    def test_multiply_before_subtract(self):
        assert calc('ten minus two times three') == 4   # 10 - (2*3)

    def test_divide_before_add(self):
        assert calc_via_slot('eight plus six divided by two') == 11  # 8 + (6/2)

    def test_chain_multiply_add(self):
        assert calc('two times three plus four times five') == 26  # (2*3)+(4*5)

    def test_power_before_multiply(self):
        assert calc('two times three squared') == 18  # 2*(3^2)

    def test_power_right_associative(self):
        # 2^(3^2) = 2^9 = 512, not (2^3)^2 = 64
        # Slot buffer has 'two power three power two' ('to', 'the' are fillers)
        assert calc('two power three power two') == 512


# ── sign disambiguation ───────────────────────────────────────────────────────

class TestCalcSignDisambiguation:
    def test_signed_operand(self):
        assert calc('negative five plus three') == -2

    def test_minus_as_sign(self):
        assert calc('minus five plus three') == -2

    def test_minus_as_operator(self):
        assert calc('five minus three') == 2

    def test_signed_second_operand(self):
        # 'minus' after number → subtraction; 'negative' after operator → sign
        assert calc('ten minus negative three') == 13

    def test_negative_times_negative(self):
        assert calc('negative three times minus two') == 6

    def test_signed_after_operator(self):
        assert calc('ten plus negative five') == 5

    def test_minus_negative(self):
        # "three minus negative five" = 3 - (-5) = 8
        assert calc('three minus negative five') == 8
        assert calc('three minus minus five') == 8

    def test_negative_minus_negative(self):
        assert calc('negative three minus negative two') == -1

    def test_times_negative(self):
        assert calc('two times negative four') == -8

    def test_divide_negative(self):
        assert calc('negative six divided negative two') == 3


# ── power / exponent ──────────────────────────────────────────────────────────

class TestCalcPower:
    def test_squared(self):
        assert calc('five squared') == 25

    def test_cubed(self):
        assert calc('three cubed') == 27

    def test_power_bare(self):
        # Slot buffer only contains CALC_WORDS — 'to', 'the', 'of' are fillers.
        # 'power' alone between numbers is the signal.
        assert calc('two power eight') == 256

    def test_raised_bare(self):
        # 'raised' alone between numbers also means exponent
        assert calc('two raised eight') == 256

    def test_raised_and_power_together(self):
        # Both in buffer — 'raised' defers to 'power'
        assert calc('two raised power eight') == 256

    def test_power_in_expression(self):
        assert calc('five squared plus two') == 27
        assert calc('two cubed times three') == 24
        assert calc('five squared plus two power three') == 33

    def test_power_right_assoc_raised(self):
        # 'raised' is also right-associative: 2^(3^2) = 512
        assert calc('two raised three raised two') == 512

    def test_power_of_one(self):
        assert calc('two power one') == 2

    def test_power_of_zero(self):
        assert calc('five power zero') == 1

    def test_decimal_base(self):
        assert calc('two point five power two') == approx(6.25)

    def test_negative_base(self):
        assert calc('negative two power three') == approx(-8)


# ── square / cube root ────────────────────────────────────────────────────────

class TestCalcRoot:
    def test_square_root(self):
        # slot buffer strips 'of' — tokenizer sees 'square root sixty four'
        assert calc('square root sixty four') == approx(8.0)
        assert calc_via_slot('square root of sixty four') == approx(8.0)

    def test_cube_root(self):
        assert calc('cube root twenty seven') == approx(3.0)
        assert calc_via_slot('cube root of twenty seven') == approx(3.0)

    def test_square_root_non_perfect(self):
        assert calc('square root sixty five') == approx(math.sqrt(65))

    def test_cube_root_negative(self):
        assert calc('cube root negative twenty seven') == approx(-3.0)

    def test_root_in_expression(self):
        assert calc('square root sixty four plus one') == approx(9.0)

    def test_root_of_decimal(self):
        assert calc('square root two point two five') == approx(1.5)

    def test_chained_root(self):
        # Chained prefix unary not supported — prefix handler expects a number
        # after the phrase, not another prefix. Returns None (incomplete).
        assert calc('square root square root sixteen') is None


# ── absolute value ────────────────────────────────────────────────────────────

class TestCalcAbsoluteValue:
    def test_negative(self):
        assert calc('absolute value negative five') == 5

    def test_minus(self):
        assert calc('absolute value minus five') == 5

    def test_positive(self):
        assert calc('absolute value five') == 5

    def test_decimal(self):
        assert calc('absolute value negative three point six') == approx(3.6)

    def test_in_expression(self):
        assert calc('absolute value negative three plus one') == 4

    def test_abs_captures_one_operand(self):
        # abs captures the immediately following number only.
        # "absolute value three minus ten" = abs(3) - 10 = -7, NOT abs(3-10) = 7
        assert calc('absolute value three minus ten') == -7

    def test_of_is_filler(self):
        # 'of' never enters the slot buffer — words_to_calc only sees filtered input.
        assert calc('absolute value negative five') == 5
        # calc_via_slot strips 'of' before calling words_to_calc, same as the matcher.
        assert calc_via_slot('absolute value of negative five') == 5


# ── decimal input ─────────────────────────────────────────────────────────────

class TestCalcDecimal:
    def test_point(self):
        assert calc('three point five times two') == approx(7.0)

    def test_dot(self):
        assert calc('three dot five times two') == approx(7.0)

    def test_multi_digit_fraction(self):
        assert calc('one point two five times four') == approx(5.0)

    def test_decimal_plus_decimal(self):
        assert calc('one point five plus two point five') == approx(4.0)

    def test_negative_decimal(self):
        assert calc('negative two point five times four') == approx(-10.0)

    def test_decimal_in_absolute_value(self):
        assert calc('absolute value negative three point six') == approx(3.6)


# ── incomplete expressions — FINAL should not act ─────────────────────────────
# These represent partial utterances that arrive in an interim FINAL before
# the speaker finishes. words_to_calc must return None so the matcher waits
# for the next FINAL rather than committing a wrong result.

class TestCalcIncomplete:
    def test_bare_number_no_op(self):
        assert calc('five') is None

    def test_bare_signed_number(self):
        assert calc('negative five') is None

    def test_bare_decimal_no_op(self):
        assert calc('three point five') is None

    def test_trailing_operator(self):
        # "five times" — operator with no right operand
        assert calc('five times') is None

    def test_trailing_plus(self):
        assert calc('ten plus') is None

    def test_trailing_power_keyword(self):
        # "five power" — right operand missing (filler words stripped by slot)
        assert calc('five power') is None

    def test_trailing_raised_to(self):
        # 'raised' aliases to 'power', 'to' is a filler stripped by slot
        assert calc('five power') is None

    def test_prefix_unary_no_operand(self):
        assert calc('square root') is None
        assert calc('absolute value') is None

    def test_point_no_digits(self):
        # "three point" — decimal started but no digit words follow
        assert calc('three point') is None

    def test_operator_no_left_operand(self):
        assert calc('plus five') is None
        assert calc('times three') is None

    def test_divide_by_zero(self):
        assert calc_via_slot('ten divided by zero') is None

    def test_mod_by_zero(self):
        assert calc('ten mod zero') is None


# ── word set membership ───────────────────────────────────────────────────────
# Ensures the slot next-word sets contain expected words and exclude words
# that should be fillers (never entering the slot buffer).

# ── result metadata — operation type surfaced in return dict ──────────────────
# words_to_calc returns 'operation' key so skills can apply per-operation
# rounding without having to re-parse the tokens themselves.

class TestCalcOperationMetadata:
    """The return dict includes an 'operation' key identifying the top-level
    operation type. Skills use this to choose per-operation decimal places."""

    def test_addition(self):
        r = words_to_calc('five plus three')
        assert r['operation'] == 'add'

    def test_subtraction(self):
        r = words_to_calc('ten minus four')
        assert r['operation'] == 'subtract'

    def test_multiplication(self):
        r = words_to_calc('three times seven')
        assert r['operation'] == 'multiply'

    def test_division(self):
        r = words_to_calc('ten divided two')
        assert r['operation'] == 'divide'

    def test_modulo(self):
        r = words_to_calc('ten mod three')
        assert r['operation'] == 'modulo'

    def test_power(self):
        r = words_to_calc('two power eight')
        assert r['operation'] == 'power'

    def test_squared(self):
        r = words_to_calc('five squared')
        assert r['operation'] == 'power'

    def test_cubed(self):
        r = words_to_calc('three cubed')
        assert r['operation'] == 'power'

    def test_square_root(self):
        r = words_to_calc('square root sixty four')
        assert r['operation'] == 'root'

    def test_cube_root(self):
        r = words_to_calc('cube root twenty seven')
        assert r['operation'] == 'root'

    def test_absolute_value(self):
        r = words_to_calc('absolute value negative five')
        assert r['operation'] == 'abs'

    def test_mixed_expression_uses_dominant(self):
        # When multiple operators are present, 'operation' is 'mixed'
        assert words_to_calc('two plus three times four')['operation'] == 'mixed'
        assert words_to_calc('five squared plus two')['operation'] == 'mixed'
        assert words_to_calc('square root sixty four plus one')['operation'] == 'mixed'


# ── skill-side rounding helper ────────────────────────────────────────────────
# format_calc_result() is the utility skills call to apply rounding.
# It reads global + per-operation overrides, leaves ints untouched.

class TestFormatCalcResult:
    """Tests for the skill-side rounding helper format_calc_result().

    Signature:
        format_calc_result(result_dict, decimal_places=2, overrides=None) -> int | float

    Args:
        result_dict:   the dict returned by words_to_calc
        decimal_places: global default decimal places for float results
        overrides:     dict of operation → decimal_places, e.g. {'root': 4}

    Returns int if result is whole, otherwise float rounded to appropriate places.
    """
    def setup_method(self):
        from mycroft.client.realtime.number_parser import format_calc_result
        self.fmt = format_calc_result

    def test_int_result_untouched(self):
        r = words_to_calc('five plus three')
        assert self.fmt(r) == 8
        assert isinstance(self.fmt(r), int)

    def test_float_rounded_to_global_default(self):
        r = words_to_calc('ten divided three')
        assert self.fmt(r, decimal_places=2) == 3.33
        assert self.fmt(r, decimal_places=4) == 3.3333

    def test_root_uses_override(self):
        r = words_to_calc('square root two')
        # global=2 but root override=4
        result = self.fmt(r, decimal_places=2, overrides={'root': 4})
        assert result == round(math.sqrt(2), 4)

    def test_root_falls_back_to_global_without_override(self):
        r = words_to_calc('square root two')
        assert self.fmt(r, decimal_places=2) == round(math.sqrt(2), 2)

    def test_override_for_other_op_doesnt_affect_root(self):
        r = words_to_calc('square root two')
        result = self.fmt(r, decimal_places=2, overrides={'divide': 4})
        assert result == round(math.sqrt(2), 2)

    def test_division_uses_override(self):
        r = words_to_calc('ten divided three')
        result = self.fmt(r, decimal_places=2, overrides={'divide': 4})
        assert result == 3.3333

    def test_mixed_expression_uses_global(self):
        r = words_to_calc('square root sixty four plus one')
        # sqrt(64)+1 = 9.0, which is whole → int
        assert self.fmt(r, decimal_places=2, overrides={'root': 4}) == 9
        assert isinstance(self.fmt(r, decimal_places=2, overrides={'root': 4}), int)

    def test_zero_decimal_places(self):
        r = words_to_calc('ten divided three')
        assert self.fmt(r, decimal_places=0) == 3

    def test_whole_float_becomes_int(self):
        # 10/2 = 5.0 — already returned as int by words_to_calc, stays int
        r = words_to_calc('ten divided two')
        assert self.fmt(r) == 5
        assert isinstance(self.fmt(r), int)


class TestWordSets:
    def test_number_words_contains_ones(self):
        for w in ('one', 'two', 'three', 'nine', 'eleven', 'nineteen'):
            assert w in NUMBER_WORDS

    def test_number_words_contains_tens(self):
        for w in ('twenty', 'thirty', 'ninety'):
            assert w in NUMBER_WORDS

    def test_number_words_contains_magnitudes(self):
        for w in ('hundred', 'thousand', 'million', 'billion'):
            assert w in NUMBER_WORDS

    def test_sign_words(self):
        assert 'negative' in SIGN_WORDS
        assert 'minus' in SIGN_WORDS

    def test_decimal_words(self):
        assert 'point' in DECIMAL_WORDS
        assert 'dot' in DECIMAL_WORDS

    def test_calc_words_includes_operators(self):
        for w in ('plus', 'minus', 'times', 'divided', 'mod', 'modulo'):
            assert w in CALC_WORDS

    def test_calc_words_includes_power(self):
        # 'power' and 'raised' both mean exponent when following a number
        for w in ('squared', 'cubed', 'square', 'cube', 'root', 'power', 'raised'):
            assert w in CALC_WORDS

    def test_power_filler_words_not_in_calc(self):
        # 'to', 'the', 'of' are fillers — budget system ignores them
        for w in ('to', 'the', 'of'):
            assert w not in CALC_WORDS, f"'{w}' should be a filler, not in CALC_WORDS"

    def test_calc_words_includes_absolute(self):
        assert 'absolute' in CALC_WORDS
        assert 'value' in CALC_WORDS

    def test_calc_words_includes_decimal(self):
        assert 'point' in CALC_WORDS
        assert 'dot' in CALC_WORDS

    def test_filler_words_not_in_calc(self):
        # These appear inside phrases but must stay as fillers so they don't
        # corrupt the slot buffer when heard in other contexts.
        for w in ('of', 'the', 'by'):
            assert w not in CALC_WORDS, f"'{w}' should be a filler, not in CALC_WORDS"

    def test_to_not_in_calc(self):
        # 'to' is a filler — 'power' alone handles exponent, no phrase matching needed
        assert 'to' not in CALC_WORDS

    def test_filler_words_not_in_number(self):
        for w in ('of', 'the', 'by', 'and'):
            assert w not in NUMBER_WORDS

    def test_calc_words_includes_trig(self):
        for w in ('sin', 'sine', 'sign', 'cosine', 'cosign', 'tangent'):
            assert w in CALC_WORDS, f"'{w}' should be in CALC_WORDS"

    def test_of_not_in_calc_trig(self):
        # 'of' is a filler even for trig — "sine of thirty" works via budget system
        assert 'of' not in CALC_WORDS


# ── trig functions ────────────────────────────────────────────────────────────

class TestCalcTrig:
    def test_sine_exact(self):
        # sin(90) = 1.0
        assert calc('sine ninety') == approx(1.0)

    def test_sin_abbreviated(self):
        # Riva may produce abbreviated 'sin'
        assert calc('sin ninety') == approx(1.0)

    def test_sine_homophone(self):
        # Riva may transcribe 'sine' as 'sign'
        assert calc('sign ninety') == approx(1.0)

    def test_cosine_exact(self):
        # cos(0) = 1.0
        assert calc('cosine zero') == approx(1.0)

    def test_cosine_homophone(self):
        assert calc('cosign zero') == approx(1.0)

    def test_tangent_exact(self):
        # tan(45) = 1.0
        assert calc('tangent forty five') == approx(1.0)

    def test_sine_general(self):
        assert calc('sine thirty') == approx(math.sin(math.radians(30)))

    def test_cosine_general(self):
        assert calc('cosine sixty') == approx(math.cos(math.radians(60)))

    def test_tangent_general(self):
        assert calc('tangent thirty') == approx(math.tan(math.radians(30)))

    def test_sine_of_filler(self):
        # 'of' is stripped by slot buffer — tokenizer sees 'sine thirty'
        assert calc('sine thirty') == approx(math.sin(math.radians(30)))
        assert calc_via_slot('sine of thirty') == approx(math.sin(math.radians(30)))

    def test_negative_angle(self):
        assert calc('sine negative ninety') == approx(-1.0)

    def test_trig_in_expression(self):
        # trig result participates in further arithmetic
        r = calc('sine ninety plus cosine zero')
        assert r == approx(2.0)

    def test_trig_times_number(self):
        r = calc('cosine sixty times two')
        assert r == approx(math.cos(math.radians(60)) * 2)

    def test_operation_metadata_trig(self):
        r = words_to_calc('sine thirty')
        assert r is not None
        assert r['operation'] == 'trig'

    def test_operation_metadata_mixed_trig(self):
        r = words_to_calc('sine ninety plus one')
        assert r is not None
        assert r['operation'] == 'mixed'

    def test_trig_of_squared_operand(self):
        # "sine three squared" = sin(3²) = sin(9°), not sin(3°)²
        assert calc('sine three squared') == approx(math.sin(math.radians(9)))

    def test_trig_of_cubed_operand(self):
        # "tangent two cubed" = tan(2³) = tan(8°)
        assert calc('tangent two cubed') == approx(math.tan(math.radians(8)))

    def test_trig_of_squared_via_slot(self):
        assert calc_via_slot('sine three squared') == approx(math.sin(math.radians(9)))

    def test_bare_trig_word_no_number_is_none(self):
        # No operand — incomplete
        assert calc('sine') is None

    def test_trig_decimal_angle(self):
        assert calc('sine forty five point five') == approx(math.sin(math.radians(45.5)))


# ── compound operations ───────────────────────────────────────────────────────

class TestCalcCompound:
    """Combinations of prefix unary, postfix unary, and infix operators."""

    # prefix + postfix on operand (applies postfix to arg before prefix fn)
    def test_root_of_squared(self):
        # sqrt(4²) = sqrt(16) = 4
        assert calc('square root four squared') == approx(4.0)

    def test_cube_root_of_cubed(self):
        # cbrt(2³) = cbrt(8) = 2
        assert calc('cube root two cubed') == approx(2.0)

    def test_abs_of_cubed(self):
        # abs((-3)³) = abs(-27) = 27
        assert calc('absolute value negative three cubed') == approx(27)

    def test_abs_of_squared(self):
        # abs((-4)²) = abs(16) = 16
        assert calc('absolute value negative four squared') == approx(16)

    # prefix result on left of infix
    def test_root_plus_number(self):
        assert calc('square root nine plus one') == approx(4.0)

    def test_abs_plus_number(self):
        assert calc('absolute value negative five plus three') == approx(8)

    def test_cosine_times_number(self):
        assert calc('cosine zero times two') == approx(2.0)

    def test_tangent_plus_number(self):
        assert calc('tangent forty five plus one') == approx(2.0)

    # prefix result on right of infix
    def test_number_plus_root(self):
        assert calc('two plus square root nine') == approx(5.0)

    def test_number_times_root(self):
        assert calc('three times square root four') == approx(6.0)

    def test_number_plus_trig(self):
        assert calc('one plus sine ninety') == approx(2.0)

    def test_number_minus_cosine(self):
        assert calc('ten minus cosine zero') == approx(9.0)

    # prefix on both sides of infix
    def test_root_plus_root(self):
        assert calc('square root nine plus square root sixteen') == approx(7.0)

    def test_trig_plus_trig(self):
        assert calc('sine ninety plus cosine zero') == approx(2.0)

    def test_root_times_trig(self):
        assert calc('square root four times sine ninety') == approx(2.0)

    # postfix chaining
    def test_squared_then_cubed(self):
        # 3² = 9, 9³ = 729
        assert calc('three squared cubed') == approx(729)

    def test_cubed_then_squared(self):
        # 2³ = 8, 8² = 64
        assert calc('two cubed squared') == approx(64)

    # chained postfix on prefix operand
    def test_trig_of_squared_cubed(self):
        # sine two squared cubed = sin((2²)³) = sin(64°)
        assert calc('sine two squared cubed') == approx(math.sin(math.radians(64)))

    def test_root_of_squared_cubed(self):
        # sqrt((2²)³) = sqrt(64) = 8
        assert calc('square root two squared cubed') == approx(math.sqrt(64))

    def test_cosine_of_cubed_squared(self):
        # cos((2³)²) = cos(64°)
        assert calc('cosine two cubed squared') == approx(math.cos(math.radians(64)))

    # postfix operand in infix
    def test_squared_plus_number(self):
        assert calc('three squared plus one') == approx(10)

    def test_number_plus_squared(self):
        assert calc('two plus three squared') == approx(11)

    def test_squared_times_cubed(self):
        # 2² * 3³ = 4 * 27 = 108
        assert calc('two squared times three cubed') == approx(108)

    # infix power with prefix/postfix on operands
    def test_power_result_in_infix(self):
        assert calc('two power three plus one') == approx(9)

    def test_number_plus_power(self):
        assert calc('one plus two power three') == approx(9)

    def test_power_then_multiply(self):
        assert calc('two power three times two') == approx(16)

    # prefix result raised to a power
    def test_root_result_to_power(self):
        # sqrt(4)³ = 2³ = 8
        assert calc('square root four power three') == approx(8)

    # postfix result raised to a power
    def test_postfix_result_to_power(self):
        # (2²)³ = 4³ = 64
        assert calc('two squared power three') == approx(64)


# ── factorial ────────────────────────────────────────────────────────────────

class TestCalcFactorial:
    def test_five_factorial(self):
        assert calc('five factorial') == 120

    def test_zero_factorial(self):
        assert calc('zero factorial') == 1

    def test_ten_factorial(self):
        assert calc('ten factorial') == 3628800

    def test_factorial_in_expression(self):
        assert calc('three factorial plus one') == approx(7)

    def test_factorial_then_squared(self):
        # (2!)² = 4
        assert calc('two factorial squared') == approx(4)

    def test_negative_factorial_returns_none(self):
        assert calc('negative five factorial') is None

    def test_non_integer_factorial_returns_none(self):
        assert calc('five point five factorial') is None

    def test_factorial_operation_tag(self):
        r = words_to_calc('five factorial')
        assert r is not None
        assert r['operation'] == 'factorial'


# ── mathematical constants ────────────────────────────────────────────────────

class TestCalcConstants:
    def test_pi(self):
        assert calc('pi') == approx(math.pi)

    def test_tau(self):
        assert calc('tau') == approx(math.tau)

    def test_tao_alias(self):
        assert calc('tao') == approx(math.tau)

    def test_taw_alias(self):
        assert calc('taw') == approx(math.tau)

    def test_euler(self):
        assert calc('euler') == approx(math.e)

    def test_e_alias(self):
        # 'e' aliases to 'euler'
        assert calc('e') == approx(math.e)

    def test_oiler_alias(self):
        # Riva mishear of 'euler'
        assert calc('oiler') == approx(math.e)

    def test_e_in_expression(self):
        assert calc('e squared') == approx(math.e ** 2)

    def test_e_and_euler_equivalent(self):
        assert calc('e') == calc('euler')

    def test_golden_ratio(self):
        assert calc('golden ratio') == approx((1 + math.sqrt(5)) / 2)

    def test_pi_in_expression(self):
        assert calc('pi times two') == approx(math.pi * 2)

    def test_tau_divided_two(self):
        # tau / 2 = pi
        assert calc('tau divided two') == approx(math.pi)

    def test_euler_squared(self):
        assert calc('euler squared') == approx(math.e ** 2)

    def test_pi_plus_euler(self):
        assert calc('pi plus euler') == approx(math.pi + math.e)

    def test_euler_power_pi(self):
        assert calc('euler power pi') == approx(math.e ** math.pi)

    def test_negative_pi(self):
        assert calc('negative pi') == approx(-math.pi)

    def test_pi_squared(self):
        assert calc('pi squared') == approx(math.pi ** 2)

    def test_golden_ratio_in_expression(self):
        assert calc('golden ratio plus one') == approx((1 + math.sqrt(5)) / 2 + 1)

    def test_sine_pi(self):
        # sin(180°) = 0 (approximately)
        assert calc('sine pi') == approx(math.sin(math.radians(math.pi)), rel=1e-6)

    def test_cosine_pi(self):
        # cos(180°) = -1
        assert calc('cosine pi') == approx(math.cos(math.radians(math.pi)))

    def test_square_root_golden_ratio(self):
        assert calc('square root golden ratio') == approx(math.sqrt((1 + math.sqrt(5)) / 2))

    def test_inverse_sine_plus_pi(self):
        # asin(1) + pi = 90 + π
        assert calc('inverse sine one plus pi') == approx(math.degrees(math.asin(1)) + math.pi)

    def test_via_slot_euler(self):
        # Riva produces "euler" without apostrophe; "number" is a filler
        assert calc_via_slot('euler number') == approx(math.e)

    def test_apery(self):
        assert calc('apery') == approx(1.2020569031595942)

    def test_apery_aliases(self):
        assert calc('apory') == approx(1.2020569031595942)
        assert calc('apri') == approx(1.2020569031595942)

    def test_catalan(self):
        assert calc('catalan') == approx(0.9159655941772190)

    def test_catalan_alias(self):
        assert calc('catalon') == approx(0.9159655941772190)

    def test_mascheroni(self):
        assert calc('mascheroni') == approx(0.5772156649015329)

    def test_mascheroni_aliases(self):
        assert calc('mascueroni') == approx(0.5772156649015329)
        assert calc('mascarone') == approx(0.5772156649015329)

    def test_apery_in_expression(self):
        assert calc('apery plus one') == approx(1.2020569031595942 + 1)

    def test_mascheroni_plus_catalan(self):
        assert calc('mascheroni plus catalan') == approx(0.5772156649015329 + 0.9159655941772190)

    def test_constants_are_operations(self):
        r = words_to_calc('pi times two')
        assert r is not None
        assert r['operation'] in ('multiply', 'mixed')


# ── logarithms ───────────────────────────────────────────────────────────────

class TestCalcLog:
    def test_log_base10(self):
        assert calc('log one thousand') == approx(3.0)

    def test_log_base10_hundred(self):
        assert calc('log one hundred') == approx(2.0)

    def test_log_with_filler_of(self):
        assert calc_via_slot('log of one thousand') == approx(3.0)

    def test_natural_log_e(self):
        assert calc('natural log euler') == approx(1.0)

    def test_natural_log_one(self):
        assert calc('natural log one') == approx(0.0)

    def test_ln_alias(self):
        assert calc('ln euler') == approx(1.0)

    def test_ln_equals_natural_log(self):
        assert calc('ln one thousand') == approx(calc('natural log one thousand'))

    def test_log_base_two(self):
        assert calc('log base two eight') == approx(3.0)

    def test_log_base_ten_explicit(self):
        assert calc('log base ten one thousand') == approx(3.0, rel=1e-9)

    def test_log_base_multiword(self):
        # Both base and argument are multi-word numbers
        assert calc('log base sixty four sixty nine') == approx(math.log(69, 64))

    def test_log_constant_argument(self):
        assert calc('log pi') == approx(math.log10(math.pi))

    def test_natural_log_constant(self):
        assert calc('natural log pi') == approx(math.log(math.pi))

    def test_log_in_expression(self):
        # log(1000) + 1 = 4
        assert calc('log one thousand plus one') == approx(4.0)

    def test_log_zero_returns_none(self):
        assert calc('log zero') is None

    def test_log_base_one_returns_none(self):
        assert calc('log base one five') is None

    def test_log_operation_tag(self):
        r = words_to_calc('log one thousand')
        assert r is not None
        assert r['operation'] == 'log'

    def test_natural_log_operation_tag(self):
        r = words_to_calc('natural log euler')
        assert r is not None
        assert r['operation'] == 'log'


# ── ordinal_to_int ────────────────────────────────────────────────────────────

class TestOrdinalToInt:
    """Tests for ordinal_to_int() — spoken ordinal → integer value."""

    def test_irregulars(self):
        from mycroft.client.realtime.number_parser import ordinal_to_int
        assert ordinal_to_int('first') == 1
        assert ordinal_to_int('second') == 2
        assert ordinal_to_int('third') == 3

    def test_suffix_map(self):
        from mycroft.client.realtime.number_parser import ordinal_to_int
        assert ordinal_to_int('fifth') == 5
        assert ordinal_to_int('eighth') == 8
        assert ordinal_to_int('ninth') == 9
        assert ordinal_to_int('twelfth') == 12

    def test_regular_th(self):
        from mycroft.client.realtime.number_parser import ordinal_to_int
        assert ordinal_to_int('fourth') == 4
        assert ordinal_to_int('sixth') == 6
        assert ordinal_to_int('seventh') == 7
        assert ordinal_to_int('tenth') == 10
        assert ordinal_to_int('eleventh') == 11
        assert ordinal_to_int('thirteenth') == 13
        assert ordinal_to_int('hundredth') == 100
        assert ordinal_to_int('thousandth') == 1000

    def test_ieth_tens(self):
        from mycroft.client.realtime.number_parser import ordinal_to_int
        assert ordinal_to_int('twentieth') == 20
        assert ordinal_to_int('thirtieth') == 30
        assert ordinal_to_int('fortieth') == 40
        assert ordinal_to_int('fiftieth') == 50
        assert ordinal_to_int('sixtieth') == 60
        assert ordinal_to_int('seventieth') == 70
        assert ordinal_to_int('eightieth') == 80
        assert ordinal_to_int('ninetieth') == 90

    def test_multiword_composite(self):
        from mycroft.client.realtime.number_parser import ordinal_to_int
        assert ordinal_to_int('twenty first') == 21
        assert ordinal_to_int('sixty fourth') == 64
        assert ordinal_to_int('one hundred twenty eighth') == 128
        assert ordinal_to_int('thirty second') == 32

    def test_invalid_forms(self):
        from mycroft.client.realtime.number_parser import ordinal_to_int
        assert ordinal_to_int('fiveth') is None   # canonical is 'fifth'
        assert ordinal_to_int('zeroth') is None   # zero not a valid ordinal
        assert ordinal_to_int('twond') is None
        assert ordinal_to_int('power') is None
        assert ordinal_to_int('') is None


# ── ordinal exponent power ────────────────────────────────────────────────────

class TestCalcOrdinalPower:
    """'<number> <ordinal> power' — ordinal used as exponent."""

    def test_fifth_power(self):
        # "three to the fifth power" → slot: "three fifth power"
        assert calc('three fifth power') == approx(3**5)

    def test_second_power(self):
        assert calc('two second power') == approx(4)

    def test_third_power(self):
        assert calc('two third power') == approx(8)

    def test_tenth_power(self):
        assert calc('two tenth power') == approx(1024)

    def test_twenty_first_power(self):
        assert calc('two twenty first power') == approx(2**21)

    def test_sixty_fourth_power(self):
        assert calc('two sixty fourth power') == approx(2**64)

    def test_one_hundred_twenty_eighth_power(self):
        assert calc('two one hundred twenty eighth power') == approx(2**128)

    def test_ordinal_power_operation_tag(self):
        r = words_to_calc('two fifth power')
        assert r is not None
        assert r['operation'] == 'power'

    def test_ordinal_via_slot(self):
        # 'to the' are fillers stripped by slot buffer
        assert calc_via_slot('two to the fifth power') == approx(2**5)

    def test_ordinal_power_in_expression(self):
        # 2^5 + 3 = 35
        assert calc('two fifth power plus three') == approx(35)

    def test_regular_cardinal_power_still_works(self):
        # Existing numeric exponent form must not regress
        assert calc('two power eight') == approx(256)
        assert calc('three power three') == approx(27)


# ── nth root ──────────────────────────────────────────────────────────────────

class TestCalcNthRoot:
    """'<ordinal> root <number>' — nth root."""

    def test_fourth_root(self):
        assert calc('fourth root sixteen') == approx(2.0)

    def test_third_root(self):
        # cube root of 27 = 3
        assert calc('third root twenty seven') == approx(3.0)

    def test_second_root(self):
        # second root = square root
        assert calc('second root nine') == approx(3.0)

    def test_tenth_root(self):
        assert calc('tenth root one thousand') == approx(1000**(1/10))

    def test_twelfth_root(self):
        assert calc('twelfth root four thousand ninety six') == approx(4096**(1/12))

    def test_sixty_fourth_root(self):
        # Multi-word ordinal before root — "sixty fourth" must parse as 64, not 60
        assert calc('sixty fourth root two') == approx(2**(1/64))

    def test_one_hundred_twenty_eighth_root(self):
        assert calc('one hundred twenty eighth root two') == approx(2**(1/128))

    def test_nth_root_operation_tag(self):
        r = words_to_calc('fourth root sixteen')
        assert r is not None
        assert r['operation'] == 'root'

    def test_nth_root_via_slot(self):
        # 'of' is a filler
        assert calc_via_slot('fourth root of sixteen') == approx(2.0)

    def test_nth_root_in_expression(self):
        # fourth root of 16 + 3 = 2 + 3 = 5
        assert calc('fourth root sixteen plus three') == approx(5.0)

    def test_square_and_cube_root_still_work(self):
        # Existing prefix root forms must not regress
        assert calc('square root nine') == approx(3.0)
        assert calc('cube root twenty seven') == approx(3.0)


# ── inverse trig ──────────────────────────────────────────────────────────────

class TestCalcInverseTrig:
    # basic inverse trig — result in degrees
    def test_inverse_sine(self):
        # asin(1) = 90°
        assert calc('inverse sine one') == approx(90.0)

    def test_inverse_cosine(self):
        # acos(1) = 0°
        assert calc('inverse cosine one') == approx(0.0)

    def test_inverse_tangent(self):
        # atan(1) = 45°
        assert calc('inverse tangent one') == approx(45.0)

    def test_inverse_sine_half(self):
        # asin(0.5) = 30°
        assert calc('inverse sine zero point five') == approx(30.0)

    def test_inverse_cosine_half(self):
        # acos(0.5) = 60°
        assert calc('inverse cosine zero point five') == approx(60.0)

    def test_inverse_tangent_zero(self):
        # atan(0) = 0°
        assert calc('inverse tangent zero') == approx(0.0)

    # trig aliases work as modifier target — alias rewrite happens before inverse check
    def test_inverse_sign_alias(self):
        # "sign" → "sine" via alias → asin(1) = 90°
        assert calc('inverse sign one') == approx(90.0)

    def test_inverse_cosign_alias(self):
        # "cosign" → "cosine" via alias → acos(0) = 90°
        assert calc('inverse cosign zero') == approx(90.0)

    def test_inverse_sin_alias(self):
        # "sin" → "sine" via alias → asin(1) = 90°
        assert calc('inverse sin one') == approx(90.0)

    # arc aliases for "inverse"
    def test_arc_sine(self):
        # "arc" → "inverse" via alias → asin(1) = 90°
        assert calc('arc sine one') == approx(90.0)

    def test_arc_cosine(self):
        assert calc('arc cosine one') == approx(0.0)

    def test_arc_tangent(self):
        assert calc('arc tangent one') == approx(45.0)

    def test_arxie_sine(self):
        # Riva mishear of "arc" → still works
        assert calc('arxie sine one') == approx(90.0)

    def test_ark_sign(self):
        # Combined mishear: "ark" for "arc", "sign" for "sine"
        assert calc('ark sign one') == approx(90.0)

    # domain error — asin/acos require input in [-1, 1]
    def test_inverse_sine_out_of_domain(self):
        assert calc('inverse sine two') is None

    def test_inverse_cosine_out_of_domain(self):
        assert calc('inverse cosine negative two') is None

    def test_inverse_tangent_large_value(self):
        # atan has no domain restriction — large values are fine
        assert calc('inverse tangent one thousand') is not None

    # negative operand
    def test_inverse_sine_negative(self):
        # asin(-1) = -90°
        assert calc('inverse sine negative one') == approx(-90.0)

    def test_inverse_tangent_negative(self):
        # atan(-1) = -45°
        assert calc('inverse tangent negative one') == approx(-45.0)

    # inverse trig in an expression
    def test_inverse_sine_plus_number(self):
        # asin(1) + 45 = 90 + 45 = 135
        assert calc('inverse sine one plus forty five') == approx(135.0)

    def test_number_plus_inverse_cosine(self):
        # 30 + acos(0.5) = 30 + 60 = 90
        assert calc('thirty plus inverse cosine zero point five') == approx(90.0)

    def test_via_slot_arc_sine(self):
        # "of" is a filler — stripped by slot buffer
        assert calc_via_slot('arc sine of one') == approx(90.0)

    def test_operation_tag(self):
        r = words_to_calc('inverse sine one')
        assert r is not None
        assert r['operation'] == 'inverse_trig'

    def test_mixed_trig_and_inverse_trig(self):
        # sine(90) + asin(1) = 1 + 90 = 91
        assert calc('sine ninety plus inverse sine one') == approx(91.0)


# ── calc with concatenated number operands ────────────────────────────────────

class TestCalcConcat:
    """Arithmetic expressions where operands use positional concat notation."""

    # words_to_int directly
    def test_words_to_int_year_style(self):
        assert words_to_int('nineteen forty one') == 1941
        assert words_to_int('eighteen sixty five') == 1865

    def test_words_to_int_three_group(self):
        assert words_to_int('thirteen six fifty') == 13650

    # calc expressions with concat operands
    def test_year_minus_year(self):
        # 1941 - 1865 = 76
        assert calc('nineteen forty one minus eighteen sixty five') == 76

    def test_year_plus_one(self):
        assert calc('twenty nineteen plus one') == 2020

    def test_concat_times_two(self):
        assert calc('nine eleven times two') == 1822

    def test_three_group_plus(self):
        assert calc('thirteen six fifty plus three fifty') == 14000
