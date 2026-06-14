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

import io
import os
import sys
import subprocess
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

    Supports early spawn: call spawn() when the trigger fires so the model loads
    in parallel with VAD recording, then call transcribe() once audio is ready.
    The worker signals READY on stdout when the model is loaded and waiting for
    audio on stdin. transcribe() writes the audio and collects the result.

    Each transcribe() call ends with the subprocess exiting, so VRAM is fully
    released between uses.

    Config keys (under realtime.free_text_stt.faster_whisper):
      model:                        path to model directory or HuggingFace model name
      device:                       "cuda" or "cpu" (default: "cuda")
      compute_type:                 "float16", "int8", etc. (default: "float16")
      language:                     language code, e.g. "en" (default: "en")
      beam_size:                    beam search width (default: 5)
      no_speech_threshold:          prob below which segment is silence (default: 0.6)
      hallucination_silence_threshold: seconds of silence that triggers hallucination filter (default: None)
    """

    _WORKER = os.path.join(os.path.dirname(__file__), 'whisper_worker.py')

    def __init__(self, config: dict):
        self._config = config
        self._proc = None   # pre-spawned subprocess, or None

    def _make_cmd(self, sample_rate: int) -> list:
        model_path  = self._config.get('model')
        device      = self._config.get('device', 'cuda')
        compute_type = self._config.get('compute_type', 'float16')
        language    = self._config.get('language', 'en')
        beam_size   = self._config.get('beam_size', 5)
        no_speech_threshold = self._config.get('no_speech_threshold', 0.6)
        hallucination_silence_threshold = self._config.get('hallucination_silence_threshold', None)
        cmd = [
            sys.executable, self._WORKER,
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
        return cmd

    def spawn(self, sample_rate: int):
        """Spawn the worker subprocess early so the model loads during VAD.

        The worker prints 'READY' once the model is loaded and waits for audio
        on stdin. Call transcribe() to deliver audio and collect the result.
        Safe to call even if a prior process is still running (it will be replaced).
        """
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

        model_name = os.path.basename(self._config.get('model', ''))
        LOG.info(f"FasterWhisper subprocess spawning early (model={model_name})")
        self._proc = subprocess.Popen(
            self._make_cmd(sample_rate),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        """Transcribe audio. Uses pre-spawned process if available, else spawns now.

        Waits for the worker READY signal, sends audio via stdin, reads result.
        """
        try:
            if self._proc is None:
                LOG.info("FasterWhisper subprocess spawning at transcribe time (no pre-spawn)")
                self._proc = subprocess.Popen(
                    self._make_cmd(sample_rate),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            proc = self._proc
            self._proc = None

            # Wait for READY signal (model loaded)
            ready_line = proc.stdout.readline().decode().strip()
            if ready_line != 'READY':
                LOG.error(f"FasterWhisper worker unexpected output before READY: '{ready_line}'")
                proc.kill()
                return ''

            # Send audio as npy bytes on stdin, then close stdin to signal EOF
            buf = io.BytesIO()
            np.save(buf, audio)
            stdout_data, stderr_data = proc.communicate(input=buf.getvalue())

            stderr_text = stderr_data.decode().strip()
            if proc.returncode != 0:
                LOG.error(f"FasterWhisper worker failed (rc={proc.returncode}): {stderr_text}")
                return ''

            for line in stderr_text.splitlines():
                if line.startswith('TIMING'):
                    LOG.info(f"FasterWhisper {line}")
                elif line:
                    LOG.warning(f"FasterWhisper worker stderr: {line}")

            text = stdout_data.decode().strip()
            LOG.info(f"FasterWhisper transcribed: '{text}'")
            return text

        except Exception as e:
            LOG.error(f"FasterWhisper transcription failed: {e}")
            if self._proc is not None:
                try:
                    self._proc.kill()
                except Exception:
                    pass
                self._proc = None
            return ''


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
