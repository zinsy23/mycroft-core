"""
Streaming Command Matcher for real-time intent matching.

Matches commands word-by-word as they arrive from Riva, allowing immediate
execution without waiting for phrase completion.

Uses multi-path evolutionary matching to handle Riva's changing interim results.
Multiple candidate paths compete, weak paths are pruned, and the fittest path wins.
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
        # CRITICAL: Keep word[suffix] patterns together (e.g., light[s|])
        # This handles multi-word options like [how to|peanut butter]
        tokens = []
        current_token = ""
        in_bracket = False
        in_entity = False

        for char in pattern_str:
            if char == '[':
                # DON'T split if we have a word immediately before the bracket
                # This keeps word[suffix] patterns together
                current_token += char
                in_bracket = True
            elif char == ']':
                current_token += char
                in_bracket = False
                # DON'T append yet - check if there's more coming (for word[s|] pattern)
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


class MatcherPath:
    """Single evolutionary path tracking a potential command match.

    Each path maintains its own word list and local budget. Paths compete
    based on fitness (how many valid words matched). Weak paths get pruned,
    strong paths survive. Winner's budget syncs to global.
    """

    def __init__(self, path_id, starting_budget, all_sequences, filler_config):
        """Initialize a new matcher path.

        Args:
            path_id: Unique identifier for this path
            starting_budget: Initial budget inherited from global
            all_sequences: All command patterns to match against
            filler_config: Filler word configuration
        """
        self.path_id = path_id
        self.matched_words = []
        self.local_budget = starting_budget
        self.consecutive_fillers = 0
        self.all_sequences = all_sequences
        self.filler_config = filler_config
        self.base_max_fillers = filler_config.get('base_max', 4)
        self.fitness_score = 0  # Number of valid words matched

    def try_add_word(self, word):
        """Try to add a word to this path.

        Args:
            word: The word to try adding

        Returns:
            bool: True if word was valid and added, False if it's a filler
        """
        word = word.lower().strip()

        # Get sequences matching current word list
        matching_sequences = self._get_matching_sequences()

        # Get valid next words
        valid_next_words = self._get_valid_next_words(matching_sequences)

        # Check if word is valid
        is_valid = word in valid_next_words or '<ENTITY>' in valid_next_words

        if is_valid:
            # Valid word - add to list
            self.matched_words.append(word)
            self.consecutive_fillers = 0
            self.fitness_score += 1

            # Update local budget
            if len(self.matched_words) == 1:
                # First word - budget breaks even
                pass
            else:
                # Consecutive match - increase budget
                increment = self.filler_config.get('increment_per_word', 1)
                self.local_budget += increment

            return True
        else:
            # Filler word - not added
            self.consecutive_fillers += 1
            self.local_budget = max(0, self.local_budget - 1)
            return False

    def _get_matching_sequences(self):
        """Get sequences matching current word list."""
        if not self.matched_words:
            return self.all_sequences

        matching = []
        for seq_info in self.all_sequences:
            sequence = seq_info['sequence']

            if len(sequence) < len(self.matched_words):
                continue

            match = True
            for i, word in enumerate(self.matched_words):
                seq_item = sequence[i]
                if 'word' in seq_item:
                    if seq_item['word'] != word:
                        match = False
                        break
                # Entities match anything

            if match:
                matching.append(seq_info)

        return matching

    def _get_valid_next_words(self, matching_sequences):
        """Get valid next words from matching sequences."""
        valid_words = set()
        next_position = len(self.matched_words)

        for seq_info in matching_sequences:
            sequence = seq_info['sequence']

            if next_position >= len(sequence):
                continue

            next_item = sequence[next_position]

            if 'word' in next_item:
                valid_words.add(next_item['word'])
            elif 'entity' in next_item:
                valid_words.add('<ENTITY>')

        return valid_words

    def check_completion(self):
        """Check if this path has completed a command.

        Returns:
            dict or None: Match result if complete, None otherwise
        """
        for seq_info in self.all_sequences:
            sequence = seq_info['sequence']

            if len(sequence) != len(self.matched_words):
                continue

            match = True
            entities = {}

            for i, word in enumerate(self.matched_words):
                seq_item = sequence[i]

                if 'word' in seq_item:
                    if seq_item['word'] != word:
                        match = False
                        break
                elif 'entity' in seq_item:
                    entities[seq_item['entity']] = word

            if match:
                return {
                    'intent': seq_info['intent'],
                    'utterance': ' '.join(self.matched_words),
                    'entities': entities,
                    'pattern': seq_info['pattern']
                }

        return None

    def is_viable(self):
        """Check if this path is still viable (not exhausted or dead-end).

        Returns:
            bool: True if path should continue, False if it should be pruned
        """
        # Budget exhausted
        if self.local_budget <= 0:
            return False

        # Too many consecutive fillers
        if self.consecutive_fillers >= self.base_max_fillers:
            return False

        # No possible continuations
        matching_sequences = self._get_matching_sequences()
        if not matching_sequences:
            return False

        return True


class StreamingCommandMatcher:
    """Matches commands incrementally as words arrive.

    Uses multi-path evolutionary matching: maintains multiple competing matcher paths
    that track different possible command interpretations. Weak paths are pruned,
    strong paths survive, and the fittest (first to complete) wins.

    Handles Riva's changing interim results by updating affected paths when words change.
    """

    def __init__(self, filler_config):
        """Initialize the command matcher.

        Args:
            filler_config (dict): Filler word configuration
        """
        self.patterns = []  # List of CommandPattern objects
        self.filler_config = filler_config
        self.base_max_fillers = filler_config.get('base_max', 4)

        # Global filler budget that persists across command matches
        # Winner's local budget syncs here after command execution
        self.global_budget = self.base_max_fillers

        # Cache of all expanded sequences for faster matching
        self.all_sequences = []  # Will be populated when patterns are registered

        # Multi-path matching state
        self.active_paths = []  # List of MatcherPath objects competing
        self.next_path_id = 0  # For generating unique path IDs

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
        """Process a new word from STT using multi-path matching.

        Creates a new matcher path starting with this word, and tries adding
        the word to all existing paths. Prunes weak paths and checks for completion.

        Args:
            word (str): The word to process

        Returns:
            dict or None: Match result if complete command matched, else None
        """
        word = word.lower().strip()
        if not word:
            return None

        LOG.info(f"Processing word: '{word}' | Active paths: {len(self.active_paths)} | Global budget: {self.global_budget}")

        # Track if word was accepted by any path
        word_accepted = False

        # Try adding word to all existing paths
        for path in self.active_paths:
            if path.try_add_word(word):
                word_accepted = True

        # Check if word is valid as a first word (start of new path)
        # Get valid next words for an empty path (i.e., valid first words)
        valid_first_words = set()
        for seq_info in self.all_sequences:
            if len(seq_info['sequence']) > 0:
                first_item = seq_info['sequence'][0]
                if 'word' in first_item:
                    valid_first_words.add(first_item['word'])
                elif 'entity' in first_item:
                    valid_first_words.add('<ENTITY>')

        LOG.info(f"  Valid first words count: {len(valid_first_words)}, word '{word}' valid: {word in valid_first_words}")

        # If word is valid as first word, create new path
        if word in valid_first_words or '<ENTITY>' in valid_first_words:
            new_path = MatcherPath(self.next_path_id, self.global_budget, self.all_sequences, self.filler_config)
            self.next_path_id += 1
            new_path.try_add_word(word)
            self.active_paths.append(new_path)
            word_accepted = True
            LOG.info(f"  ✓ Created new path {new_path.path_id} starting with '{word}'")

        # If word wasn't accepted anywhere, it's a true filler - decrease global budget
        if not word_accepted:
            self.global_budget = max(0, self.global_budget - 1)
            LOG.info(f"  ✗ True filler '{word}'. Global budget now: {self.global_budget}")

        # Prune unviable paths
        before_count = len(self.active_paths)
        self.active_paths = [p for p in self.active_paths if p.is_viable()]
        pruned_count = before_count - len(self.active_paths)

        if pruned_count > 0:
            LOG.debug(f"  Pruned {pruned_count} weak paths, {len(self.active_paths)} remain")

        # Log top paths for debugging
        if self.active_paths:
            top_paths = sorted(self.active_paths, key=lambda p: p.fitness_score, reverse=True)[:3]
            for path in top_paths:
                LOG.debug(f"  Path {path.path_id}: {path.matched_words} (fitness={path.fitness_score}, budget={path.local_budget})")

        # Check for complete matches - fittest wins
        for path in sorted(self.active_paths, key=lambda p: p.fitness_score, reverse=True):
            match = path.check_completion()
            if match:
                LOG.info(f"  ✓ COMPLETE MATCH from path {path.path_id}: {match}")

                # Winner! Sync local budget to global
                self.global_budget = path.local_budget
                LOG.debug(f"  Winner's budget {path.local_budget} synced to global")

                # Clear all paths for next command
                self.active_paths = []

                return match

        return None

    def should_timeout(self):
        """Check if global budget is exhausted."""
        # Timeout only when global budget hits zero
        # Having no active paths is normal (fillers, between commands, etc.)
        return self.global_budget <= 0

    def reset(self):
        """Reset matcher state after command execution.

        Clears all paths. Global budget was already synced by winner.
        Budget stays at winner's level for command chaining.
        """
        self.active_paths = []

        # Keep global budget (already synced from winner)
        # But ensure it's at least base_max
        self.global_budget = max(self.base_max_fillers, self.global_budget)

    def reset_session(self):
        """Reset matcher state for a new session (after wake word).

        Completely resets budget to base_max, unlike reset() which preserves
        accumulated budget for command chaining within a session.
        """
        self.active_paths = []
        self.global_budget = self.base_max_fillers


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
            print(f"    Active paths: {len(matcher.active_paths)}")
            print(f"    Global budget: {matcher.global_budget}")

        print(f"\nExpected: {test['expected']}")
        print(f"Got: {result['utterance'] if result else 'None'}")
        print(f"Status: {'✓ PASS' if (result and result['utterance'] == test['expected']) or (not result and test['expected'].startswith('Should fail')) else '✗ FAIL'}")


if __name__ == '__main__':
    test_matcher()
