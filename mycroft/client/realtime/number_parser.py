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


# ── ordinal support ──────────────────────────────────────────────────────────

# Irregular ordinals that can't be derived by suffix stripping
_ORDINAL_IRREGULARS = {
    'first':  'one',
    'second': 'two',
    'third':  'three',
}

# Explicit suffix map for the irregular teens/special cases that don't
# follow a simple chop-the-suffix rule.
_ORDINAL_SUFFIX_MAP = {
    'twelfth':    'twelve',
    'eighth':     'eight',
    'ninth':      'nine',
    'fifth':      'five',
}

def ordinal_to_int(word: str) -> int | None:
    """Convert a spoken ordinal word to its integer value.

    Handles irregular forms (first/second/third) and the full regular pattern:
      'fourth' → 4, 'twelfth' → 12, 'twentieth' → 20,
      'twenty first' → 21, 'one hundred twenty eighth' → 128, etc.

    Multi-word ordinals (e.g. 'twenty first') must be passed as a single
    space-joined string. Returns None if not a valid ordinal.
    """
    word = word.strip().lower()
    if not word:
        return None

    tokens = word.split()
    last = tokens[-1]

    # Explicit irregular forms (take priority over suffix rules)
    if last in _ORDINAL_IRREGULARS:
        cardinal_last = _ORDINAL_IRREGULARS[last]
    elif last in _ORDINAL_SUFFIX_MAP:
        cardinal_last = _ORDINAL_SUFFIX_MAP[last]
    # -ieth suffix: twentieth → twenty, thirtieth → thirty, hundredth→hundred
    elif last.endswith('ieth'):
        cardinal_last = last[:-4] + 'y'
    # -th suffix on regular words: fourth→four, sixth→six, tenth→ten, etc.
    # Rejected if the stripped base corresponds to a number whose canonical
    # ordinal is irregular (five→fifth, eight→eighth, etc. — those are in
    # _ORDINAL_SUFFIX_MAP and are already handled above).
    elif last.endswith('th') and len(last) > 4:
        candidate = last[:-2]
        # The canonical ordinals for these cardinals use irregular forms,
        # not plain -th — reject any -th attempt for them.
        _irregular_cardinals = frozenset(
            v for v in {**_ORDINAL_IRREGULARS, **_ORDINAL_SUFFIX_MAP}.values()
        )
        if candidate in _irregular_cardinals or words_to_int(candidate) is None:
            return None
        cardinal_last = candidate
    # -st: only valid for 'first' (handled above via irregulars)
    # -nd: only valid for 'second' (handled above via irregulars)
    # -rd: only valid for 'third' (handled above via irregulars)
    else:
        return None

    cardinal = ' '.join(tokens[:-1] + [cardinal_last])
    val = words_to_int(cardinal)
    # Sanity: reject if cardinal didn't parse or value is <= 0
    if val is None or val <= 0:
        return None
    return val


# Dynamically build the set of single-token ordinal words valid in calc slots.
# Multi-word ordinals (e.g. 'twenty first') are handled by lookahead in the
# tokenizer; only single-token forms need to be in CALC_WORDS for slot filtering.
def _build_ordinal_tokens() -> frozenset:
    candidates = list(_ORDINAL_IRREGULARS) + list(_ORDINAL_SUFFIX_MAP)
    # -ieth forms from tens bases
    for tens_word in ('twenty', 'thirty', 'forty', 'fifty',
                      'sixty', 'seventy', 'eighty', 'ninety'):
        candidates.append(tens_word[:-1] + 'ieth')
    # -th forms from ones/teens/magnitudes
    for w in ('four', 'six', 'seven', 'ten', 'eleven',
              'thirteen', 'fourteen', 'fifteen', 'sixteen',
              'seventeen', 'eighteen', 'nineteen',
              'hundred', 'thousand', 'million', 'billion', 'trillion'):
        candidates.append(w + 'th')
    # magnitude -th forms that end in a vowel need special handling
    # 'hundredth' — already in list above as 'hundredth'
    return frozenset(w for w in candidates if ordinal_to_int(w) is not None)

_ORDINAL_SINGLE_TOKENS = _build_ordinal_tokens()


# ── calculator vocabulary ────────────────────────────────────────────────────

import math as _math
import mpmath as _mpmath

def _sin_deg(x):  return _math.sin(_math.radians(x))
def _cos_deg(x):  return _math.cos(_math.radians(x))
def _tan_deg(x):  return _math.tan(_math.radians(x))

def _asin_deg(x):
    if x < -1 or x > 1:
        return None
    return _math.degrees(_math.asin(x))

def _acos_deg(x):
    if x < -1 or x > 1:
        return None
    return _math.degrees(_math.acos(x))

def _atan_deg(x):  return _math.degrees(_math.atan(x))

# Alias map: canonical word → list of accepted synonyms/homophones.
# Aliases are rewritten to their canonical form as the first step in
# _tokenize_calc, so everything downstream (tokenizer, dedup, logging)
# sees only canonical words.
# Context-dependent words (minus, negative) are NOT aliased here.
CALC_ALIASES = {
    # infix operators — canonical is the shortest/most common spoken form
    'plus':     ['add', 'added'],
    'minus':    ['subtract', 'subtracted'],
    'times':    ['multiplied', 'multiply'],
    'divided':  ['divide', 'over'],
    'mod':      ['modulo', 'remainder'],
    # power
    'power':    ['raised'],
    # postfix unary
    'squared':   [],
    'cubed':     [],
    'factorial': [],
    # prefix unary — root/abs (no aliases needed currently)
    'square':   [],
    'cube':     [],
    'root':     [],
    'absolute': [],
    'value':    [],
    # trig — canonical + all Riva variants/homophones
    'sine':     ['sin', 'sign'],
    'cosine':   ['cosign'],
    'tangent':  [],
    # inverse trig modifier + Riva mishear variants for "arc"
    'inverse':  ['arc', 'arxie', 'ark'],
    # mathematical constants
    'pi':           [],
    'tau':          ['tao', 'taw'],
    'euler':        ['e', 'oiler'],
    'apery':        ['apory', 'apri'],
    'catalan':      ['catalon'],
    'mascheroni':   ['mascueroni', 'mascarone'],
    # logarithms
    'log':          [],
    'natural':      [],   # 'natural log' two-token → ln
    'ln':           [],   # single-token natural log (low-confidence Riva alias)
    'base':         [],   # modifier word for log base N
    # 'golden' + 'ratio' are only valid as a two-token pair — both in CALC_WORDS
    # so the slot buffer passes them through; tokenizer handles the pair.
    'golden':   [],
    'ratio':    [],
}

# Reverse map: alias word → canonical word (built from CALC_ALIASES)
_ALIAS_TO_CANONICAL = {
    alias: canonical
    for canonical, aliases in CALC_ALIASES.items()
    for alias in aliases
}

# Operator canonicals → symbol (minus handled separately for sign disambiguation)
OPERATOR_WORDS = {
    'plus': '+',
    'minus': '-',
    'times': '*',
    'divided': '/',
    'mod': '%',
}

# Postfix unary: canonical word immediately after a number → apply fn to number
# factorial returns None for non-integer or negative input — tokenizer checks this.
_POSTFIX_UNARY = {
    'squared':   lambda x: x ** 2,
    'cubed':     lambda x: x ** 3,
    'factorial': lambda x: _math.factorial(int(x)) if x >= 0 and x == int(x) else None,
}

# Inverse trig: maps trig canonical → inverse function (result in degrees)
_INVERSE_TRIG = {
    'sine':    _asin_deg,
    'cosine':  _acos_deg,
    'tangent': _atan_deg,
}

# Mathematical constants: canonical token(s) → float value.
# Single-token constants use a plain string key; two-token use a tuple.
# All alias rewriting has already happened before these are checked.
_GOLDEN_RATIO = (1 + _math.sqrt(5)) / 2  # ≈ 1.618

_CONSTANTS = {
    'pi':                _math.pi,       # ≈ 3.14159
    'tau':               _math.tau,      # ≈ 6.28318  (2π)
    'euler':             _math.e,        # ≈ 2.71828  (aliases: e, oiler)
    ('golden', 'ratio'): _GOLDEN_RATIO,  # ≈ 1.61803
    'apery':             float(_mpmath.zeta(3)),      # Apéry's constant ζ(3)
    'catalan':           float(_mpmath.catalan),      # Catalan's constant G
    'mascheroni':        float(_mpmath.euler),        # Euler-Mascheroni constant γ
}

# Prefix unary: canonical phrase tuple → function applied to following operand
# 'of' is intentionally absent — it's a filler the budget system handles.
# Longer phrases first (greediest match wins).
# Note: inverse trig is handled separately in _tokenize_calc via _INVERSE_TRIG
# so that "inverse" composes with the already-aliased trig words automatically.
_PREFIX_UNARY_PHRASES = [
    (('square', 'root'), lambda x: x ** 0.5),
    (('cube',   'root'), lambda x: x ** (1/3) if x >= 0 else -((-x) ** (1/3))),
    (('absolute', 'value'), abs),
    (('sine',),    _sin_deg),
    (('cosine',),  _cos_deg),
    (('tangent',), _tan_deg),
]

# All words valid inside a {number_calc} slot — canonical words plus all their
# aliases. Built automatically so adding an alias here is the only change needed.
CALC_WORDS = (
    NUMBER_WORDS
    | SIGN_WORDS
    | DECIMAL_WORDS
    | frozenset(CALC_ALIASES)           # all canonical words
    | frozenset(_ALIAS_TO_CANONICAL)    # all alias words
    | _ORDINAL_SINGLE_TOKENS            # ordinal exponent/root words
)


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
               'power' | 'root' | 'abs' | 'trig' | 'mixed'
    """
    # Rewrite aliases to canonical form first so all downstream logic is uniform.
    # 'minus'/'negative' are intentionally NOT in CALC_ALIASES — context-dependent.
    tokens = [_ALIAS_TO_CANONICAL.get(t, t) for t in tokens]

    result = []
    i = 0
    n = len(tokens)
    _ops_seen = set()  # track which operation types appear

    def last_token_is_number():
        return result and isinstance(result[-1], (int, float))

    def apply_postfix(val, tok):
        """Apply a postfix op, returning None on domain error."""
        r = _POSTFIX_UNARY[tok](val)
        return r  # lambda returns None on domain error

    def parse_number_at(pos, allow_ordinal=False) -> tuple[float | int | None, int]:
        """Parse an integer (+ optional decimal suffix) starting at pos.
        Returns (value, tokens_consumed) or (None, 0) on failure.

        allow_ordinal: also accept ordinal words as a number value
        (used after '**' so 'fifth' in 'three to the fifth power' works).
        """
        num_val = None
        consumed = 0
        for length in range(min(n - pos, 20), 0, -1):
            sub = ' '.join(tokens[pos:pos + length])
            val = words_to_int(sub)
            if val is not None:
                num_val = val
                consumed = length
                break
        if num_val is None and allow_ordinal:
            # Try ordinal — greedy, longest match first
            for length in range(min(n - pos, 20), 0, -1):
                sub = ' '.join(tokens[pos:pos + length])
                val = ordinal_to_int(sub)
                if val is not None:
                    num_val = val
                    consumed = length
                    break
        if num_val is None:
            # Try two-token constant first, then single-token
            if pos + 1 < n and (tokens[pos], tokens[pos + 1]) in _CONSTANTS:
                num_val = _CONSTANTS[(tokens[pos], tokens[pos + 1])]
                consumed = 2
            elif tokens[pos] in _CONSTANTS:
                num_val = _CONSTANTS[tokens[pos]]
                consumed = 1
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

        # ── logarithms ────────────────────────────────────────────────────────
        # Forms (after alias rewrite, 'of' stripped as filler):
        #   "log <number>"              → log base 10
        #   "log base <number> <number>"→ log arbitrary base
        #   "natural log <number>"      → natural log (base e)
        #   "ln <number>"               → natural log (base e)
        is_natural_log = (
            tok == 'ln' or
            (tok == 'natural' and i + 1 < n and tokens[i + 1] == 'log')
        )
        is_log = tok == 'log'

        if is_natural_log or is_log:
            j = i + (2 if tok == 'natural' else 1)  # skip 'natural log' or 'log'/'ln'
            if j >= n:
                return None

            log_base = None
            if is_log and j < n and tokens[j] == 'base':
                # "log base <N> <X>" — parse base then argument
                j += 1
                if j >= n:
                    return None
                base_val, base_consumed = parse_number_at(j)
                if base_val is None or base_val <= 0 or base_val == 1:
                    return None
                log_base = base_val
                j += base_consumed

            if j >= n:
                return None
            sign = 1
            if tokens[j] in ('minus', 'negative'):
                sign = -1
                j += 1
                if j >= n:
                    return None
            val, consumed = parse_number_at(j)
            if val is None:
                return None
            arg = sign * val
            if arg <= 0:
                return None  # log of non-positive undefined
            k = j + consumed
            while k < n and tokens[k] in _POSTFIX_UNARY:
                arg = _POSTFIX_UNARY[tokens[k]](arg)
                if arg is None:
                    return None
                k += 1
            if is_natural_log:
                result.append(_math.log(arg))
            else:
                result.append(_math.log10(arg) if log_base is None
                              else _math.log(arg, log_base))
            _ops_seen.add('log')
            i = k
            continue

        # ── nth root: "<ordinal> root <number>" ──────────────────────────────
        # "fourth root of sixteen" → slot buffer: "fourth root sixteen" → 2.0
        # Ordinal gives n; result = operand ** (1/n).
        # Negative cube-root-style: preserve sign for odd n, undefined for even n.
        #
        # Multi-word ordinal lookahead: try the longest span ending just before
        # 'root' — e.g. "sixty fourth root two" → ordinal span "sixty fourth"
        # = 64, not greedy-cardinal "sixty" = 60 followed by "fourth root".
        ord_val = None
        ord_span = 0
        for length in range(min(n - i, 20), 0, -1):
            if i + length < n and tokens[i + length] == 'root':
                sub = ' '.join(tokens[i:i + length])
                v = ordinal_to_int(sub)
                if v is not None:
                    ord_val, ord_span = v, length
                    break
        if ord_val is None:
            # Fall back to single-token check (already covered by loop above,
            # but kept explicit for clarity when no 'root' follows at all)
            ord_val = ordinal_to_int(tok)
            ord_span = 1
        if ord_val is not None and i + ord_span < n and tokens[i + ord_span] == 'root':
            n_root = ord_val
            if n_root < 1:
                return None
            j = i + ord_span + 1  # skip ordinal span + 'root'
            if j >= n:
                return None
            sign = 1
            if tokens[j] in ('minus', 'negative'):
                sign = -1
                j += 1
                if j >= n:
                    return None
            val, consumed = parse_number_at(j)
            if val is None:
                return None
            k = j + consumed
            while k < n and tokens[k] in _POSTFIX_UNARY:
                val = _POSTFIX_UNARY[tokens[k]](sign * val)
                if val is None:
                    return None
                sign = 1
                k += 1
            base = sign * val
            if base < 0 and n_root % 2 == 0:
                return None  # even root of negative — undefined in reals
            if base < 0:
                result.append(-((-base) ** (1 / n_root)))
            else:
                result.append(base ** (1 / n_root))
            _ops_seen.add('root')
            i = k
            continue

        # ── inverse trig: "inverse sine/cosine/tangent <number>" ─────────────
        # Handled before _PREFIX_UNARY_PHRASES so "inverse" composes with the
        # already-aliased trig canonicals — "inverse sign" works because the
        # alias rewrite above already turned "sign" → "sine".
        if tok == 'inverse' and i + 1 < n and tokens[i + 1] in _INVERSE_TRIG:
            fn = _INVERSE_TRIG[tokens[i + 1]]
            j = i + 2
            if j >= n:
                return None
            sign = 1
            if tokens[j] in ('minus', 'negative'):
                sign = -1
                j += 1
                if j >= n:
                    return None
            val, consumed = parse_number_at(j)
            if val is None:
                return None
            k = j + consumed
            while k < n and tokens[k] in _POSTFIX_UNARY:
                val = _POSTFIX_UNARY[tokens[k]](sign * val)
                if val is None:
                    return None
                sign = 1
                k += 1
            computed = fn(sign * val)
            if computed is None:
                return None  # domain error (asin/acos out of [-1, 1])
            result.append(computed)
            _ops_seen.add('inverse_trig')
            i = k
            continue

        # ── prefix unary (square root / cube root / absolute value / trig) ───
        for phrase, fn in _PREFIX_UNARY_PHRASES:
            plen = len(phrase)
            if tuple(tokens[i:i + plen]) == phrase:
                j = i + plen
                if j >= n:
                    return None
                sign = 1
                if tokens[j] in ('minus', 'negative'):
                    sign = -1
                    j += 1
                    if j >= n:
                        return None
                val, consumed = parse_number_at(j)
                if val is None:
                    return None
                k = j + consumed
                # Apply all immediately following postfix unary ops to the operand
                # before passing to the prefix function — handles chaining like
                # "sine two squared cubed" → fn((2²)³) = sin(64°)
                while k < n and tokens[k] in _POSTFIX_UNARY:
                    val = _POSTFIX_UNARY[tokens[k]](sign * val)
                    if val is None:
                        return None
                    sign = 1
                    k += 1
                result.append(fn(sign * val))
                i = k
                if phrase[0] in ('square', 'cube'):
                    _ops_seen.add('root')
                elif phrase[0] in ('sine', 'cosine', 'tangent'):
                    _ops_seen.add('trig')
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
                    pass  # fall through to number parsing as sign

            # ── postfix unary (squared / cubed / factorial) ───────────────────
            elif tok in _POSTFIX_UNARY and last_token_is_number():
                val = _POSTFIX_UNARY[tok](result[-1])
                if val is None:
                    return None  # domain error (e.g. factorial of non-integer)
                result[-1] = val
                _ops_seen.add('factorial' if tok == 'factorial' else 'power')
                i += 1
                continue

            # ── infix power — 'power' after a number means exponent ───────────
            # 'raised' rewrites to 'power' via alias; skip duplicate if both
            # were spoken ('two raised power eight' → 'two power power eight').
            elif tok == 'power' and last_token_is_number():
                result.append('**')
                _ops_seen.add('power')
                i += 1
                # skip a consecutive 'power' (artifact of raised→power rewrite)
                if i < n and tokens[i] == 'power':
                    i += 1
                continue

            # ── operator words ────────────────────────────────────────────────
            if tok in OPERATOR_WORDS and tok not in ('minus', 'negative'):
                if not last_token_is_number():
                    return None  # operator without a left operand
                sym = OPERATOR_WORDS[tok]
                result.append(sym)
                _ops_seen.add({'+': 'add', '-': 'subtract', '*': 'multiply',
                               '/': 'divide', '%': 'modulo'}[sym])
                i += 1
                continue

            # ── ordinal + power: "<number> <ordinal> power" ──────────────────
            # e.g. slot buffer "three fifth power" → 3 ** 5.
            # Consume the ordinal span + 'power' together so the result list
            # stays in [number, op, number] order.
            if last_token_is_number():
                ord_exp = None
                ord_consumed = 0
                for length in range(min(n - i, 20), 0, -1):
                    if i + length < n and tokens[i + length] == 'power':
                        sub = ' '.join(tokens[i:i + length])
                        v = ordinal_to_int(sub)
                        if v is not None:
                            ord_exp, ord_consumed = v, length
                            break
                if ord_exp is not None:
                    result.append('**')
                    result.append(ord_exp)
                    _ops_seen.add('power')
                    i += ord_consumed + 1  # skip ordinal tokens + 'power'
                    # skip duplicate 'power' from raised→power alias artifact
                    if i < n and tokens[i] == 'power':
                        i += 1
                    continue

            # ── mathematical constants ────────────────────────────────────────
            # Two-token first (greedy), then single-token.
            # Sign before a constant: "negative pi" → -π.
            const_sign = 1
            const_start = i
            if tok in ('minus', 'negative') and not last_token_is_number():
                const_sign = -1
                const_start = i + 1

            const_val = None
            const_consumed = 0
            if const_start < n:
                # try two-token constant first
                if const_start + 1 < n:
                    two = (tokens[const_start], tokens[const_start + 1])
                    if two in _CONSTANTS:
                        const_val = _CONSTANTS[two]
                        const_consumed = (const_start + 2) - i
                # then single-token
                if const_val is None and tokens[const_start] in _CONSTANTS:
                    const_val = _CONSTANTS[tokens[const_start]]
                    const_consumed = (const_start + 1) - i

            if const_val is not None:
                # allow postfix ops on constants: "pi squared", "tau cubed"
                k = i + const_consumed
                val = const_sign * const_val
                while k < n and tokens[k] in _POSTFIX_UNARY:
                    val = _POSTFIX_UNARY[tokens[k]](val)
                    if val is None:
                        return None
                    k += 1
                result.append(val)
                _ops_seen.add('constant')
                i = k
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
                                    'modulo', 'power', 'root', 'abs', 'trig',
                                    'inverse_trig', 'mixed'
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
