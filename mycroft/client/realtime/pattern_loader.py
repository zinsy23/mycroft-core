"""
Pattern loading utilities for realtime intent matching.

This module is shared between the realtime service and test scripts
to ensure identical pattern loading behavior.

Handles three pattern types:
  1. Normal deterministic patterns — expanded from entity files, passed to matcher as-is
  2. Free-text trigger patterns — patterns containing {free_text} entity; the prefix
     before {free_text} is registered as a deterministic sequence tagged __FREE_TEXT__:intent
  3. Repeat patterns — synthesized from the quantifiers file and repeat config;
     registered as sequences tagged __REPEAT__:N
"""

import os
import re
import itertools
from mycroft.util.log import LOG

# Tag prefixes used in intent names to signal mode switches
FREE_TEXT_TAG = '__FREE_TEXT__:'
REPEAT_TAG = '__REPEAT__:'
MANAGE_TAG = '__MANAGE__:'
QA_TAG = '__QA__:'


def build_management_patterns(command_groups, stt_manage_config=None):
    """Build enable/disable management patterns from command_groups config.

    Returns dict of {tagged_intent: [pattern_line]} for all combinations.
    These are registered into the protected pattern layer (always active).

    Args:
        command_groups: list of {'name': str, 'skill_prefix': str} dicts
        stt_manage_config: optional dict with 'load_phrases' and 'unload_phrases'
            lists for secondary STT load/unload voice commands

    Returns:
        dict mapping tagged intent names to single-element pattern lists
    """
    patterns = {}
    group_names = [g['name'] for g in command_groups]

    for action in ('enable', 'disable'):
        # "enable commands" / "disable commands" — global mute/unmute
        tagged = f'{MANAGE_TAG}{action}:commands'
        patterns[tagged] = [f'{action} commands']

        # "enable search" / "disable search" etc — per-skill
        for name in group_names:
            tagged = f'{MANAGE_TAG}{action}:{name}'
            patterns[tagged] = [f'{action} {name}']

    # Secondary STT load/unload commands
    if stt_manage_config and stt_manage_config.get('enabled', True):
        for phrase in stt_manage_config.get('load_phrases', []):
            tagged = f'{MANAGE_TAG}stt:load'
            patterns.setdefault(tagged, []).append(phrase)
        for phrase in stt_manage_config.get('unload_phrases', []):
            tagged = f'{MANAGE_TAG}stt:unload'
            patterns.setdefault(tagged, []).append(phrase)

    return patterns


def build_audio_wake_patterns(wake_phrases):
    """Build audio:wake patterns from a list of trigger phrases (config: audio_wake.phrases)."""
    if not wake_phrases:
        return {}
    return {f'{MANAGE_TAG}audio:wake': list(wake_phrases)}


def load_skill_config(skill_path):
    """Load SKILL_CONFIG from a skill's __init__.py if it exists.

    Returns:
        dict: Skill config or empty dict if not found
    """
    import importlib.util
    try:
        init_path = os.path.join(skill_path, '__init__.py')
        if not os.path.exists(init_path):
            return {}
        spec = importlib.util.spec_from_file_location("temp_skill_cfg", init_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, 'SKILL_CONFIG', {})
    except Exception as e:
        LOG.debug(f"Could not load SKILL_CONFIG from {skill_path}: {e}")
        return {}


def load_entity_expansions(skill_path):
    """Load ENTITY_EXPANSIONS from a skill's __init__.py if it exists.

    Args:
        skill_path: Path to skill directory

    Returns:
        dict: Entity expansions or empty dict if not found
    """
    import importlib.util

    try:
        init_path = os.path.join(skill_path, '__init__.py')
        if not os.path.exists(init_path):
            return {}

        spec = importlib.util.spec_from_file_location("temp_skill", init_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        return getattr(module, 'ENTITY_EXPANSIONS', {})
    except Exception as e:
        LOG.debug(f"Could not load ENTITY_EXPANSIONS from {skill_path}: {e}")
        return {}


def expand_pattern_entities(pattern, entity_expansions):
    """Expand {entity} placeholders in pattern with values from files.

    Creates separate pattern lines for each entity value, matching how Padatious
    expands patterns. This allows CommandPattern._expand_pattern() to handle
    the remaining optional groups correctly.

    {free_text} (or any entity with no expansion defined) is left as-is —
    the caller detects it and handles it separately.

    Args:
        pattern: Pattern string like "color {color}" or "[open|close] {main}"
        entity_expansions: Dict mapping entity names to their config

    Returns:
        list: Multiple pattern lines, one for each entity value combination
              Example: "[open|close] {main}" with main=[gpt,terminal] returns:
                - "[open|close] gpt"
                - "[open|close] terminal"
    """
    # Find all {entity} placeholders
    entities = re.findall(r'\{(\w+)\}', pattern)
    if not entities:
        return [pattern]

    # For each entity, load values from files
    entity_values = {}
    for entity_name in entities:
        if entity_name not in entity_expansions:
            # No expansion defined — leave placeholder as-is (e.g. {free_text})
            continue

        config = entity_expansions[entity_name]
        files = config.get('files', [])
        format_type = config.get('format', 'csv')
        column = config.get('column', 0)

        values = list(config.get('values', []))
        for file_path in files:
            try:
                with open(file_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue

                        if format_type == 'csv':
                            if ';' in line:
                                parts = line.split(';')
                            else:
                                parts = line.split(',')
                            if len(parts) > column:
                                values.append(parts[column].strip())
                        else:
                            values.append(line)
            except Exception as e:
                LOG.warning(f"Failed to load entity file {file_path}: {e}")

        if values:
            entity_values[entity_name] = values

    # If no entities were expanded, return original (with any {free_text} still in it)
    if not entity_values:
        return [pattern]

    # Generate Cartesian product of all expanded entity values
    entity_order = entities
    value_lists = [entity_values.get(name, [f'{{{name}}}']) for name in entity_order]

    expanded_patterns = []
    for combination in itertools.product(*value_lists):
        expanded = pattern
        for entity_name, value in zip(entity_order, combination):
            placeholder = f'{{{entity_name}}}'
            expanded = expanded.replace(placeholder, value)
        expanded_patterns.append(expanded)

    return expanded_patterns


def _split_free_text_pattern(pattern, free_text_entity):
    """Split a pattern at its {free_text} placeholder.

    Returns the prefix portion (everything before {free_text}) as a string,
    or None if the pattern contains no {free_text} entity.

    The prefix may contain optional groups like [for|] which the matcher
    will expand normally. Anything after {free_text} is discarded.

    Examples:
        "search google [for] {free_text}"  -> "search google [for]"
        "search google {free_text} extra"  -> "search google"  (trailing ignored)
        "type {free_text}"                 -> "type"
        "play video"                       -> None
    """
    placeholder = f'{{{free_text_entity}}}'
    if placeholder not in pattern:
        return None

    idx = pattern.index(placeholder)
    prefix = pattern[:idx].strip()
    return prefix if prefix else None


def _split_qa_pattern(pattern, entity_expansions):
    """Split a pattern at its {qa_text} placeholder.

    Returns (prefix, qa_config) if a qa_mode entity is found, else (None, None).
    prefix is the text before {qa_text}, qa_config is the ENTITY_EXPANSIONS entry.
    """
    for entity_name, config in entity_expansions.items():
        if config.get('type') != 'qa_mode':
            continue
        placeholder = f'{{{entity_name}}}'
        if placeholder not in pattern:
            continue
        idx = pattern.index(placeholder)
        prefix = pattern[:idx].strip()
        return (prefix if prefix else None), config
    return None, None


def load_quantifier_patterns(repeat_config):
    """Build synthetic repeat patterns from the quantifiers file and repeat config.

    Produces two sets of pattern strings tagged for the matcher:
      - Prefixed: "[repeat|redo] [that|] twice"  -> intent __REPEAT__:2
      - Bare (window-only): "twice"               -> intent __REPEAT__:2

    Multi-word quantifiers ("three times") become multi-word pattern sequences.
    Only the first token of each quantifier phrase is added to the transient
    valid-first-word set at runtime (handled by realtime_loop).

    Args:
        repeat_config: dict from realtime.repeat config block

    Returns:
        dict: intent_name -> list of pattern strings, where intent_name is
              __REPEAT__:N for integer N.
              Returns empty dict if repeat disabled or no quantifier file.
    """
    if not repeat_config.get('enabled', False):
        return {}

    quantifier_file = repeat_config.get('quantifier_file')
    if not quantifier_file or not os.path.exists(quantifier_file):
        LOG.warning(f"Repeat quantifier_file not found: {quantifier_file}")
        return {}

    prefix_words = repeat_config.get('prefix_words', ['repeat', 'redo', 'again'])
    suffix_words = repeat_config.get('suffix_words', ['that'])
    require_prefix = repeat_config.get('require_prefix', False)

    # Load quantifier phrase -> N mappings
    # Format: "phrase,N"  e.g. "twice,2" or "three times,3"
    quantifiers = {}  # phrase (str) -> N (int)
    try:
        with open(quantifier_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(',')
                if len(parts) >= 2:
                    phrase = parts[0].strip()
                    try:
                        n = int(parts[1].strip())
                        quantifiers[phrase] = n
                    except ValueError:
                        LOG.warning(f"Invalid quantifier line (non-integer N): '{line}'")
    except Exception as e:
        LOG.warning(f"Failed to load quantifier file {quantifier_file}: {e}")
        return {}

    if not quantifiers:
        return {}

    # Build prefix group string e.g. "[repeat|redo|again]"
    prefix_group = '[' + '|'.join(prefix_words) + ']'
    # Build optional suffix group e.g. "[that|]"
    if suffix_words:
        suffix_group = '[' + '|'.join(suffix_words) + '|]'
    else:
        suffix_group = None

    patterns_by_intent = {}

    for phrase, n in quantifiers.items():
        intent_name = f'{REPEAT_TAG}{n}'
        if intent_name not in patterns_by_intent:
            patterns_by_intent[intent_name] = []

        # Prefixed pattern: "[repeat|redo|again] [that|] three times"
        parts = [prefix_group]
        if suffix_group:
            parts.append(suffix_group)
        parts.append(phrase)
        prefixed = ' '.join(parts)
        patterns_by_intent[intent_name].append(prefixed)

        # Bare pattern (no prefix): "three times" — only valid within window
        # Tagged differently so realtime_loop knows it needs window check
        if not require_prefix:
            bare_intent = f'{REPEAT_TAG}{n}:bare'
            if bare_intent not in patterns_by_intent:
                patterns_by_intent[bare_intent] = []
            patterns_by_intent[bare_intent].append(phrase)

    LOG.info(f"Loaded {len(quantifiers)} repeat quantifiers → "
             f"{len(patterns_by_intent)} repeat intent variants")
    return patterns_by_intent


def load_all_intent_patterns(skills_dir, verbose=False, realtime_config=None):
    """Load all intent patterns from Mycroft skills directory.

    Also loads repeat patterns from config if repeat is enabled.

    Args:
        skills_dir: Path to skills directory
        verbose: If True, print loading progress
        realtime_config: Full realtime config dict (for free_text entity name
                         and repeat config). If None, uses defaults.

    Returns:
        tuple: (deterministic_patterns, free_text_triggers, repeat_patterns, qa_triggers)
          - deterministic_patterns: dict intent_name -> [pattern_str, ...]
            Normal patterns for StreamingCommandMatcher
          - free_text_triggers: dict intent_name -> [prefix_pattern_str, ...]
            Prefix-only patterns tagged __FREE_TEXT__:intent, registered in
            matcher; when matched, realtime_loop switches to free-text mode
          - repeat_patterns: dict intent_name -> [pattern_str, ...]
            Synthesized repeat patterns tagged __REPEAT__:N, registered in
            matcher with transient first-word handling
          - qa_triggers: dict intent_name -> {'prefixes': [...], 'config': {...}}
            Prefix-only patterns tagged __QA__:intent; when matched, realtime_loop
            switches to QA mode using the bundled qa_config from ENTITY_EXPANSIONS
    """
    if realtime_config is None:
        realtime_config = {}

    free_text_entity = (realtime_config
                        .get('free_text', {})
                        .get('entity_name', 'free_text'))

    if verbose:
        print(f"Loading intent patterns from: {skills_dir}")
        print(f"Free-text entity name: {{{free_text_entity}}}")

    deterministic_patterns = {}
    free_text_triggers = {}
    qa_triggers = {}
    total_deterministic = 0
    total_free_text = 0
    total_qa = 0

    # Walk all .intent files
    for root, dirs, files in os.walk(skills_dir):
        for file in files:
            if not file.endswith('.intent'):
                continue

            file_path = os.path.join(root, file)
            intent_name = file[:-7]  # Remove .intent

            # Skill path: /opt/mycroft/skills/skill-name/locale/en-us/file.intent
            skill_path = os.path.dirname(os.path.dirname(os.path.dirname(file_path)))

            try:
                entity_expansions = load_entity_expansions(skill_path)

                with open(file_path, 'r') as f:
                    pattern_lines = [line.strip() for line in f.readlines() if line.strip()]

                det_lines = []
                ft_lines = []

                for pattern in pattern_lines:
                    # First expand any normal {entity} placeholders (not {free_text})
                    expanded = expand_pattern_entities(pattern, entity_expansions)

                    qa_lines = []
                qa_config_for_intent = None

                for exp_pattern in expanded:
                        # Check for QA entity first
                        qa_prefix, qa_config = _split_qa_pattern(exp_pattern, entity_expansions)
                        if qa_prefix is not None:
                            qa_lines.append(qa_prefix)
                            qa_config_for_intent = qa_config
                            continue

                        prefix = _split_free_text_pattern(exp_pattern, free_text_entity)

                        if prefix is not None:
                            # This pattern has a {free_text} trigger
                            ft_lines.append(prefix)
                        else:
                            # Normal deterministic pattern
                            det_lines.append(exp_pattern)

                if det_lines:
                    deterministic_patterns[intent_name] = det_lines
                    total_deterministic += len(det_lines)

                if ft_lines:
                    tagged = f'{FREE_TEXT_TAG}{intent_name}'
                    free_text_triggers[tagged] = ft_lines
                    total_free_text += len(ft_lines)

                if qa_lines:
                    tagged = f'{QA_TAG}{intent_name}'
                    qa_triggers[tagged] = {
                        'prefixes': qa_lines,
                        'config': qa_config_for_intent or {},
                    }
                    total_qa += len(qa_lines)

                if verbose:
                    parts = []
                    if det_lines:
                        parts.append(f"{len(det_lines)} deterministic")
                    if ft_lines:
                        parts.append(f"{len(ft_lines)} free-text triggers")
                    if qa_lines:
                        parts.append(f"{len(qa_lines)} QA triggers")
                    if parts:
                        print(f"  {intent_name}: {' + '.join(parts)}")

            except Exception as e:
                if verbose:
                    print(f"  Warning: Failed to load {intent_name}: {e}")
                LOG.warning(f"Failed to load intent {intent_name} from {file_path}: {e}")

    # Load repeat patterns from config
    repeat_config = realtime_config.get('repeat', {})
    repeat_patterns = load_quantifier_patterns(repeat_config)

    if verbose:
        print(f"\nLoaded {total_deterministic} deterministic patterns across "
              f"{len(deterministic_patterns)} intents")
        print(f"Loaded {total_free_text} free-text trigger prefixes across "
              f"{len(free_text_triggers)} intents")
        if total_qa:
            print(f"Loaded {total_qa} QA trigger prefixes across "
                  f"{len(qa_triggers)} intents")
        if repeat_patterns:
            total_repeat = sum(len(v) for v in repeat_patterns.values())
            print(f"Loaded {total_repeat} repeat patterns across "
                  f"{len(repeat_patterns)} quantifier variants")

    return deterministic_patterns, free_text_triggers, repeat_patterns, qa_triggers
