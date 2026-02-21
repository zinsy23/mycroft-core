"""
Streaming Command Matcher for real-time intent matching.

Matches commands word-by-word as they arrive from Vosk, allowing immediate
execution without waiting for phrase completion.
"""

import re
from mycroft.util.log import LOG


class CommandPattern:
    """Represents a single command pattern from a Padatious .intent file.

    Expands Padatious syntax into concrete word sequences.
    Example: "turn [on|off] lights" becomes:
      - ["turn", "on", "lights"]
      - ["turn", "off", "lights"]
    """

    def __init__(self, intent_name, pattern_str, original_line):
        self.intent_name = intent_name
        self.original_line = original_line
        self.word_sequences = self._expand_pattern(pattern_str)

    def _expand_pattern(self, pattern_str):
        """Expand Padatious pattern into all possible concrete word sequences.

        Args:
            pattern_str: Pattern like "turn [on|off] [the|my|] lights"

        Returns:
            List of word sequences, each sequence is a list of dicts:
            [
                [{'word': 'turn'}, {'word': 'on'}, {'word': 'the'}, {'word': 'lights'}],
                [{'word': 'turn'}, {'word': 'on'}, {'word': 'lights'}],
                ...
            ]
            Entities are marked: {'entity': 'value'}
        """
        parts = pattern_str.split()

        # Parse each part into possible options
        part_options = []
        for part in parts:
            if '{' in part and '}' in part:
                # Entity - represents a wildcard position
                entity_name = part.strip('{}')
                part_options.append([{'entity': entity_name}])
            elif '[' in part and ']' in part:
                # Optional or choice
                inner = part.strip('[]')
                if '|' in inner:
                    # Choice like [on|off] or [the|my|]
                    options = inner.split('|')
                    # Empty string means this position is optional (can be skipped)
                    variants = []
                    for opt in options:
                        if opt:  # Non-empty option
                            variants.append({'word': opt.lower()})
                        else:  # Empty means skip this position
                            variants.append(None)  # None = skip this position
                    part_options.append(variants)
                else:
                    # Simple optional like [my]
                    if inner:
                        part_options.append([{'word': inner.lower()}, None])  # Can match or skip
            else:
                # Required word
                part_options.append([{'word': part.lower()}])

        # Generate all combinations (cartesian product)
        import itertools
        sequences = []
        for combo in itertools.product(*part_options):
            # Filter out None values (skipped optional positions)
            sequence = [item for item in combo if item is not None]
            if sequence:  # Don't add empty sequences
                sequences.append(sequence)

        return sequences


class PartialMatch:
    """Tracks a partial command match in progress for one specific word sequence."""

    def __init__(self, pattern, sequence_index, filler_config):
        self.pattern = pattern  # CommandPattern
        self.sequence = pattern.word_sequences[sequence_index]  # Specific word sequence we're matching
        self.sequence_index = sequence_index
        self.matched_words = []  # Actual words matched so far
        self.sequence_position = 0  # Current position in sequence
        self.filler_budget = filler_config.get('base_max', 4)  # Remaining filler budget
        self.entities = {}  # Captured entity values
        self.filler_config = filler_config

    def try_add_word(self, word):
        """Try to add a word to this partial match.

        Much simpler now - just check if word matches next expected position.
        No optionals to handle since pattern is already expanded.

        Returns:
            str: 'match' if word extends this pattern
                 'complete' if word completes this pattern
                 'filler' if word doesn't match but within filler budget
                 'dead' if word doesn't match and exceeded filler budget
        """
        word = word.lower()

        # Check if we've completed the sequence
        if self.sequence_position >= len(self.sequence):
            # Already complete, can't add more words
            if self.filler_budget > 0:
                self.filler_budget -= 1
                return 'filler'
            else:
                return 'dead'

        # Get expected next item in sequence
        expected = self.sequence[self.sequence_position]

        if 'entity' in expected:
            # Entity matches any word
            entity_name = expected['entity']
            self.entities[entity_name] = word
            self.matched_words.append(word)
            self.sequence_position += 1
            self._reset_filler_budget()

            if self.sequence_position >= len(self.sequence):
                return 'complete'
            return 'match'

        elif 'word' in expected:
            # Expected word must match exactly
            if word == expected['word']:
                # Match!
                self.matched_words.append(word)
                self.sequence_position += 1
                self._reset_filler_budget()

                if self.sequence_position >= len(self.sequence):
                    return 'complete'
                return 'match'
            else:
                # Doesn't match - this is a filler
                if self.filler_budget > 0:
                    self.filler_budget -= 1
                    return 'filler'
                else:
                    return 'dead'

        # Shouldn't get here
        return 'dead'

    def _reset_filler_budget(self):
        """Reset filler budget after a successful match.

        Uses max() to keep higher budget if consecutive valid words built it up.
        """
        if len(self.matched_words) == 1:
            # First word matched, set to in_command_base
            new_budget = self.filler_config.get('in_command_base', 5)
        else:
            # Each additional matched word adds increment
            in_command_base = self.filler_config.get('in_command_base', 5)
            increment = self.filler_config.get('increment_per_word', 1)
            new_budget = in_command_base + ((len(self.matched_words) - 1) * increment)

        # Keep the higher budget (allows it to stay high with consecutive valid words)
        self.filler_budget = max(self.filler_budget, new_budget)

    def get_utterance(self):
        """Get the matched utterance text."""
        return ' '.join(self.matched_words)


class StreamingCommandMatcher:
    """Matches commands incrementally as words arrive."""

    def __init__(self, filler_config):
        """Initialize the command matcher.

        Args:
            filler_config (dict): Filler word configuration
        """
        self.patterns = []  # List of CommandPattern objects
        self.active_matches = []  # List of PartialMatch objects in progress
        self.consecutive_fillers = 0  # Consecutive words that didn't extend any match
        self.filler_config = filler_config
        self.base_max_fillers = filler_config.get('base_max', 4)

    def register_intent(self, intent_name, pattern_lines):
        """Register a Padatious intent pattern.

        Args:
            intent_name (str): The intent name
            pattern_lines (list): Lines from the .intent file
        """
        for line in pattern_lines:
            line = line.strip()
            if not line:
                continue

            pattern = CommandPattern(intent_name, line, line)
            self.patterns.append(pattern)

        LOG.debug(f"Registered {len(pattern_lines)} patterns for {intent_name}")

    def add_word(self, word):
        """Process a new word from Vosk.

        Args:
            word (str): The word to process

        Returns:
            dict or None: Match result if complete command matched, else None
        """
        word = word.lower().strip()
        if not word:
            return None

        LOG.debug(f"Processing word: '{word}' | Active matches: {len(self.active_matches)}")

        extended_any_match = False
        completed_matches = []
        surviving_matches = []

        # Try to extend existing active matches
        for match in self.active_matches:
            result = match.try_add_word(word)
            LOG.debug(f"  Pattern '{match.pattern.original_line}' -> {result}")

            if result == 'complete':
                completed_matches.append(match)
                extended_any_match = True
            elif result == 'match':
                surviving_matches.append(match)
                extended_any_match = True
            elif result == 'filler':
                surviving_matches.append(match)
                # Filler for this match, but might match others
            # 'dead' matches are discarded

        # Try to start new matches with this word
        for pattern in self.patterns:
            # Try each expanded sequence from this pattern
            for seq_idx in range(len(pattern.word_sequences)):
                new_match = PartialMatch(pattern, seq_idx, self.filler_config)
                result = new_match.try_add_word(word)
                LOG.debug(f"  New pattern '{pattern.original_line}' [seq {seq_idx}] -> {result}")

                if result == 'complete':
                    completed_matches.append(new_match)
                    extended_any_match = True
                elif result == 'match':
                    surviving_matches.append(new_match)
                    extended_any_match = True

        # Update active matches
        self.active_matches = surviving_matches

        # Update consecutive filler count
        if extended_any_match:
            self.consecutive_fillers = 0
        else:
            self.consecutive_fillers += 1
            LOG.debug(f"  Filler word. Consecutive: {self.consecutive_fillers}/{self.base_max_fillers}")

        # Return first completed match if any
        if completed_matches:
            # Pick the longest/best match
            best_match = max(completed_matches, key=lambda m: len(m.matched_words))

            return {
                'intent': best_match.pattern.intent_name,
                'utterance': best_match.get_utterance(),
                'entities': best_match.entities,
                'pattern': best_match.pattern.original_line
            }

        return None

    def should_timeout(self):
        """Check if too many consecutive filler words to continue session."""
        return self.consecutive_fillers >= self.base_max_fillers

    def reset(self):
        """Reset matcher state for new session."""
        self.active_matches = []
        self.consecutive_fillers = 0


def test_matcher():
    """Test the command matcher with simulated word stream."""
    print("=" * 60)
    print("Testing StreamingCommandMatcher")
    print("=" * 60)

    matcher = StreamingCommandMatcher({
        'base_max': 4,
        'in_command_base': 5,
        'increment_per_word': 1
    })

    # Register test patterns
    matcher.register_intent('hue:lights', [
        'turn [on|off] [the|my|] lights',
        'lights [on|off]',
        'set brightness {value}'
    ])

    print("\nPatterns registered:")
    for p in matcher.patterns:
        print(f"  - {p.original_line}")

    # Test cases
    test_cases = [
        {
            'name': 'Simple: turn on lights',
            'words': ['turn', 'on', 'lights'],
            'expected': 'turn on lights'
        },
        {
            'name': 'With optional: turn on the lights',
            'words': ['turn', 'on', 'the', 'lights'],
            'expected': 'turn on the lights'
        },
        {
            'name': 'With fillers: turn um on uh the lights',
            'words': ['turn', 'um', 'on', 'uh', 'the', 'lights'],
            'expected': 'turn on the lights'
        },
        {
            'name': 'Short form: lights on',
            'words': ['lights', 'on'],
            'expected': 'lights on'
        },
        {
            'name': 'Entity: set brightness fifty',
            'words': ['set', 'brightness', 'fifty'],
            'expected': 'set brightness fifty'
        },
        {
            'name': 'Multiple fillers: turn like uh um on lights',
            'words': ['turn', 'like', 'uh', 'um', 'on', 'lights'],
            'expected': 'turn on lights'  # Should fail - too many consecutive fillers before 'on'
        }
    ]

    for test in test_cases:
        print(f"\n{'=' * 60}")
        print(f"Test: {test['name']}")
        print(f"Words: {test['words']}")
        print(f"{'=' * 60}")

        matcher.reset()
        result = None

        for i, word in enumerate(test['words'], 1):
            print(f"\nWord {i}: '{word}'")
            result = matcher.add_word(word)

            if result:
                print(f"  ✓ MATCH FOUND: '{result['utterance']}'")
                print(f"    Intent: {result['intent']}")
                print(f"    Pattern: {result['pattern']}")
                if result['entities']:
                    print(f"    Entities: {result['entities']}")
                break

        if not result:
            print(f"\n  ✗ No match found")
            print(f"    Consecutive fillers: {matcher.consecutive_fillers}")
            print(f"    Active matches: {len(matcher.active_matches)}")

        print(f"\nExpected: {test['expected']}")
        print(f"Got: {result['utterance'] if result else 'None'}")
        print(f"Status: {'✓ PASS' if (result and result['utterance'] == test['expected']) or (not result and test['expected'].startswith('Should fail')) else '✗ FAIL'}")


if __name__ == '__main__':
    test_matcher()
