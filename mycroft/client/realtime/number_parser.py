"""
Spoken number → integer/expression conversion for realtime intent matching.

Handles English number words as transcribed by Riva (all spelled out):
  "forty two"                → 42
  "one hundred thirty eight" → 138
  "two thousand"             → 2000
  "one million four hundred thousand" → 1_400_000

Entry points:
  words_to_int(phrase: str) -> int | None
      Convert a spoken number phrase to an integer. Returns None on failure.

  words_to_calc(phrase: str) -> dict | None
      Parse and evaluate a spoken arithmetic expression. Returns dict with
      'result' (float), 'expr' (spoken words), 'tokens' (mixed int/str list).
      Returns None on parse failure.

  NUMBER_WORDS: frozenset[str]
      Valid number component words for {number} entity next-word matching.

  CALC_WORDS: frozenset[str]
      All words valid inside a {number_calc} expression (NUMBER_WORDS +
      operator words + sign words).
"""

ONES = {
    'zero': 0,
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19,
}

TENS = {
    'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50,
    'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
}

MAGNITUDES = [
    ('trillion', 1_000_000_000_000),
    ('billion',  1_000_000_000),
    ('million',  1_000_000),
    ('thousand', 1_000),
    ('hundred',  100),
]

MAGNITUDE_WORDS = frozenset(name for name, _ in MAGNITUDES)

# Flat set registered as valid next-words wherever {number} appears.
# The matcher just checks membership; words_to_int rejects bad sequences.
NUMBER_WORDS = frozenset(ONES) | frozenset(TENS) | MAGNITUDE_WORDS

# Signed variant adds these prefixes
SIGN_WORDS = frozenset({'negative', 'minus'})
SIGNED_NUMBER_WORDS = SIGN_WORDS | NUMBER_WORDS

# Decimal point words — only valid in calc context (words_to_int stays integer-only)
DECIMAL_WORDS = frozenset({'point', 'dot'})


def _parse_below_thousand(tokens: list[str]) -> tuple[int, int]:
    """Parse a value < 1000 from the front of tokens.

    Returns (value, tokens_consumed). Consumed may be 0 if nothing matched.
    Handles: ones/teens, tens, tens+ones, [N] hundred [tens/ones].
    Bare 'hundred' with no preceding coefficient implies 1 (e.g. "hundred thirteen" → 113).
    """
    value = 0
    i = 0
    n = len(tokens)

    # Optional hundreds block: "[<ones>] hundred"
    # Coefficient is explicit (e.g. "three hundred") or implied 1 (e.g. "hundred")
    if i < n and tokens[i] == 'hundred':
        # bare "hundred" — implied coefficient 1
        value += 100
        i += 1
        if i < n and tokens[i] == 'and':
            i += 1
    elif (i < n and tokens[i] in ONES and ONES[tokens[i]] > 0
            and i + 1 < n and tokens[i + 1] == 'hundred'):
        value += ONES[tokens[i]] * 100
        i += 2
        if i < n and tokens[i] == 'and':
            i += 1

    # Tens + optional ones, or teens, or bare ones
    if i < n and tokens[i] in TENS:
        value += TENS[tokens[i]]
        i += 1
        if i < n and tokens[i] in ONES and ONES[tokens[i]] > 0:
            value += ONES[tokens[i]]
            i += 1
    elif i < n and tokens[i] in ONES:
        value += ONES[tokens[i]]
        i += 1

    return value, i


def words_to_int(phrase: str) -> int | None:
    """Convert a spoken number phrase to an integer.

    Returns None if the phrase is not a valid number expression.

    Accepts optional leading 'negative'/'minus' for {signed_number} use.
    """
    if not phrase:
        return None

    tokens = phrase.lower().split()
    if not tokens:
        return None

    negative = False
    if tokens[0] in SIGN_WORDS:
        negative = True
        tokens = tokens[1:]
    if not tokens:
        return None

    if tokens == ['zero']:
        return 0

    # "a hundred/thousand/million/..." → "one hundred/thousand/million/..."
    tokens = list(tokens)
    for idx in range(len(tokens) - 1):
        if tokens[idx] == 'a' and tokens[idx + 1] in MAGNITUDE_WORDS:
            tokens[idx] = 'one'

    result = 0
    i = 0
    n = len(tokens)

    # Process magnitude tiers largest → smallest.
    # For each tier: parse a sub-thousand coefficient, then consume the
    # magnitude word if present. A bare magnitude with no preceding chunk
    # implies coefficient 1 (e.g. "thousand five hundred" → 1500).
    for mag_name, mag_value in MAGNITUDES:
        if mag_name == 'hundred':
            continue  # handled inside _parse_below_thousand
        if i >= n:
            break

        chunk, consumed = _parse_below_thousand(tokens[i:])
        # Bare magnitude with no preceding number — implied coefficient 1
        if consumed == 0 and i < n and tokens[i] == mag_name:
            result += mag_value
            i += 1
            continue
        if consumed == 0:
            continue
        if i + consumed < n and tokens[i + consumed] == mag_name:
            result += chunk * mag_value
            i += consumed + 1

    # Sub-thousand tail
    if i < n:
        tail, consumed = _parse_below_thousand(tokens[i:])
        if consumed == 0:
            return None  # unrecognised token
        result += tail
        i += consumed

    if i < n:
        return None  # leftover tokens

    if result == 0 and tokens != ['zero']:
        return None

    return -result if negative else result


# ── calculator vocabulary ────────────────────────────────────────────────────

# Operator words → canonical symbol
OPERATOR_WORDS = {
    'plus': '+', 'add': '+', 'added': '+',
    'minus': '-', 'subtract': '-', 'subtracted': '-',
    'times': '*', 'multiplied': '*', 'multiply': '*',
    'divided': '/', 'divide': '/', 'over': '/',
    'mod': '%', 'modulo': '%', 'remainder': '%',
}

# Postfix unary: word immediately after a number → replace number with result
_POSTFIX_UNARY = {
    'squared': lambda x: x ** 2,
    'cubed': lambda x: x ** 3,
}

# Prefix unary phrases: tuple of words that precede the operand
# Each maps to (words_tuple, function)
_PREFIX_UNARY_PHRASES = [
    (('square', 'root', 'of'), lambda x: x ** 0.5),
    (('cube', 'root', 'of'), lambda x: x ** (1/3) if x >= 0 else -((-x) ** (1/3))),
    (('square', 'root'), lambda x: x ** 0.5),
    (('cube', 'root'), lambda x: x ** (1/3) if x >= 0 else -((-x) ** (1/3))),
    (('absolute', 'value'), abs),
]

# Infix power phrases: sequence of words between left operand and right operand
# No phrase list needed — 'power' alone after a number means exponent.
# All surrounding words (to, the, of, raised) are fillers handled by the budget system.

# Words valid inside a calc slot buffer — only those that _tokenize_calc can
# consume standalone or as part of a phrase. 'the' and 'of' are only meaningful
# inside a fully-matched phrase; loose they cause parse failures, so leave them
# out — the matcher treats them as fillers (budget hit on FINAL, path survives).
_POWER_WORDS = frozenset({
    'squared', 'cubed',       # postfix unary — meaningful alone after a number
    'square', 'cube', 'root', # prefix unary — meaningful in combination
    'power', 'raised',        # infix — either alone between numbers = exponent
    # 'to', 'the', 'of' — fillers; budget system handles them
})

_PREFIX_UNARY_WORDS = frozenset({
    'absolute', 'value',
})

# Words that are part of operator phrases but not operators themselves
OPERATOR_HELPER_WORDS = frozenset({'by', 'and'})

# All words valid anywhere inside a {number_calc} expression
CALC_WORDS = (NUMBER_WORDS | SIGN_WORDS | frozenset(OPERATOR_WORDS)
              | _POWER_WORDS | _PREFIX_UNARY_WORDS | DECIMAL_WORDS)


# ── tokenizer ────────────────────────────────────────────────────────────────

def _tokenize_calc(tokens: list[str]) -> tuple[list, str] | None:
    """Convert a spoken token list into a mixed list of numbers and operator symbols.

    "forty two plus negative eighteen times three" →
        ([42, '+', -18, '*', 3], 'mixed')

    Sign vs operator disambiguation:
      'minus'/'negative' is a SIGN if it appears at the start or immediately
      after an operator symbol. Otherwise it's the subtraction operator '-'.

    Power/root forms supported:
      postfix:  "five squared" → 25, "three cubed" → 27
      infix:    "two to the power of eight" → 256
      prefix:   "square root of nine" → 3.0, "cube root of twenty seven" → 3.0

    Returns (mixed_tokens, operation) or None on failure.
    operation: 'add' | 'subtract' | 'multiply' | 'divide' | 'modulo' |
               'power' | 'root' | 'abs' | 'mixed'
    """
    result = []
    i = 0
    n = len(tokens)
    _ops_seen = set()  # track which operation types appear

    def last_token_is_number():
        return result and isinstance(result[-1], (int, float))

    def remaining(offset=0):
        return tokens[i + offset:]

    def parse_number_at(pos) -> tuple[float | int | None, int]:
        """Parse an integer (+ optional decimal suffix) starting at pos.
        Returns (value, tokens_consumed) or (None, 0) on failure."""
        num_val = None
        consumed = 0
        for length in range(min(n - pos, 20), 0, -1):
            sub = ' '.join(tokens[pos:pos + length])
            val = words_to_int(sub)
            if val is not None:
                num_val = val
                consumed = length
                break
        if num_val is None:
            return None, 0
        j = pos + consumed
        if j < n and tokens[j] in DECIMAL_WORDS:
            k = j + 1
            frac_digits = []
            while k < n and tokens[k] in ONES and ONES[tokens[k]] < 10:
                frac_digits.append(str(ONES[tokens[k]]))
                k += 1
            if frac_digits:
                num_val = num_val + float('0.' + ''.join(frac_digits))
                consumed = k - pos
            else:
                return None, 0  # trailing point with no digits — incomplete
        return num_val, consumed

    while i < n:
        tok = tokens[i]

        # ── prefix unary (square root / cube root / absolute value) ──────────
        for phrase, fn in _PREFIX_UNARY_PHRASES:
            plen = len(phrase)
            if tuple(tokens[i:i + plen]) == phrase:
                # parse the operand that follows
                j = i + plen
                if j >= n:
                    return None
                # consume sign if present
                sign = 1
                if tokens[j] in ('minus', 'negative'):
                    sign = -1
                    j += 1
                    if j >= n:
                        return None
                # parse number (integer or decimal)
                val, consumed = parse_number_at(j)
                if val is None:
                    return None
                operand = sign * val
                result.append(fn(operand))
                i = j + consumed
                # tag operation type by which phrase matched
                if phrase[0] in ('square', 'cube'):
                    _ops_seen.add('root')
                else:
                    _ops_seen.add('abs')
                break
        else:
            # ── sign/operator disambiguation for 'minus'/'negative' ──────────
            if tok in ('minus', 'negative'):
                if last_token_is_number():
                    result.append('-')
                    _ops_seen.add('subtract')
                    i += 1
                    continue
                else:
                    pass  # fall through to number parsing

            # ── postfix unary (squared / cubed) — must follow a number ────────
            elif tok in _POSTFIX_UNARY and last_token_is_number():
                result[-1] = _POSTFIX_UNARY[tok](result[-1])
                _ops_seen.add('power')
                i += 1
                continue

            # ── infix power ───────────────────────────────────────────────────
            # 'power' or 'raised' after a number means exponent.
            # Surrounding words (to, the, of) are fillers the budget system handles.
            # If both appear (e.g. 'raised power'), 'raised' defers to 'power'.
            elif tok in ('power', 'raised') and last_token_is_number():
                if tok == 'raised' and i + 1 < n and tokens[i + 1] == 'power':
                    # 'raised power N' — skip 'raised', let 'power' handle it
                    i += 1
                    continue
                result.append('**')
                _ops_seen.add('power')
                i += 1
                continue

            # ── operator words ────────────────────────────────────────────────
            if tok in OPERATOR_WORDS and tok not in ('minus', 'negative'):
                sym = OPERATOR_WORDS[tok]
                if sym is None:
                    i += 1
                    continue
                if not last_token_is_number():
                    return None  # operator without a left operand
                result.append(sym)
                _ops_seen.add({'+': 'add', '-': 'subtract', '*': 'multiply',
                               '/': 'divide', '%': 'modulo'}[sym])
                i += 1
                # Consume optional 'by' after 'multiplied'/'divided'
                if i < n and tokens[i] == 'by':
                    i += 1
                continue

            # ── skip filler 'and' between number parts ────────────────────────
            if tok == 'and' and last_token_is_number():
                i += 1
                continue

            # ── number (possibly signed) ──────────────────────────────────────
            sign = 1
            if tok in ('minus', 'negative'):
                sign = -1
                i += 1
                if i >= n:
                    return None

            num_val, consumed = parse_number_at(i)
            if num_val is None:
                return None  # unrecognised token

            result.append(sign * num_val)
            i += consumed

    if not result:
        return None

    # Validate: must start and end with a number
    if not isinstance(result[0], (int, float)):
        return None
    if not isinstance(result[-1], (int, float)):
        return None

    operation = _ops_seen.pop() if len(_ops_seen) == 1 else 'mixed'
    return result, operation


# ── evaluator ────────────────────────────────────────────────────────────────

def _evaluate(tokens: list) -> float | None:
    """Evaluate a tokenized expression with operator precedence.

    Precedence (high → low): ** > * / > + -
    tokens: alternating [number, op, number, op, number, ...]
    Returns float result, or None on error (e.g. division by zero).
    """
    if not tokens:
        return None

    # Copy so we can mutate
    toks = list(tokens)

    # Pass 0: ** (right-associative — process right to left)
    i = len(toks) - 2
    while i >= 1:
        if toks[i] == '**':
            left = toks[i - 1]
            if i + 1 >= len(toks):
                return None  # trailing operator
            right = toks[i + 1]
            toks[i - 1:i + 2] = [left ** right]
            i -= 2
        else:
            i -= 2

    # Pass 1: * / %
    i = 1
    while i < len(toks):
        if toks[i] in ('*', '/', '%'):
            left = toks[i - 1]
            op = toks[i]
            if i + 1 >= len(toks):
                return None  # trailing operator
            right = toks[i + 1]
            if op in ('/', '%') and right == 0:
                return None  # division/modulo by zero
            if op == '*':
                val = left * right
            elif op == '/':
                val = left / right
            else:
                val = left % right
            toks[i - 1:i + 2] = [val]
        else:
            i += 2

    # Pass 2: + and -
    acc = toks[0]
    i = 1
    while i < len(toks):
        op = toks[i]
        if i + 1 >= len(toks):
            return None  # trailing operator
        right = toks[i + 1]
        if op == '+':
            acc += right
        elif op == '-':
            acc -= right
        i += 2

    return float(acc)


# ── public entry point ───────────────────────────────────────────────────────

def words_to_calc(phrase: str) -> dict | None:
    """Parse and evaluate a spoken arithmetic expression.

    Args:
        phrase: Space-separated spoken words, e.g. "forty two plus eighteen"

    Returns:
        dict with keys:
          'result': float — evaluated answer
          'expr':   str   — original spoken phrase
          'tokens': list  — mixed [int/float, str_op, int/float, ...] form
        or None if phrase cannot be parsed as an arithmetic expression.

    Examples:
        "forty two plus eighteen"           → result=60.0
        "one hundred minus fifty three"     → result=47.0
        "two times three plus four"         → result=10.0  (precedence)
        "negative five plus three"          → result=-2.0
        "ten divided by two"                → result=5.0
        "negative three times minus two"    → result=6.0
    """
    if not phrase:
        return None
    tokens = phrase.lower().split()
    # Require at least one operation — bare numbers (including signed) are rejected.
    # Check after tokenizing: a valid expression with an operation produces either
    # multiple tokens (infix) or a result that required a unary op (postfix/prefix).
    # We defer to post-tokenize check below rather than pre-scanning words, because
    # 'minus' is ambiguous (sign vs subtraction operator).
    tokenized = _tokenize_calc(tokens)
    if tokenized is None:
        return None
    mixed, operation = tokenized
    # Require at least one operation. A bare (possibly signed) number tokenizes to
    # a single-element list [n]. Any real operation produces either multiple tokens
    # (infix: [n, op, n, ...]) or consumed extra spoken words to produce the result
    # (postfix squared/cubed, prefix sqrt/cbrt) — detected by input having non-number words.
    _op_words = frozenset(CALC_WORDS) - NUMBER_WORDS - SIGN_WORDS - DECIMAL_WORDS
    has_op = len(mixed) > 1 or any(t in _op_words for t in tokens)
    if not has_op:
        return None
    result = _evaluate(mixed)
    if result is None:
        return None
    if isinstance(result, float) and result == int(result):
        result = int(result)
    return {
        'result': result,
        'expr': phrase,
        'tokens': mixed,
        'operation': operation,
    }


def format_calc_result(
    result_dict: dict,
    decimal_places: int = 2,
    overrides: dict | None = None,
) -> int | float:
    """Round a words_to_calc result for display.

    Applies per-operation overrides first, then the global default.
    Integer results are always returned as-is (no rounding needed).

    Args:
        result_dict:    dict returned by words_to_calc
        decimal_places: global decimal places for float results (default 2)
        overrides:      per-operation overrides, e.g. {'root': 4, 'divide': 3}
                        valid keys: 'add', 'subtract', 'multiply', 'divide',
                                    'modulo', 'power', 'root', 'abs', 'mixed'
    """
    value = result_dict['result']
    if not isinstance(value, float):
        return value
    operation = result_dict.get('operation', 'mixed')
    places = (overrides or {}).get(operation, decimal_places)
    rounded = round(value, places)
    # Convert back to int if rounding produces a whole number
    if rounded == int(rounded):
        return int(rounded)
    return rounded
