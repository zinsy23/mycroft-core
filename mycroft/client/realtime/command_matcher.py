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

        Properly handles suffixes like light[s|] -> lights, light

        Args:
            pattern_str: Pattern like "turn [on|off] light[s|]"

        Returns:
            List of word sequences, each sequence is a list of dicts:
            [
                [{'word': 'turn'}, {'word': 'on'}, {'word': 'lights'}],
                [{'word': 'turn'}, {'word': 'on'}, {'word': 'light'}],
                ...
            ]
            Entities are marked: {'entity': 'value'}
        """
        import re
        import itertools

        # Helper to expand word+suffix patterns like "light[s|]"
        def expand_word_suffixes(token):
            """Expand word[suffix|suffix|] into variants."""
            # Match: word + [variants|variants|]
            pattern = r'^(\w+)(\[[\w|]*\])$'
            match = re.match(pattern, token)
            if match:
                base_word = match.group(1)
                suffix_part = match.group(2)[1:-1]  # Remove [ ]
                suffix_options = suffix_part.split('|')
                return [base_word + suffix if suffix else base_word for suffix in suffix_options]
            return None

        # Split pattern into tokens, preserving brackets and entities as single tokens
        # This handles multi-word options like [how to|peanut butter]
        tokens = []
        current_token = ""
        in_bracket = False
        in_entity = False

        for char in pattern_str:
            if char == '[':
                if current_token.strip():
                    tokens.append(current_token.strip())
                current_token = char
                in_bracket = True
            elif char == ']':
                current_token += char
                in_bracket = False
                tokens.append(current_token)
                current_token = ""
            elif char == '{':
                if current_token.strip():
                    tokens.append(current_token.strip())
                current_token = char
                in_entity = True
            elif char == '}':
                current_token += char
                in_entity = False
                tokens.append(current_token)
                current_token = ""
            elif char == ' ' and not in_bracket and not in_entity:
                if current_token.strip():
                    tokens.append(current_token.strip())
                current_token = ""
            else:
                current_token += char

        # Add final token if any
        if current_token.strip():
            tokens.append(current_token.strip())

        all_token_options = []

        for token in tokens:
            if '{' in token and '}' in token:
                # Entity like {value}
                entity_name = token.strip('{}')
                all_token_options.append([{'entity': entity_name}])

            elif '[' in token and ']' in token:
                # Check if it's a word+suffix or standalone choice
                suffix_variants = expand_word_suffixes(token)

                if suffix_variants:
                    # Word with suffix like light[s|] -> ['lights', 'light']
                    all_token_options.append([{'word': v.lower()} for v in suffix_variants])
                else:
                    # Standalone choice like [on|off] or [the|my|] or multi-word [how to|peanut butter]
                    inner = token.strip('[]')
                    choices = inner.split('|')
                    # Empty choice means optional (can be skipped with None)
                    variants = []
                    for choice in choices:
                        if choice:
                            # Split multi-word choices into separate words
                            words = choice.strip().split()
                            if len(words) == 1:
                                variants.append({'word': words[0].lower()})
                            else:
                                # Multi-word option - create a tuple of word dicts
                                variants.append(tuple({'word': w.lower()} for w in words))
                        else:
                            variants.append(None)  # None = skip this position
                    all_token_options.append(variants)
            else:
                # Plain required word
                all_token_options.append([{'word': token.lower()}])

        # Generate all combinations (cartesian product)
        sequences = []
        for combo in itertools.product(*all_token_options):
            # Filter out None values (skipped optional positions) and flatten tuples
            sequence = []
            for item in combo:
                if item is None:
                    continue
                elif isinstance(item, tuple):
                    # Multi-word option - add all words
                    sequence.extend(item)
                else:
                    sequence.append(item)
            if sequence:  # Don't add empty sequences
                sequences.append(sequence)

        return sequences


class StreamingCommandMatcher:
    """Matches commands incrementally as words arrive.

    Uses a single word list approach: tracks one list of matched words,
    and validates each new word against all possible sequences that match
    the current list.
    """

    def __init__(self, filler_config):
        """Initialize the command matcher.

        Args:
            filler_config (dict): Filler word configuration
        """
        self.patterns = []  # List of CommandPattern objects
        self.matched_words = []  # Current word list being built
        self.consecutive_fillers = 0  # Consecutive words that didn't extend the list
        self.filler_config = filler_config
        self.base_max_fillers = filler_config.get('base_max', 4)

        # Global filler budget that persists across command matches
        # Increases with consecutive valid words, resets to base_max after command match
        self.filler_budget = self.base_max_fillers

        # Cache of all expanded sequences for faster matching
        self.all_sequences = []  # Will be populated when patterns are registered

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

            # Add all expanded sequences to cache
            for sequence in pattern.word_sequences:
                self.all_sequences.append({
                    'intent': intent_name,
                    'pattern': line,
                    'sequence': sequence
                })

        LOG.debug(f"Registered {len(pattern_lines)} patterns for {intent_name}")

    def add_word(self, word):
        """Process a new word from STT.

        Uses single-list approach: checks if word is valid next word based on
        all sequences that match current list.

        Args:
            word (str): The word to process

        Returns:
            dict or None: Match result if complete command matched, else None
        """
        word = word.lower().strip()
        if not word:
            return None

        LOG.info(f"Processing word: '{word}' | Current list: {self.matched_words} | Budget: {self.filler_budget} | Total sequences: {len(self.all_sequences)}")

        # Find all sequences that match our current word list
        matching_sequences = self._get_matching_sequences(self.matched_words)

        LOG.info(f"  {len(matching_sequences)} sequences match current list")

        # Get all valid next words from those sequences
        valid_next_words = self._get_valid_next_words(matching_sequences)

        LOG.info(f"  Valid next words: {valid_next_words}")

        # Check if incoming word is valid (exact match OR entity position)
        is_valid = word in valid_next_words or '<ENTITY>' in valid_next_words

        LOG.debug(f"  Checking: '{word}' in valid_next_words? {word in valid_next_words}")
        LOG.debug(f"  is_valid: {is_valid}")

        if is_valid:
            # Valid word - add to list
            self.matched_words.append(word)
            self.consecutive_fillers = 0

            # Update budget
            if len(self.matched_words) == 1:
                # First word - budget breaks even (no change)
                pass
            else:
                # Consecutive match - increase budget
                increment = self.filler_config.get('increment_per_word', 1)
                self.filler_budget += increment

            LOG.debug(f"  ✓ Valid word added. List: {self.matched_words}, Budget: {self.filler_budget}")

            # Check if we completed a command
            completed = self._check_completion(self.matched_words)
            if completed:
                LOG.info(f"  ✓ COMPLETE MATCH: {completed}")
                return completed

        else:
            # Filler word - not added to list
            self.consecutive_fillers += 1
            self.filler_budget = max(0, self.filler_budget - 1)
            LOG.info(f"  ✗ Filler word '{word}'. Consecutive: {self.consecutive_fillers}/{self.base_max_fillers}, Budget: {self.filler_budget}")

        return None

    def _get_matching_sequences(self, word_list):
        """Get all sequences that match the given word list so far.

        Args:
            word_list: List of words matched so far

        Returns:
            List of sequence dicts that start with these words
        """
        if not word_list:
            # Empty list - all sequences are potential matches
            return self.all_sequences

        matching = []
        for seq_info in self.all_sequences:
            sequence = seq_info['sequence']

            # Check if sequence starts with our word list
            if len(sequence) < len(word_list):
                continue  # Sequence too short

            # Compare each position
            match = True
            for i, word in enumerate(word_list):
                seq_item = sequence[i]
                if 'word' in seq_item:
                    if seq_item['word'] != word:
                        match = False
                        break
                # Entities match any word at that position
                elif 'entity' in seq_item:
                    continue  # Entity matches anything

            if match:
                matching.append(seq_info)

        return matching

    def _get_valid_next_words(self, matching_sequences):
        """Get set of valid next words from matching sequences.

        Args:
            matching_sequences: List of sequences that match current word list

        Returns:
            Set of valid next words
        """
        valid_words = set()
        next_position = len(self.matched_words)

        for seq_info in matching_sequences:
            sequence = seq_info['sequence']

            if next_position >= len(sequence):
                continue  # Already at end of this sequence

            next_item = sequence[next_position]

            if 'word' in next_item:
                valid_words.add(next_item['word'])
            elif 'entity' in next_item:
                # Entity can match any word - for now, we'll accept the word
                # This is handled specially during completion check
                valid_words.add('<ENTITY>')

        return valid_words

    def _check_completion(self, word_list):
        """Check if word list exactly matches any complete sequence.

        Args:
            word_list: Current word list

        Returns:
            Match dict if complete, None otherwise
        """
        for seq_info in self.all_sequences:
            sequence = seq_info['sequence']

            if len(sequence) != len(word_list):
                continue  # Different length

            # Check if all positions match
            match = True
            entities = {}

            for i, word in enumerate(word_list):
                seq_item = sequence[i]

                if 'word' in seq_item:
                    if seq_item['word'] != word:
                        match = False
                        break
                elif 'entity' in seq_item:
                    # Capture entity value
                    entities[seq_item['entity']] = word

            if match:
                # Complete match found!
                return {
                    'intent': seq_info['intent'],
                    'utterance': ' '.join(word_list),
                    'entities': entities,
                    'pattern': seq_info['pattern']
                }

        return None

    def should_timeout(self):
        """Check if too many consecutive filler words to continue session."""
        return self.consecutive_fillers >= self.base_max_fillers

    def reset(self):
        """Reset matcher state after command execution.

        Budget resets to base_max if lower, otherwise stays at current level.
        This allows chaining commands with accumulated budget from consecutive matches.
        """
        self.matched_words = []
        self.consecutive_fillers = 0

        # Reset budget to base_max, but keep higher budget if we earned it
        self.filler_budget = max(self.base_max_fillers, self.filler_budget)

    def reset_session(self):
        """Reset matcher state for a new session (after wake word).

        Completely resets budget to base_max, unlike reset() which preserves
        accumulated budget for command chaining within a session.
        """
        self.matched_words = []
        self.consecutive_fillers = 0
        self.filler_budget = self.base_max_fillers


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
