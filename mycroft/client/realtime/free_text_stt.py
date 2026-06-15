"""
Plug-and-play secondary STT for free-text (nondeterministic) mode.

Used when the realtime conformer matches a deterministic prefix ending at a
{free_text} entity — the recorded audio is sent here for batch inference
rather than streaming word-by-word recognition.

Backend is selected via config: realtime.free_text_stt.backend

Two operating modes (controlled by realtime.free_text_stt.preload):
  preload=false (default): on-demand subprocess — spawned at trigger time so
      the model loads in parallel with VAD, process exits after transcription,
      VRAM fully released between uses.
  preload=true: persistent worker — model loaded once and kept in VRAM,
      zero load cost per transcription. load()/unload() control the worker
      lifetime and can be called at runtime (e.g. via voice command).

The worker subprocess uses ovos-stt-plugin-fasterwhisper internally, so all
model resolution, quantization, and inference settings come from the plugin.
VRAM is released automatically on subprocess exit (one-shot mode).

To add a new backend:
  1. Subclass FreeTextStt and implement transcribe(), load(), unload(), spawn()
  2. Add a branch in load_free_text_stt() keyed by backend name
"""

import io
import os
import struct
import sys
import subprocess
import numpy as np
from mycroft.util.log import LOG


class FreeTextStt:
    """Base class for free-text batch STT backends."""

    @property
    def is_loaded(self) -> bool:
        """True if the backend has a persistent worker loaded in VRAM."""
        return False

    def load(self, sample_rate: int):
        """Load the model persistently (no-op for on-demand backends)."""

    def unload(self):
        """Unload the persistent worker and free VRAM (no-op if not loaded)."""

    def spawn(self, sample_rate: int):
        """Pre-spawn for on-demand mode (no-op if persistent worker is loaded)."""

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        raise NotImplementedError


class FasterWhisperSubprocess(FreeTextStt):
    """Batch transcription via whisper_worker.py subprocess.

    The worker uses ovos-stt-plugin-fasterwhisper internally. Running in a
    subprocess means VRAM is fully released when the process exits (one-shot
    mode) — no residual allocations in the main Mycroft process.

    On-demand mode (preload=false):
      spawn() is called at trigger time — the worker loads the model in parallel
      with VAD recording. transcribe() waits for READY, sends audio, gets result,
      and the process exits (VRAM freed).

    Persistent mode (preload=true or after load() is called):
      load() starts a long-lived worker with --persistent. The worker stays alive
      between transcriptions, keeping the model in VRAM. transcribe() sends a
      length-prefixed audio job and reads the result without reloading the model.
      unload() sends a zero sentinel and waits for the process to exit.

    Config keys (under realtime.free_text_stt.faster_whisper):
      model:        model name ("medium.en") or HF repo id
      use_cuda:     true/false (default: true)
      compute_type: "int8", "float16", etc. (default: "int8")
      language:     language code (default: "en")
      beam_size:    beam search width (default: 5)
    """

    _WORKER = os.path.join(os.path.dirname(__file__), 'whisper_worker.py')

    def __init__(self, config: dict):
        self._config = config
        self._proc = None           # pre-spawned one-shot subprocess
        self._persistent = None     # long-lived persistent worker process
        self._persistent_ready = False  # True only after READY received

    def _make_cmd(self, persistent: bool = False) -> list:
        model        = self._config.get('model', 'medium.en')
        use_cuda     = str(self._config.get('use_cuda', True))
        compute_type = self._config.get('compute_type', 'int8')
        language     = self._config.get('language', 'en')
        beam_size    = str(self._config.get('beam_size', 5))
        cmd = [sys.executable, self._WORKER,
               model, use_cuda, compute_type, language, beam_size]
        if persistent:
            cmd.append('--persistent')
        return cmd

    # ── Persistent mode ───────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return (self._persistent is not None
                and self._persistent.poll() is None
                and self._persistent_ready)

    def load(self, sample_rate: int):
        if self._persistent is not None:
            if self._persistent_ready:
                LOG.info("FasterWhisper persistent worker already loaded")
            else:
                LOG.info("FasterWhisper persistent worker already loading — ignoring duplicate")
            return

        model_name = self._config.get('model', 'medium.en')
        LOG.info(f"FasterWhisper loading persistent worker ({model_name})")
        self._persistent_ready = False
        self._persistent = subprocess.Popen(
            self._make_cmd(persistent=True),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        import threading
        def _drain_stderr():
            for line in self._persistent.stderr:
                line = line.decode().strip()
                if line.startswith('LOAD') or line.startswith('INFER'):
                    LOG.info(f"FasterWhisper persistent worker {line}")
                elif line:
                    LOG.warning(f"FasterWhisper worker stderr: {line}")
        threading.Thread(target=_drain_stderr, daemon=True).start()

        ready = self._persistent.stdout.readline().decode().strip()
        if ready != 'READY':
            LOG.error(f"FasterWhisper persistent worker failed to start: '{ready}'")
            self._persistent.kill()
            self._persistent = None
            self._persistent_ready = False
            return
        self._persistent_ready = True
        LOG.info("FasterWhisper persistent worker ready")

    def unload(self):
        if self._persistent is None:
            return
        LOG.info("FasterWhisper unloading persistent worker")
        self._persistent_ready = False
        try:
            self._persistent.stdin.write(struct.pack('<Q', 0))
            self._persistent.stdin.flush()
            self._persistent.stdin.close()
            self._persistent.wait(timeout=5)
        except Exception as e:
            LOG.warning(f"FasterWhisper unload error: {e}")
            self._persistent.kill()
        finally:
            self._persistent = None
        LOG.info("FasterWhisper persistent worker unloaded")

    # ── On-demand mode ────────────────────────────────────────────────────────

    def spawn(self, sample_rate: int):
        """Pre-spawn one-shot worker so model loads during VAD (on-demand mode).

        No-op if persistent worker is already loaded.
        """
        if self.is_loaded:
            return

        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

        model_name = self._config.get('model', 'medium.en')
        LOG.info(f"FasterWhisper subprocess spawning early ({model_name})")
        self._proc = subprocess.Popen(
            self._make_cmd(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    # ── Transcription ─────────────────────────────────────────────────────────

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        if self.is_loaded:
            return self._transcribe_persistent(audio)
        return self._transcribe_ondemand(audio)

    def _transcribe_persistent(self, audio: np.ndarray) -> str:
        try:
            buf = io.BytesIO()
            np.save(buf, audio)
            audio_bytes = buf.getvalue()

            self._persistent.stdin.write(struct.pack('<Q', len(audio_bytes)))
            self._persistent.stdin.write(audio_bytes)
            self._persistent.stdin.flush()

            result_line = self._persistent.stdout.readline().decode().strip()

            if not self.is_loaded:
                LOG.error("FasterWhisper persistent worker died during transcription")
                self._persistent = None
                self._persistent_ready = False
                return ''

            LOG.info(f"FasterWhisper transcribed: '{result_line}'")
            return result_line

        except Exception as e:
            LOG.error(f"FasterWhisper persistent transcription failed: {e}")
            if self._persistent is not None:
                try:
                    self._persistent.kill()
                except Exception:
                    pass
                self._persistent = None
            return ''

    def _transcribe_ondemand(self, audio: np.ndarray) -> str:
        try:
            if self._proc is None:
                LOG.info("FasterWhisper spawning at transcribe time (no pre-spawn)")
                self._proc = subprocess.Popen(
                    self._make_cmd(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            proc = self._proc
            self._proc = None

            ready_line = proc.stdout.readline().decode().strip()
            if ready_line != 'READY':
                LOG.error(f"FasterWhisper worker unexpected output: '{ready_line}'")
                proc.kill()
                return ''

            buf = io.BytesIO()
            np.save(buf, audio)
            proc.stdin.write(buf.getvalue())
            proc.stdin.close()

            text = proc.stdout.readline().decode().strip()

            import threading
            def _drain_stderr():
                for line in proc.stderr:
                    line = line.decode().strip()
                    if line.startswith('LOAD') or line.startswith('INFER'):
                        LOG.info(f"FasterWhisper {line}")
                    elif line:
                        LOG.warning(f"FasterWhisper worker stderr: {line}")
            threading.Thread(target=_drain_stderr, daemon=True).start()

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
    """Factory: instantiate the configured free-text STT backend.

    Does not call load() — the caller is responsible for calling load() at
    startup if preload=true, or load()/unload() at runtime via voice command.

    Returns FreeTextStt instance, or None if disabled/not configured.
    """
    cfg = realtime_config.get('free_text_stt', {})

    if not cfg.get('enabled', False):
        LOG.info("free_text_stt disabled in config")
        return None

    backend = cfg.get('backend', 'faster_whisper')

    if backend == 'faster_whisper':
        backend_cfg = cfg.get('faster_whisper', {})
        if not backend_cfg.get('model'):
            LOG.error("free_text_stt.faster_whisper.model not configured — free-text disabled")
            return None
        return FasterWhisperSubprocess(backend_cfg)

    LOG.error(f"Unknown free_text_stt backend: '{backend}' — free-text disabled")
    return None
