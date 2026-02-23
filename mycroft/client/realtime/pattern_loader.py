"""
Pattern loading utilities for realtime intent matching.

This module is shared between the realtime service and test scripts
to ensure identical pattern loading behavior.
"""

import os
import itertools
from mycroft.util.log import LOG


def load_entity_expansions(skill_path):
    """Load ENTITY_EXPANSIONS from a skill's __init__.py if it exists.

    Args:
        skill_path: Path to skill directory

    Returns:
        dict: Entity expansions or empty dict if not found
    """
    import importlib.util
    import sys

    try:
        init_path = os.path.join(skill_path, '__init__.py')
        if not os.path.exists(init_path):
            return {}

        # Load module to get ENTITY_EXPANSIONS
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

    Args:
        pattern: Pattern string like "color {color}" or "[open|close] {main}"
        entity_expansions: Dict mapping entity names to their config

    Returns:
        list: Multiple pattern lines, one for each entity value combination
              Example: "[open|close] {main}" with main=[gpt,terminal] returns:
                - "[open|close] gpt"
                - "[open|close] terminal"
    """
    import re

    # Find all {entity} placeholders
    entities = re.findall(r'\{(\w+)\}', pattern)
    if not entities:
        return [pattern]

    # For each entity, load values from files
    entity_values = {}
    for entity_name in entities:
        if entity_name not in entity_expansions:
            # No expansion defined, keep as-is
            continue

        config = entity_expansions[entity_name]
        files = config.get('files', [])
        format_type = config.get('format', 'csv')
        column = config.get('column', 0)

        values = []
        for file_path in files:
            try:
                with open(file_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue

                        if format_type == 'csv':
                            # Try semicolon first, then comma
                            if ';' in line:
                                parts = line.split(';')
                            else:
                                parts = line.split(',')
                            if len(parts) > column:
                                values.append(parts[column].strip())
                        else:
                            # Simple format - one value per line
                            values.append(line)
            except Exception as e:
                LOG.warning(f"Failed to load entity file {file_path}: {e}")

        if values:
            entity_values[entity_name] = values

    # If no entities were expanded, return original
    if not entity_values:
        return [pattern]

    # Generate Cartesian product of all entity values
    # For each combination, create a separate pattern line
    expanded_patterns = []

    # Get entity names in order they appear in pattern
    entity_order = entities

    # Get value lists in same order
    value_lists = [entity_values.get(name, [name]) for name in entity_order]

    # Generate all combinations
    for combination in itertools.product(*value_lists):
        expanded = pattern
        for entity_name, value in zip(entity_order, combination):
            placeholder = f'{{{entity_name}}}'
            expanded = expanded.replace(placeholder, value)
        expanded_patterns.append(expanded)

    return expanded_patterns


def load_all_intent_patterns(skills_dir, verbose=False):
    """Load all intent patterns from Mycroft skills directory.

    Args:
        skills_dir: Path to skills directory
        verbose: If True, print loading progress

    Returns:
        dict: Mapping of intent_name -> list of expanded pattern strings
    """
    if verbose:
        print(f"Loading intent patterns from: {skills_dir}")

    all_patterns = {}
    total_expanded = 0

    # Find all .intent files
    for root, dirs, files in os.walk(skills_dir):
        for file in files:
            if not file.endswith('.intent'):
                continue

            file_path = os.path.join(root, file)
            intent_name = file[:-7]  # Remove .intent

            # Determine skill path from intent file path
            # Path format: /opt/mycroft/skills/skill-name/locale/en-us/file.intent
            skill_path = os.path.dirname(os.path.dirname(os.path.dirname(file_path)))

            try:
                # Load entity expansions from skill
                entity_expansions = load_entity_expansions(skill_path)

                # Read patterns
                with open(file_path, 'r') as f:
                    pattern_lines = [line.strip() for line in f.readlines() if line.strip()]

                # Expand patterns with entity values
                expanded_lines = []
                for pattern in pattern_lines:
                    expanded = expand_pattern_entities(pattern, entity_expansions)
                    expanded_lines.extend(expanded)

                all_patterns[intent_name] = expanded_lines
                total_expanded += len(expanded_lines)

                if verbose and entity_expansions:
                    print(f"  {intent_name}: {len(pattern_lines)} → {len(expanded_lines)} patterns")
                elif verbose:
                    print(f"  {intent_name}: {len(pattern_lines)} patterns")

            except Exception as e:
                if verbose:
                    print(f"  Warning: Failed to load {intent_name}: {e}")
                LOG.warning(f"Failed to load intent {intent_name} from {file_path}: {e}")

    if verbose:
        print(f"\nLoaded {len(all_patterns)} intents with {total_expanded} total patterns")

    return all_patterns
