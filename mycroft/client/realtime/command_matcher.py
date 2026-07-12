"""
Streaming Command Matcher for real-time intent matching.

Matches commands word-by-word as they arrive from Riva, allowing immediate
execution without waiting for phrase completion.

Uses multi-path evolutionary matching to handle Riva's changing interim results.
Multiple candidate paths compete, weak paths are pruned, and the fittest path wins.
"""

import re
from mycroft.util.log import LOG
from mycroft.client.realtime.number_parser import (
    NUMBER_WORDS, SIGN_WORDS, CALC_WORDS, words_to_int, words_to_calc,
    format_calc_result,
)

# Entity names that get greedy multi-word number capture
_NUMBER_ENTITY_NAMES = frozenset({'number', 'signed_number'})
# Entity names that get greedy calc expression capture
_CALC_ENTITY_NAMES = frozenset({'number_calc'})


def _apply_calc_rounding(calc: dict, cfg: dict) -> int | float:
    """Apply rounding config to a words_to_calc result dict for entity dispatch.

    cfg keys (all optional):
      'decimal_places':               global default (default 2)
      'decimal_places_root':          override for root results
      'decimal_places_trig':          override for trig results
      'decimal_places_inverse_trig':  override for inverse trig results
      'decimal_places_divide':        override for division results
      'decimal_places_multiply':      override for multiplication results
      'decimal_places_abs':           override for absolute value results
      'decimal_places_power':         override for power/exponent results
      'decimal_places_constant':      override for expressions involving constants
    """
    places = cfg.get('decimal_places', 2)
    overrides = {}
    _op_cfg_keys = {
        'decimal_places_root':          'root',
        'decimal_places_trig':          'trig',
        'decimal_places_inverse_trig':  'inverse_trig',
        'decimal_places_divide':        'divide',
        'decimal_places_multiply':      'multiply',
        'decimal_places_abs':           'abs',
        'decimal_places_power':         'power',
        'decimal_places_constant':      'constant',
        'decimal_places_log':           'log',
    }
    for cfg_key, op_name in _op_cfg_keys.items():
        val = cfg.get(cfg_key)
        if val is not None:
            overrides[op_name] = val
    return format_calc_result(calc, decimal_places=places, overrides=overrides)


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
                if entity_name in _NUMBER_ENTITY_NAMES:
                    all_token_options.append([{'number_slot': entity_name}])
                elif entity_name in _CALC_ENTITY_NAMES:
                    all_token_options.append([{'calc_slot': entity_name}])
                else:
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

    def __init__(self, path_id, starting_budget, all_sequences, filler_config, start_position=0, calc_entity_configs=None):
        """Initialize a new matcher path.

        Args:
            path_id: Unique identifier for this path
            starting_budget: Initial budget inherited from global
            all_sequences: All command patterns to match against
            filler_config: Filler word configuration
            start_position: Transcript position where this path's first word came from.
                Used to prevent replay from feeding earlier-position words into this path,
                which would cause cross-positional matches (e.g. path started at pos 7
                grabbing 'full' from pos 5 during replay).
            calc_entity_configs: Per-entity config for calc slots, e.g.
                {'number_calc': {'decimal_places': 2, 'decimal_places_root': 4}}
        """
        self.path_id = path_id
        self.matched_words = []
        self.local_budget = starting_budget
        self.consecutive_fillers = 0
        self.all_sequences = all_sequences
        self.filler_config = filler_config
        self.base_max_fillers = filler_config.get('base_max', 4)
        self.calc_entity_configs = calc_entity_configs or {}
        self.fitness_score = 0  # Number of valid words matched
        self.start_position = start_position  # Transcript position of first word
        # Greedy number accumulation
        self._number_buf = []
        self._number_slot_name = None
        # Greedy calc expression accumulation
        self._calc_buf = []
        self._calc_slot_name = None
        # True if a slot was active at any point — defers completion to FINAL only
        self._had_slot = False
        # True once slot closed via a post-entity pattern word — INTERIM allowed from here
        self._slot_closed_by_word = False

    def clear_slot_state(self):
        """Reset open slot buffers so replay re-enters slots cleanly."""
        self._number_slot_name = None
        self._number_buf = []
        self._calc_slot_name = None
        self._calc_buf = []
        self._had_slot = False
        self._slot_closed_by_word = False

    def try_add_word(self, word, stream_type="FINAL"):
        """Try to add a word to this path.

        Args:
            word: The word to try adding
            stream_type: "INTERIM" or "FINAL" - determines if filler impacts local budget

        Returns:
            bool: True if word was valid and added, False if it's a filler
        """
        word = word.lower().strip()

        # ── greedy number slot ────────────────────────────────────────────────
        if self._number_slot_name is not None:
            valid_for_slot = NUMBER_WORDS
            if self._number_slot_name == 'signed_number' and not self._number_buf:
                valid_for_slot = SIGN_WORDS | NUMBER_WORDS
            if word in valid_for_slot:
                # Still inside entity — accumulate
                self._number_buf.append(word)
                self.consecutive_fillers = 0
                self.fitness_score += 1
                # First slot word already incremented budget on slot entry — skip
                if len(self._number_buf) > 1:
                    self.local_budget += self.filler_config.get('increment_per_word', 1)
                return True
            # Not a slot word — check if it's a valid post-entity pattern word.
            # Only attempt to close if buffer holds a valid complete number.
            # Everything else is a filler — slot stays open, same as normal realtime.
            phrase = ' '.join(self._number_buf)
            closed = False
            if phrase and words_to_int(phrase, concat=True) is not None:
                post_words = self._get_post_slot_words('__number__:', phrase)
                if word in post_words:
                    self.matched_words.append(f'__number__:{phrase}')
                    self._number_slot_name = None
                    self._number_buf = []
                    self._slot_closed_by_word = True
                    closed = True
            if not closed:
                self.consecutive_fillers += 1
                if stream_type == "FINAL":
                    self.local_budget = max(0, self.local_budget - 1)
                return False

        # ── greedy calc slot ──────────────────────────────────────────────────
        if self._calc_slot_name is not None:
            if word in CALC_WORDS:
                # Still inside entity — accumulate
                self._calc_buf.append(word)
                self.consecutive_fillers = 0
                self.fitness_score += 1
                # First slot word already incremented budget on slot entry — skip
                if len(self._calc_buf) > 1:
                    self.local_budget += self.filler_config.get('increment_per_word', 1)
                return True
            # Not a slot word — check if it's a valid post-entity pattern word.
            # Only attempt to close if buffer holds a valid complete expression.
            # Everything else is a filler — slot stays open, same as normal realtime.
            phrase = ' '.join(self._calc_buf)
            closed = False
            if phrase and words_to_calc(phrase, concat=True) is not None:
                post_words = self._get_post_slot_words('__calc__:', phrase)
                if word in post_words:
                    self.matched_words.append(f'__calc__:{phrase}')
                    self._calc_slot_name = None
                    self._calc_buf = []
                    self._slot_closed_by_word = True
                    closed = True
            if not closed:
                self.consecutive_fillers += 1
                if stream_type == "FINAL":
                    self.local_budget = max(0, self.local_budget - 1)
                return False

        # ── normal matching ───────────────────────────────────────────────────
        matching_sequences = self._get_matching_sequences()
        valid_next_words = self._get_valid_next_words(matching_sequences)

        # Check if the next position is a number slot
        if '<NUMBER_SLOT>' in valid_next_words:
            slot_name = self._get_next_number_slot_name(matching_sequences)
            valid_for_slot = NUMBER_WORDS
            if slot_name == 'signed_number':
                valid_for_slot = SIGN_WORDS | NUMBER_WORDS
            if word in valid_for_slot:
                self._number_slot_name = slot_name
                self._number_buf = [word]
                self._had_slot = True
                self.consecutive_fillers = 0
                self.fitness_score += 1
                if len(self.matched_words) > 0:
                    self.local_budget += self.filler_config.get('increment_per_word', 1)
                return True

        # Check if the next position is a calc slot
        if '<CALC_SLOT>' in valid_next_words:
            slot_name = self._get_next_calc_slot_name(matching_sequences)
            if word in CALC_WORDS:
                self._calc_slot_name = slot_name
                self._calc_buf = [word]
                self._had_slot = True
                self.consecutive_fillers = 0
                self.fitness_score += 1
                if len(self.matched_words) > 0:
                    self.local_budget += self.filler_config.get('increment_per_word', 1)
                return True

        is_valid = word in valid_next_words or '<ENTITY>' in valid_next_words

        if is_valid:
            self.matched_words.append(word)
            self.consecutive_fillers = 0
            self.fitness_score += 1
            if len(self.matched_words) == 1:
                pass
            else:
                increment = self.filler_config.get('increment_per_word', 1)
                self.local_budget += increment
            return True
        else:
            self.consecutive_fillers += 1
            if stream_type == "FINAL":
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
                elif 'number_slot' in seq_item:
                    if not word.startswith('__number__:'):
                        match = False
                        break
                elif 'calc_slot' in seq_item:
                    if not word.startswith('__calc__:'):
                        match = False
                        break
                # plain entity matches anything

            if match:
                matching.append(seq_info)

        return matching

    def _get_post_slot_words(self, slot_prefix, phrase):
        """Get valid next words immediately after a slot commits.

        Builds the tentative matched list with the committed slot word and returns
        what the pattern defines at the next position — same logic as _get_valid_next_words
        but at the post-slot position rather than the current matched_words position.
        """
        tentative = self.matched_words + [f'{slot_prefix}{phrase}']
        next_position = len(tentative)
        valid_words = set()
        for seq_info in self.all_sequences:
            sequence = seq_info['sequence']
            if len(sequence) <= next_position:
                continue
            ok = True
            for i, mw in enumerate(tentative):
                if i >= len(sequence):
                    ok = False
                    break
                seq_item = sequence[i]
                if 'word' in seq_item:
                    if seq_item['word'] != mw:
                        ok = False
                        break
                elif 'number_slot' in seq_item:
                    if not mw.startswith('__number__:'):
                        ok = False
                        break
                elif 'calc_slot' in seq_item:
                    if not mw.startswith('__calc__:'):
                        ok = False
                        break
            if ok:
                next_item = sequence[next_position]
                if 'word' in next_item:
                    valid_words.add(next_item['word'])
                elif 'entity' in next_item:
                    valid_words.add('<ENTITY>')
        return valid_words

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
            elif 'number_slot' in next_item:
                valid_words.add('<NUMBER_SLOT>')
            elif 'calc_slot' in next_item:
                valid_words.add('<CALC_SLOT>')

        return valid_words

    def _get_next_number_slot_name(self, matching_sequences):
        next_position = len(self.matched_words)
        for seq_info in matching_sequences:
            sequence = seq_info['sequence']
            if next_position < len(sequence):
                item = sequence[next_position]
                if 'number_slot' in item:
                    return item['number_slot']
        return 'number'

    def _get_next_calc_slot_name(self, matching_sequences):
        next_position = len(self.matched_words)
        for seq_info in matching_sequences:
            sequence = seq_info['sequence']
            if next_position < len(sequence):
                item = sequence[next_position]
                if 'calc_slot' in item:
                    return item['calc_slot']
        return 'number_calc'

    def check_completion(self):
        """Check if this path has completed a command.

        Returns:
            dict or None: Match result if complete, None otherwise
        """
        # If a slot is still open (FINAL arrived mid-accumulation), close it.
        matched = self.matched_words
        if self._number_slot_name is not None and self._number_buf:
            phrase = ' '.join(self._number_buf)
            if words_to_int(phrase) is not None:
                matched = matched + [f'__number__:{phrase}']
        elif self._calc_slot_name is not None and self._calc_buf:
            phrase = ' '.join(self._calc_buf)
            if words_to_calc(phrase) is not None:
                matched = matched + [f'__calc__:{phrase}']

        for seq_info in self.all_sequences:
            sequence = seq_info['sequence']

            if len(sequence) != len(matched):
                continue

            ok = True
            entities = {}

            for i, word in enumerate(matched):
                seq_item = sequence[i]

                if 'word' in seq_item:
                    if seq_item['word'] != word:
                        ok = False
                        break
                elif 'number_slot' in seq_item:
                    if not word.startswith('__number__:'):
                        ok = False
                        break
                    phrase = word[len('__number__:'):]
                    val = words_to_int(phrase)
                    if val is None:
                        ok = False
                        break
                    entities[seq_item['number_slot']] = val
                elif 'calc_slot' in seq_item:
                    if not word.startswith('__calc__:'):
                        ok = False
                        break
                    phrase = word[len('__calc__:'):]
                    calc = words_to_calc(phrase)
                    if calc is None:
                        ok = False
                        break
                    slot_name = seq_item['calc_slot']
                    cfg = self.calc_entity_configs.get(slot_name, {})
                    entities[slot_name] = _apply_calc_rounding(calc, cfg)
                    entities[f'{slot_name}_expr'] = calc['expr']
                    entities[f'{slot_name}_tokens'] = calc['tokens']
                elif 'entity' in seq_item:
                    entities[seq_item['entity']] = word

            if ok:
                # Commit the open slot if we used the extended matched list
                if matched is not self.matched_words:
                    self.matched_words = matched
                    self._number_slot_name = None
                    self._number_buf = []
                    self._calc_slot_name = None
                    self._calc_buf = []
                # Build clean utterance: replace internal slot markers with spoken words
                clean_words = [
                    w[len('__number__:'):] if w.startswith('__number__:') else
                    w[len('__calc__:'):] if w.startswith('__calc__:') else w
                    for w in self.matched_words
                ]
                return {
                    'intent': seq_info['intent'],
                    'utterance': ' '.join(clean_words),
                    'entities': entities,
                    'pattern': seq_info['pattern']
                }

        return None

    def is_viable(self):
        """Check if this path is still viable (not exhausted or dead-end).

        Returns:
            bool: True if path should continue, False if it should be pruned
        """
        # Budget exhausted - the only filler-based death condition.
        # consecutive_fillers is NOT used here: budget already accounts for fillers
        # per word, and paths must survive long pauses and filler words between
        # valid command words as long as budget remains.
        if self.local_budget <= 0:
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
        self.repeat_window_open = False  # Set by realtime_loop when window opens/closes

        # Per-entity config for calc slots: {entity_name: {decimal_places, ...}}
        self.calc_entity_configs = {}

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

                # DEBUG: Log patterns with "coding" to see how they're parsed
                if 'coding' in line.lower():
                    seq_words = [item.get('word', item.get('entity', '?')) for item in sequence]
                    LOG.info(f"DEBUG: Registered pattern with 'coding': {line}")
                    LOG.info(f"DEBUG: Sequence: {seq_words}")

        LOG.debug(f"Registered {len(pattern_lines)} patterns for {intent_name}")

    def _rebuild_all_sequences(self):
        """Rebuild all_sequences cache from current patterns list.

        Called after shared_patterns is repopulated (enable/disable commands).
        """
        self.all_sequences = []
        for pattern in self.patterns:
            for sequence in pattern.word_sequences:
                self.all_sequences.append({
                    'intent': pattern.intent_name,
                    'pattern': pattern.original_line,
                    'sequence': sequence
                })

    def add_word(self, word, stream_type="FINAL", transcript_position=None):
        """Process a new word from STT using multi-path matching.

        Creates a new matcher path starting with this word, and tries adding
        the word to all existing paths. Prunes weak paths and checks for completion.

        Args:
            word (str): The word to process
            stream_type (str): "INTERIM" or "FINAL" - determines budget impact
            transcript_position (int): Position of this word in the current transcript.
                Used to guard existing paths against receiving words from earlier positions
                during replay (prevents cross-positional matches like a path that started
                at position 7 consuming 'full' from position 5 during replay).

        Returns:
            dict or None: Match result if complete command matched, else None
        """
        word = word.lower().strip()
        if not word:
            return None

        LOG.debug(f"Processing word: '{word}' | Active paths: {len(self.active_paths)} | Global budget: {self.global_budget}")

        # Track if word was accepted by any path
        word_accepted = False

        # Try adding word to all existing paths.
        # Skip paths whose start_position is after the current transcript position -
        # those paths started later in the transcript and must not receive earlier words
        # during replay (would cause cross-positional matches).
        for path in self.active_paths:
            if transcript_position is not None and transcript_position < path.start_position:
                continue
            if path.try_add_word(word, stream_type=stream_type):
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

        LOG.debug(f"  Valid first words count: {len(valid_first_words)}, word '{word}' valid: {word in valid_first_words}")

        # If word is valid as first word, create new path with its transcript position
        if word in valid_first_words or '<ENTITY>' in valid_first_words:
            pos = transcript_position if transcript_position is not None else 0
            new_path = MatcherPath(self.next_path_id, self.global_budget, self.all_sequences, self.filler_config, start_position=pos, calc_entity_configs=self.calc_entity_configs)
            self.next_path_id += 1
            new_path.try_add_word(word, stream_type=stream_type)
            self.active_paths.append(new_path)
            word_accepted = True
            LOG.debug(f"  ✓ Created new path {new_path.path_id} starting with '{word}' at position {pos}")

        # INTERIM/FINAL Budget Split:
        # - INTERIM filler words do NOT impact global budget (optimistic matching)
        # - FINAL filler words DO impact global budget (authoritative - what Riva officially decided)
        # - Only decrease budget if word was rejected by competing paths (survival of the fittest)
        # - Without active paths, there's no competition, so budget shouldn't be impacted
        if not word_accepted and len(self.active_paths) > 0:
            if stream_type == "FINAL":
                self.global_budget = max(0, self.global_budget - 1)
                LOG.debug(f"  ✗ FINAL filler rejected by {len(self.active_paths)} paths. Global budget now: {self.global_budget}")
            else:
                LOG.debug(f"  ○ INTERIM filler (no budget impact): '{word}' rejected by {len(self.active_paths)} paths")
        elif not word_accepted and len(self.active_paths) == 0:
            if stream_type == "FINAL":
                LOG.debug(f"  ○ FINAL word '{word}' not valid (no active paths to compete)")
            else:
                LOG.debug(f"  ○ INTERIM word '{word}' not valid (no active paths to compete)")

        # Prune unviable paths
        before_count = len(self.active_paths)
        pruned_paths = [(p, p.is_viable()) for p in self.active_paths]
        self.active_paths = [p for p, viable in pruned_paths if viable]
        pruned_count = before_count - len(self.active_paths)

        if pruned_count > 0:
            LOG.debug(f"  ⚠️  Pruned {pruned_count} paths, {len(self.active_paths)} remain")
            for path, viable in pruned_paths:
                if not viable:
                    reason = "budget exhausted" if path.local_budget <= 0 else "unknown"
                    LOG.debug(f"     Path {path.path_id} pruned: {path.matched_words} - {reason}")

        # Log top paths for debugging
        if self.active_paths:
            top_paths = sorted(self.active_paths, key=lambda p: p.fitness_score, reverse=True)[:3]
            for path in top_paths:
                LOG.debug(f"  Path {path.path_id}: {path.matched_words} (fitness={path.fitness_score}, budget={path.local_budget})")

        # Check for complete matches - fittest wins.
        # Skip bare repeat matches when the window is closed — leaving paths active
        # so words like "times" can still feed into calc slots on the same stream.
        repeat_window_open = getattr(self, 'repeat_window_open', True)
        # Slot open → always FINAL only (still accumulating).
        # Slot closed by a valid post-slot pattern word → INTERIM allowed (the word
        # is defined in the locale after the entity, not a filler).
        # Slot not yet closed by a word → FINAL only.
        for path in sorted(self.active_paths, key=lambda p: p.fitness_score, reverse=True):
            slot_open = path._number_slot_name is not None or path._calc_slot_name is not None
            if slot_open:
                continue
            if path._had_slot and not path._slot_closed_by_word:
                continue
            match = path.check_completion()
            if match:
                # Don't fire bare repeat matches when the window is closed —
                # leave all paths active so other patterns (e.g. calc) can still match
                from mycroft.client.realtime.pattern_loader import REPEAT_TAG
                if (match['intent'].startswith(REPEAT_TAG) and
                        match['intent'].endswith(':bare') and
                        not repeat_window_open):
                    LOG.info(f"  ⏩ Bare repeat '{match['utterance']}' suppressed in matcher — window closed")
                    continue

                LOG.info(f"  ✓ COMPLETE MATCH from path {path.path_id}: {match}")

                # Winner! Sync local budget to global
                self.global_budget = path.local_budget
                LOG.debug(f"  Winner's budget {path.local_budget} synced to global")

                # Clear all paths for next command
                self.active_paths = []

                return match, True

        return None, word_accepted

    def close_slots_and_check(self):
        """Close any open slots on all active paths and check for completion.

        Mirrors what realtime_loop does at the FINAL stream boundary.
        Returns the first match found, or None.
        """
        for path in list(self.active_paths):
            if (path._number_slot_name is not None or path._calc_slot_name is not None
                    or (path._had_slot and not path._slot_closed_by_word)):
                match = path.check_completion()
                if match:
                    self.active_paths = []
                    return match
        return None

    def should_timeout(self):
        """Check if global budget is exhausted."""
        # Timeout only when global budget hits zero
        # Having no active paths is normal (fillers, between commands, etc.)
        return self.global_budget <= 0

    def prune_corrected_paths(self, changed_positions, new_words):
        """Prune paths whose starting word was corrected to a different valid first word.

        When Riva corrects a word at some position to a different valid command-starting
        word (e.g. 'and' → 'enter'), any path that started on the old word at that
        position should be killed — Riva is authoritatively saying it heard something
        different. This is distinct from correction to noise/garbage (e.g. 'play' → 'uh')
        where the path should survive because Riva is just being temporarily confused.

        Args:
            changed_positions: dict of {position: (old_word, new_word)} for positions
                where the transcript changed
            new_words: the new transcript word list
        """
        # Build set of valid first words once
        valid_first_words = set()
        for seq_info in self.all_sequences:
            if seq_info['sequence']:
                first = seq_info['sequence'][0]
                if 'word' in first:
                    valid_first_words.add(first['word'])

        before_count = len(self.active_paths)
        surviving = []
        for path in self.active_paths:
            kill = False
            if path.start_position in changed_positions:
                old_word, new_word = changed_positions[path.start_position]
                if (path.matched_words and
                        path.matched_words[0] == old_word and
                        new_word in valid_first_words and
                        new_word != old_word):
                    LOG.info(f"  ✂️  Pruning path {path.path_id} ({path.matched_words}): "
                             f"start word '{old_word}' corrected to valid command word '{new_word}' at position {path.start_position}")
                    kill = True
            if not kill:
                surviving.append(path)

        self.active_paths = surviving
        pruned = before_count - len(self.active_paths)
        if pruned > 0:
            LOG.info(f"  ✂️  Pruned {pruned} path(s) due to Riva correction to valid command word")

    def clear_slot_state_all_paths(self):
        """Clear open slot buffers on all active paths so transcript-change replay re-enters slots cleanly."""
        for path in self.active_paths:
            path.clear_slot_state()

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
