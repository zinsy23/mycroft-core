"""
Standalone Whisper transcription worker.

Spawned as a subprocess by FasterWhisperSubprocess. VRAM is fully released
on process exit — no residual allocations in the main Mycroft process.

Two modes:
  One-shot (default): load model, signal READY, read one audio job from stdin,
      transcribe, print result, exit. VRAM released on exit.

  Persistent (--persistent flag): load model, signal READY, then loop accepting
      jobs until a zero-length sentinel is received. Model stays in VRAM between
      jobs. Each job is length-prefixed: 8-byte little-endian uint64 length, then
      npy bytes. Result is a newline-terminated UTF-8 string on stdout.

Audio is always passed as npy bytes (float32 array at 16kHz).
"""
import sys
import io
import struct
import time
import numpy as np
from faster_whisper import WhisperModel


def _transcribe(model, audio, language, beam_size):
    t0 = time.monotonic()
    segments, _ = model.transcribe(
        audio,
        language=language,
        beam_size=beam_size,
        condition_on_previous_text=False,
        vad_filter=False,
    )
    text = ''.join(seg.text for seg in segments).strip()
    elapsed = time.monotonic() - t0
    print(f'INFER {elapsed:.2f}s', file=sys.stderr, flush=True)
    return text


def main():
    args = sys.argv[1:]
    persistent = '--persistent' in args
    args = [a for a in args if a != '--persistent']

    if len(args) < 5:
        sys.stdout.write('\n')
        sys.stdout.flush()
        sys.exit(1)

    model        = args[0]
    use_cuda     = args[1].lower() == 'true'
    compute_type = args[2]
    language     = args[3]
    beam_size    = int(args[4])

    device = 'cuda' if use_cuda else 'cpu'

    t0 = time.monotonic()
    engine = WhisperModel(model, device=device, compute_type=compute_type)
    print(f'LOAD {time.monotonic() - t0:.2f}s', file=sys.stderr, flush=True)

    sys.stdout.write('READY\n')
    sys.stdout.flush()

    if not persistent:
        audio_bytes = sys.stdin.buffer.read()
        audio = np.load(io.BytesIO(audio_bytes))
        text = _transcribe(engine, audio, language, beam_size)
        sys.stdout.write(text + '\n')
        sys.stdout.flush()
        return

    while True:
        header = sys.stdin.buffer.read(8)
        if len(header) < 8:
            break
        job_len = struct.unpack('<Q', header)[0]
        if job_len == 0:
            break
        audio_bytes = sys.stdin.buffer.read(job_len)
        if len(audio_bytes) < job_len:
            break
        audio = np.load(io.BytesIO(audio_bytes))
        text = _transcribe(engine, audio, language, beam_size)
        sys.stdout.write(text + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
