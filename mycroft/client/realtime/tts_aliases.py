"""TTS alias and transform system for the realtime skill pipeline.

Rewrites text right before it is sent to Riva TTS — the underlying data is
never modified, only the spoken form changes.

Two mechanisms:
  - Aliases: plain str.replace() substitutions, keyed by the string to replace.
  - Transforms: named callables (str) -> str, applied after aliases.

Configuration:
  Global  — mycroft.conf  realtime.tts_aliases  /  realtime.tts_transforms
  Skill   — SKILL_CONFIG  tts_aliases           /  tts_transforms
  Skill wins on same key.

Built-in transforms:
  "number_commas" — inserts thousands-separator commas into bare integers of
                    4+ digits: "11264" → "11,264". Decimals are left alone.

Custom transforms:
  Register a callable by name so config lists can reference it:

      from mycroft.client.realtime.tts_aliases import register_tts_transform

      def my_fn(text: str) -> str:
          return text.replace('Ms.', 'Miss')

      register_tts_transform('honorifics', my_fn)

      SKILL_CONFIG = {'tts_transforms': ['honorifics']}
"""

import re

# Transform registry: name → callable(str) -> str.
_TTS_TRANSFORMS: dict = {}


def _transform_number_commas(text: str) -> str:
    """Insert thousands-separator commas into bare integers of 4+ digits.

    "11264 items"  →  "11,264 items"
    "11264.5"      →  "11264.5"  (decimals left alone)
    """
    def _add_commas(m):
        return format(int(m.group(0)), ',')
    return re.sub(r'(?<![.\d])\d{4,}(?![.\d])', _add_commas, text)


_TTS_TRANSFORMS['number_commas'] = _transform_number_commas


def register_tts_transform(name: str, fn) -> None:
    """Register a custom TTS transform callable by name.

    Registering the same name twice overwrites the previous entry.
    """
    _TTS_TRANSFORMS[name] = fn


def apply_tts_aliases(text: str, global_aliases: dict = None,
                      skill_aliases: dict = None,
                      global_transforms: list = None,
                      skill_transforms: list = None) -> str:
    """Apply TTS string replacements and named transforms before Riva TTS.

    Order of operations:
      1. Global aliases (plain str.replace; skill alias wins on same key)
      2. Skill aliases (plain str.replace)
      3. Global transforms (named, e.g. "number_commas")
      4. Skill transforms (named; applied after global)

    Returns transformed text ready for TTS.
    """
    skill_aliases = skill_aliases or {}
    global_aliases = global_aliases or {}
    for old, new in global_aliases.items():
        if old not in skill_aliases:
            text = text.replace(old, new)
    for old, new in skill_aliases.items():
        text = text.replace(old, new)
    for name in (list(global_transforms or []) + list(skill_transforms or [])):
        fn = _TTS_TRANSFORMS.get(name)
        if fn:
            text = fn(text)
    return text
