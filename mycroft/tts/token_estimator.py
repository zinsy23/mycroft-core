"""
Token cost estimator for TTS backends with preprocessor token limits.

Loads a calibration JSON produced by scripts/calibrate_tts_tokens.py and
estimates token counts before sending text to the backend, enabling
proactive splitting with no error round-trips needed.
"""

import re
import os
import math
import json

_CALIBRATION_DIR = os.path.expanduser('~/.config/mycroft/tts_token_maps')

# Regex classifiers — ordered most-specific first
_CLASSIFIERS = [
    (re.compile(r'^-0\.\d+$'),      'decimal_neg_0xx'),
    (re.compile(r'^-\d+\.\d+$'),    'decimal_neg_xdxx'),
    (re.compile(r'^0\.\d+$'),       'decimal_0xx'),
    (re.compile(r'^\d\.\d{2,}$'),   'decimal_xdxx'),
    (re.compile(r'^\d\.\d$'),       'decimal_xdx'),
    (re.compile(r'^\d{2}$'),        'int_2digit'),
    (re.compile(r'^\d$'),           'int_1digit'),
    (re.compile(r'^negative$', re.I), 'word_negative'),
    (re.compile(r'^times$', re.I),  'word_times'),
    (re.compile(r'^point$', re.I),  'word_point'),
    (re.compile(r'^plus$', re.I),   'word_plus'),
    (re.compile(r'^is$', re.I),     'word_is'),
    (re.compile(r'^and$', re.I),    'word_and'),
]

# Conservative fallback costs (measured on English-US-RadTTSpp)
_DEFAULT_COSTS = {
    'decimal_neg_0xx':  32,
    'decimal_neg_xdxx': 29,
    'decimal_0xx':      24,
    'decimal_xdxx':     21,
    'decimal_xdx':      18,
    'int_2digit':       11,
    'int_1digit':        6,
    'word_negative':     9,
    'word_times':        7,
    'word_point':        7,
    'word_plus':         6,
    'word_is':           4,
    'word_and':          4,
    'word_default':      7,
}


def _classify(word):
    for pattern, name in _CLASSIFIERS:
        if pattern.match(word):
            return name
    return 'word_default'


class TokenEstimator:
    """Estimates token count for a TTS string and pre-splits to fit a limit."""

    def __init__(self, calibration: dict = None):
        costs = _DEFAULT_COSTS.copy()
        if calibration:
            costs.update({k: round(v) for k, v in calibration.get('token_costs', {}).items()})
        self._costs = costs
        self._limit = (calibration or {}).get('token_limit', 400)

    @classmethod
    def load(cls, voice_name: str) -> 'TokenEstimator':
        """Load calibration for voice_name, fall back to defaults if not found."""
        path = os.path.join(_CALIBRATION_DIR, voice_name + '.json')
        if os.path.exists(path):
            with open(path) as f:
                return cls(json.load(f))
        return cls()

    def estimate(self, text: str) -> int:
        return sum(self._costs.get(_classify(w), self._costs['word_default'])
                   for w in text.split())

    def split_to_fit(self, text: str, limit: int = None) -> list:
        """Split text into chunks each estimated to fit within limit."""
        limit = limit or self._limit
        if self.estimate(text) <= limit:
            return [text]
        words = text.split()
        chunks, current, current_cost = [], [], 0
        for word in words:
            cost = self._costs.get(_classify(word), self._costs['word_default'])
            if current and current_cost + cost > limit:
                chunks.append(' '.join(current))
                current, current_cost = [], 0
            current.append(word)
            current_cost += cost
        if current:
            chunks.append(' '.join(current))
        return chunks
