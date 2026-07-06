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
    """Return the numeric result of words_to_calc, or None."""
    r = words_to_calc(phrase)
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
        # 'hundred hundred' is not a valid number
        assert words_to_int('hundred hundred') is None


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
        assert calc('three multiplied by seven') == 21

    def test_multiply_alias(self):
        assert calc('three multiply seven') == 21


class TestCalcDivision:
    def test_basic(self):
        assert calc('ten divided by two') == 5

    def test_divide_alias(self):
        assert calc('ten divide two') == 5

    def test_over_alias(self):
        assert calc('ten over two') == 5

    def test_fractional_result(self):
        assert calc('five divided by two') == approx(2.5)

    def test_division_by_zero(self):
        assert calc('five divided by zero') is None


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
        assert calc('eight plus six divided by two') == 11  # 8 + (6/2)

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
        # 'of' is a filler — slot sees 'square root sixty four'
        assert calc('square root of sixty four') == approx(8.0)
        assert calc('square root sixty four') == approx(8.0)

    def test_cube_root(self):
        assert calc('cube root of twenty seven') == approx(3.0)
        assert calc('cube root twenty seven') == approx(3.0)

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
        # 'of' is not in CALC_WORDS so it never enters the slot buffer.
        # words_to_calc is called with whatever the slot collected — 'of' won't be there.
        # The slot buffer version (no 'of') must work:
        assert calc('absolute value negative five') == 5
        # Direct call with 'of' returns None — that's correct, 'of' is unrecognised.
        # The matcher filters it before words_to_calc is ever called.
        assert calc('absolute value of negative five') is None


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
        # "five to the power" — right operand missing
        assert calc('five to the power') is None

    def test_trailing_raised_to(self):
        assert calc('five raised to') is None

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
        assert calc('ten divided by zero') is None

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
        r = words_to_calc('ten divided by two')
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
        r = words_to_calc('ten divided by three')
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
        r = words_to_calc('ten divided by three')
        result = self.fmt(r, decimal_places=2, overrides={'divide': 4})
        assert result == 3.3333

    def test_mixed_expression_uses_global(self):
        r = words_to_calc('square root sixty four plus one')
        # sqrt(64)+1 = 9.0, which is whole → int
        assert self.fmt(r, decimal_places=2, overrides={'root': 4}) == 9
        assert isinstance(self.fmt(r, decimal_places=2, overrides={'root': 4}), int)

    def test_zero_decimal_places(self):
        r = words_to_calc('ten divided by three')
        assert self.fmt(r, decimal_places=0) == 3

    def test_whole_float_becomes_int(self):
        # 10/2 = 5.0 — already returned as int by words_to_calc, stays int
        r = words_to_calc('ten divided by two')
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
