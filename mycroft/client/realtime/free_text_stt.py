"""
Plug-and-play secondary STT for free-text (nondeterministic) mode.

Used when the realtime conformer matches a deterministic prefix ending at a
{free_text} entity — the recorded audio is sent here for batch inference
rather than streaming word-by-word recognition.

Backend is selected via config: realtime.free_text_stt.backend
V1 implementation: faster_whisper (subprocess — full VRAM released after each use)

To add a new backend:
  1. Subclass FreeTextStt and implement transcribe()
  2. Add a branch in load_free_text_stt() keyed by backend name
"""

import os
import sys
import subprocess
import tempfile
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


class FasterWhisperSubprocess(FreeTextStt):
    """Batch transcription using faster-whisper in a subprocess.

    Each transcribe() call spawns a fresh Python process that loads the model,
    transcribes, prints the result to stdout, and exits. The OS reclaims all
    VRAM on process exit, giving true VRAM recovery between uses.

    Config keys (under realtime.free_text_stt.faster_whisper):
      model:                        path to model directory or HuggingFace model name
      device:                       "cuda" or "cpu" (default: "cuda")
      compute_type:                 "float16", "int8", etc. (default: "float16")
      language:                     language code, e.g. "en" (default: "en")
      beam_size:                    beam search width (default: 5)
      no_speech_threshold:          prob below which segment is silence (default: 0.6)
      hallucination_silence_threshold: seconds of silence that triggers hallucination filter (default: None)
    """

    # Path to the worker script, relative to this file
    _WORKER = os.path.join(os.path.dirname(__file__), 'whisper_worker.py')

    def __init__(self, config: dict):
        self._config = config

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        """Transcribe audio by spawning a worker subprocess.

        Args:
            audio: float32 numpy array at any sample rate
            sample_rate: sample rate of the audio

        Returns:
            Transcribed text, or empty string on failure.
        """
        model_path  = self._config.get('model')
        device      = self._config.get('device', 'cuda')
        compute_type = self._config.get('compute_type', 'float16')
        language    = self._config.get('language', 'en')
        beam_size   = self._config.get('beam_size', 5)
        no_speech_threshold = self._config.get('no_speech_threshold', 0.6)
        hallucination_silence_threshold = self._config.get('hallucination_silence_threshold', None)

        tmp = tempfile.NamedTemporaryFile(suffix='.npy', delete=False)
        try:
            np.save(tmp.name, audio)
            tmp.close()

            cmd = [
                sys.executable, self._WORKER,
                tmp.name,
                str(sample_rate),
                model_path,
                device,
                compute_type,
                language,
                str(beam_size),
                str(no_speech_threshold),
            ]
            if hallucination_silence_threshold is not None:
                cmd.append(str(hallucination_silence_threshold))

            LOG.info(f"FasterWhisper subprocess starting (model={os.path.basename(model_path)})")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                LOG.error(f"FasterWhisper worker failed (rc={result.returncode}): {result.stderr.strip()}")
                return ''

            text = result.stdout.strip()
            LOG.info(f"FasterWhisper transcribed: '{text}'")
            return text

        except Exception as e:
            LOG.error(f"FasterWhisper transcription failed: {e}")
            return ''
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


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
        return FasterWhisperSubprocess(backend_cfg)

    else:
        LOG.error(f"Unknown free_text_stt backend: '{backend}' — free-text mode disabled")
        return None
