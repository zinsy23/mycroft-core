"""
Plug-and-play secondary STT for free-text (nondeterministic) mode.

Used when the realtime conformer matches a deterministic prefix ending at a
{free_text} entity — the recorded audio is sent here for batch inference
rather than streaming word-by-word recognition.

Backend is selected via config: realtime.free_text_stt.backend
V1 implementation: faster_whisper (batch, non-streaming)

To add a new backend:
  1. Subclass FreeTextStt and implement transcribe()
  2. Add a branch in load_free_text_stt() keyed by backend name
"""

import numpy as np
from mycroft.util.log import LOG


class FreeTextStt:
    """Base class for free-text batch STT backends."""

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        """Transcribe a complete audio recording to text.

        Args:
            audio: float32 numpy array, values in [-1.0, 1.0]
            sample_rate: sample rate of audio in Hz

        Returns:
            Transcribed text string, stripped of leading/trailing whitespace.
            Empty string if transcription failed or produced nothing.
        """
        raise NotImplementedError


class FasterWhisperBatch(FreeTextStt):
    """Batch transcription using faster-whisper (non-streaming).

    Model is loaded lazily on first transcribe() call to avoid startup cost
    when free-text commands are never used in a session.

    Config keys (under realtime.free_text_stt.faster_whisper):
      model:                        path to model directory or HuggingFace model name
      device:                       "cuda" or "cpu" (default: "cuda")
      compute_type:                 "float16", "int8", etc. (default: "float16")
      language:                     language code, e.g. "en" (default: "en")
      beam_size:                    beam search width (default: 5)
      no_speech_threshold:          prob below which segment is silence (default: 0.6, faster_whisper default)
      hallucination_silence_threshold: seconds of silence that triggers hallucination filter (default: None)
    """

    def __init__(self, config: dict):
        self._config = config
        self._model = None  # Loaded lazily

    def _load_model(self):
        """Load the WhisperModel on first use."""
        from faster_whisper import WhisperModel
        model_path = self._config.get('model')
        device = self._config.get('device', 'cuda')
        compute_type = self._config.get('compute_type', 'float16')

        LOG.info(f"Loading FasterWhisper batch model: {model_path} on {device}/{compute_type}")
        self._model = WhisperModel(model_path, device=device, compute_type=compute_type)
        LOG.info("FasterWhisper batch model loaded")

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        """Transcribe audio using faster-whisper batch inference.

        Args:
            audio: float32 numpy array at any sample rate (resampled to 16kHz internally by whisper)
            sample_rate: sample rate of the audio

        Returns:
            Transcribed text, or empty string on failure.
        """
        if self._model is None:
            self._load_model()

        language = self._config.get('language', 'en')

        try:
            # faster_whisper expects float32 at 16kHz — resample if needed
            if sample_rate != 16000:
                audio = _resample(audio, sample_rate, 16000)

            beam_size = self._config.get('beam_size', 5)
            no_speech_threshold = self._config.get('no_speech_threshold', 0.6)
            hallucination_silence_threshold = self._config.get('hallucination_silence_threshold', None)

            segments, info = self._model.transcribe(
                audio,
                language=language,
                beam_size=beam_size,
                vad_filter=True,
                no_speech_threshold=no_speech_threshold,
                hallucination_silence_threshold=hallucination_silence_threshold,
            )
            text = ' '.join(seg.text for seg in segments).strip()
            LOG.info(f"FasterWhisper transcribed: '{text}' (lang={info.language}, prob={info.language_probability:.2f})")
            return text

        except Exception as e:
            LOG.error(f"FasterWhisper transcription failed: {e}")
            return ''


def _resample(audio: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
    """Simple linear resampling. For production accuracy, use librosa or soxr."""
    if orig_rate == target_rate:
        return audio
    target_len = int(len(audio) * target_rate / orig_rate)
    return np.interp(
        np.linspace(0, len(audio) - 1, target_len),
        np.arange(len(audio)),
        audio
    ).astype(np.float32)


def load_free_text_stt(realtime_config: dict):
    """Factory: load the configured free-text STT backend.

    Args:
        realtime_config: the full realtime config dict

    Returns:
        FreeTextStt instance, or None if disabled/not configured.
    """
    cfg = realtime_config.get('free_text_stt', {})

    if not cfg.get('enabled', False):
        LOG.info("free_text_stt disabled in config")
        return None

    backend = cfg.get('backend', 'faster_whisper')

    if backend == 'faster_whisper':
        backend_cfg = cfg.get('faster_whisper', {})
        if not backend_cfg.get('model'):
            LOG.error("free_text_stt.faster_whisper.model not configured — free-text mode disabled")
            return None
        return FasterWhisperBatch(backend_cfg)

    else:
        LOG.error(f"Unknown free_text_stt backend: '{backend}' — free-text mode disabled")
        return None
