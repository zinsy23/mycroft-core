"""Mixin for Mycroft skills that interact with the realtime STT service.

Provides:
  - _speak_blocking(text): synthesize via Riva TTS, play via paplay,
    automatically apply TTS aliases/transforms before synthesis.
  - apply_tts_aliases(text): apply TTS aliases/transforms (needed when
    overriding _speak_blocking).
  - register_tts_transform(name, fn): add a custom transform to the registry.
  - Pattern group management: realtime_enable_group / realtime_disable_group / etc.

Usage in a skill:

    from mycroft.skills.realtime_mixin import RealtimePatternMixin

    SKILL_CONFIG = {
        'repeat_exempt': True,
        'tts_aliases': {', ': ' and '},      # applied before Riva TTS
        'tts_transforms': ['number_commas'],  # named transforms (built-in or custom)
        'pattern_groups': {
            'my_session': ['next', 'previous', 'cancel'],
        },
    }

    class MySkill(RealtimePatternMixin, MycroftSkill):
        def initialize(self):
            self._skill_config = SKILL_CONFIG

        def handle_something(self, message):
            self._speak_blocking("Two plus two is four.")

Skills only need to set self._skill_config = SKILL_CONFIG in initialize() (for TTS
aliases/transforms and repeat_exempt). Riva config is read lazily from self.config_core.

TTS aliases and transforms live in mycroft/client/realtime/tts_aliases.py.
Global config: mycroft.conf realtime.tts_aliases / realtime.tts_transforms.
Skill config: SKILL_CONFIG['tts_aliases'] / SKILL_CONFIG['tts_transforms'].
Skill wins on conflict. All applied automatically in _speak_blocking.

Overriding _speak_blocking:

    Skills that need custom speak behavior (e.g. storing the process handle for
    interrupt support) can override _speak_blocking. Call apply_tts_aliases()
    at the top — it's a no-op when nothing is configured:

        def _speak_blocking(self, text):
            text = self.apply_tts_aliases(text)
            # ... custom synthesis ...

Pattern groups:

    Declared in SKILL_CONFIG['pattern_groups'], registered as disabled at
    startup, activated at runtime via realtime_enable_group(). The skill_prefix
    used is self.skill_id (skill directory name).

    Dynamic registration (patterns not known at startup):
        self.realtime_register_patterns('my_session', ['pattern one', 'two'])
        self.realtime_enable_group('my_session')
"""

import os
import subprocess
import tempfile
import wave

from mycroft.messagebus.message import Message
from mycroft.client.realtime.tts_aliases import (
    apply_tts_aliases,
    register_tts_transform,
)

# Re-export so skills can import from here if they prefer.
__all__ = ['RealtimePatternMixin', 'apply_tts_aliases', 'register_tts_transform']


class RealtimePatternMixin:
    """Mixin that adds Riva TTS speaking and realtime pattern group management."""

    # Skills set this in initialize() to wire SKILL_CONFIG into _speak_blocking.
    _skill_config: dict = None

    # Lazily populated from config_core on first _speak_blocking call.
    _riva_auth = None
    _riva_voice: str = None
    _riva_sample_rate: int = None

    def _init_riva(self):
        """Lazily initialise Riva auth/voice/rate from config_core."""
        if self._riva_auth is not None:
            return
        import riva.client
        riva_cfg = self.config_core.get('tts', {}).get('riva', {})
        self._riva_auth = riva.client.Auth(uri=riva_cfg.get('server_uri', 'localhost:50051'))
        self._riva_voice = riva_cfg.get('voice', 'English-US-RadTTSpp.Female.narrator')
        self._riva_sample_rate = riva_cfg.get('sample_rate', 44100)

    def apply_tts_aliases(self, text: str, skill_config: dict = None) -> str:
        """Apply TTS aliases and transforms before the text goes to Riva TTS.

        Args:
            text:         Raw text to speak.
            skill_config: SKILL_CONFIG dict. If omitted, uses self._skill_config.

        Returns:
            Transformed text with all aliases and transforms applied.
        """
        rt = self.config_core.get('realtime', {})
        skill_cfg = skill_config if skill_config is not None else (self._skill_config or {})
        return apply_tts_aliases(
            text,
            global_aliases=rt.get('tts_aliases', {}),
            skill_aliases=skill_cfg.get('tts_aliases', {}),
            global_transforms=rt.get('tts_transforms', []),
            skill_transforms=skill_cfg.get('tts_transforms', []),
        )

    @staticmethod
    def register_tts_transform(name: str, fn) -> None:
        """Register a custom TTS transform callable by name.

        Delegates to tts_aliases.register_tts_transform(); available as a
        method so skills can call self.register_tts_transform() in initialize().
        """
        register_tts_transform(name, fn)

    def _speak_blocking(self, text, on_play=None) -> float | None:
        """Synthesize text via Riva TTS and play it synchronously via paplay.

        TTS aliases and transforms are applied automatically before synthesis.
        Riva config is read lazily from self.config_core — no per-skill init needed.
        Set self._skill_config = SKILL_CONFIG in initialize() for TTS aliases.

        Args:
            text:    Text to speak.
            on_play: Optional callable(duration_secs) called right before playback
                     starts — use for notifications that should show while audio plays:

                         self._speak_blocking(text, on_play=lambda d: flash_text(label, d))

        Returns:
            Duration of the audio in seconds, or None on error.

        Skills that override this method are responsible for calling _init_riva()
        and apply_tts_aliases() at the top.
        """
        import riva.client.proto.riva_audio_pb2 as raudio
        from mycroft.tts.riva_tts import RivaSpeechSynthesisService

        self._init_riva()
        text = self.apply_tts_aliases(text)
        self.log.info(f"{self.__class__.__name__}: _speak_blocking text={text!r}")
        self.bus.emit(Message('mycroft.realtime.skill_reply', {'utterance': text}))
        try:
            tts_service = RivaSpeechSynthesisService(self._riva_auth, voice_name=self._riva_voice)
            resp = tts_service.synthesize(
                text,
                voice_name=self._riva_voice,
                language_code='en-US',
                encoding=raudio.AudioEncoding.LINEAR_PCM,
                sample_rate_hz=self._riva_sample_rate,
            )
            duration_secs = len(resp.audio) / 2 / self._riva_sample_rate
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                wav_path = f.name
            with wave.open(wav_path, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self._riva_sample_rate)
                wf.writeframes(resp.audio)
            try:
                if on_play is not None:
                    on_play(duration_secs)
                subprocess.Popen(['paplay', wav_path]).wait()
            finally:
                os.unlink(wav_path)
            return duration_secs
        except Exception as e:
            import traceback
            self.log.error(
                f"{self.__class__.__name__}: _speak_blocking FAILED: {e}\n"
                f"{traceback.format_exc()}"
            )
            return None

    def realtime_group_handler(self, group: str, handler):
        """Register a handler for a dynamic pattern group intent.

        Use in initialize() instead of @intent_handler for group intents:

            self.realtime_group_handler('matrix_session', self.handle_session_input)
        """
        self.add_event(f'{self.skill_id}:__group__:{group}', handler)

    def realtime_enable_group(self, group: str):
        """Enable a named pattern sub-group in the realtime service."""
        self.bus.emit(Message('realtime:skill_patterns', {
            'action': 'enable',
            'skill_prefix': self.skill_id,
            'group': group,
        }))

    def realtime_disable_group(self, group: str):
        """Disable a named pattern sub-group in the realtime service."""
        self.bus.emit(Message('realtime:skill_patterns', {
            'action': 'disable',
            'skill_prefix': self.skill_id,
            'group': group,
        }))

    def realtime_register_patterns(self, group: str, patterns: list,
                                   enabled: bool = False):
        """Dynamically register a new pattern sub-group at runtime."""
        self.bus.emit(Message('realtime:skill_patterns', {
            'action': 'register',
            'skill_prefix': self.skill_id,
            'group': group,
            'patterns': patterns,
            'enabled': enabled,
        }))

    def realtime_unregister_patterns(self, group: str):
        """Remove a dynamic pattern sub-group entirely from the realtime service."""
        self.bus.emit(Message('realtime:skill_patterns', {
            'action': 'unregister',
            'skill_prefix': self.skill_id,
            'group': group,
        }))
