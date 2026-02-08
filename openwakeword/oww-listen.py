#!/usr/bin/env python3
"""
OpenWakeWord Listen - Live testing tool for OpenWakeWord models
Mimics precise-listen behavior with continuous scrolling bar graph

Usage:
    python oww-listen.py model.onnx [--sensitivity 0.5] [--chunk-size 1280]
"""

import argparse
import sys
import numpy as np
import pyaudio
from openwakeword import Model
from shutil import get_terminal_size


def main():
    parser = argparse.ArgumentParser(description='Run a model on microphone audio input')
    parser.add_argument('model', help='Path to .onnx model file')
    parser.add_argument('-s', '--sensitivity', type=float, default=0.5,
                       help='Network output required to be considered activated (default: 0.5)')
    parser.add_argument('-c', '--chunk-size', type=int, default=1280,
                       help='Samples between inferences (default: 1280)')
    parser.add_argument('--sample-rate', type=int, default=16000,
                       help='Audio sample rate (default: 16000)')

    args = parser.parse_args()

    # Initialize OpenWakeWord model
    try:
        oww_model = Model(wakeword_models=[args.model], inference_framework='onnx')
        model_name = list(oww_model.models.keys())[0]
        print(f"Loaded model: {model_name}")
        print(f"Sensitivity: {args.sensitivity}")
        print(f"Listening...\n")
    except Exception as e:
        print(f"Failed to load model: {e}")
        sys.exit(1)

    # Initialize PyAudio
    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=args.sample_rate,
        input=True,
        frames_per_buffer=args.chunk_size
    )

    try:
        while True:
            # Read audio chunk
            audio_data = stream.read(args.chunk_size, exception_on_overflow=False)
            audio_array = np.frombuffer(audio_data, dtype=np.int16)

            # Get prediction
            prediction = oww_model.predict(audio_array)
            conf = prediction[model_name]

            # Create bar graph like precise-listen
            max_width = 80
            width = min(get_terminal_size()[0], max_width)
            units = int(round(conf * width))
            bar = 'X' * units + '-' * (width - units)
            cutoff = round((1.0 - args.sensitivity) * width)

            # Show lowercase x for values above sensitivity threshold
            print(bar[:cutoff] + bar[cutoff:].replace('X', 'x'))

    except KeyboardInterrupt:
        print("\nStopped listening.")
    finally:
        stream.stop_stream()
        stream.close()
        audio.terminate()


if __name__ == '__main__':
    main()
