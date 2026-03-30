"""
Standalone Whisper transcription worker.

Spawned as a subprocess by FasterWhisperSubprocess.transcribe().
Loads the model, transcribes, prints result to stdout, exits.
All VRAM is released when the process exits.

Usage:
    python whisper_worker.py <npy_file> <sample_rate> <model> <device> <compute_type>
                             <language> <beam_size> <no_speech_threshold>
                             [<hallucination_silence_threshold>]
"""
import sys
import numpy as np
from faster_whisper import WhisperModel

def main():
    args = sys.argv[1:]
    if len(args) < 8:
        print('', flush=True)
        sys.exit(1)

    npy_file          = args[0]
    sample_rate       = int(args[1])
    model_path        = args[2]
    device            = args[3]
    compute_type      = args[4]
    language          = args[5]
    beam_size         = int(args[6])
    no_speech_threshold = float(args[7])
    hallucination_silence_threshold = float(args[8]) if len(args) > 8 else None

    audio = np.load(npy_file)

    # Resample to 16kHz if needed
    if sample_rate != 16000:
        target_len = int(len(audio) * 16000 / sample_rate)
        audio = np.interp(
            np.linspace(0, len(audio) - 1, target_len),
            np.arange(len(audio)),
            audio
        ).astype(np.float32)

    model = WhisperModel(model_path, device=device, compute_type=compute_type)

    segments, _ = model.transcribe(
        audio,
        language=language,
        beam_size=beam_size,
        vad_filter=True,
        no_speech_threshold=no_speech_threshold,
        hallucination_silence_threshold=hallucination_silence_threshold,
    )
    text = ' '.join(seg.text for seg in segments).strip()
    print(text, flush=True)

if __name__ == '__main__':
    main()
