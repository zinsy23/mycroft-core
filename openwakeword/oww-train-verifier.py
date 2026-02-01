#!/usr/bin/env python3
"""
Train OpenWakeWord Custom Verifier Model

This script trains a logistic regression verifier model that acts as a second-stage
confirmation for wake word detections. The verifier helps reduce false positives by
learning the specific feature patterns of the wake word.

Usage:
    python train_oww_verifier.py --model /path/to/primary_model.onnx \
                                  --positive-dir /path/to/wake-word-samples/ \
                                  --negative-dir /path/to/not-wake-word-samples/ \
                                  --output verifier_model.pkl
"""

import argparse
from pathlib import Path
from openwakeword.custom_verifier_model import train_custom_verifier


def main():
    parser = argparse.ArgumentParser(
        description='Train a custom verifier model for OpenWakeWord'
    )
    parser.add_argument(
        '--model',
        required=True,
        help='Path to the primary OpenWakeWord model (.onnx file)'
    )
    parser.add_argument(
        '--positive-dir',
        required=True,
        help='Directory containing positive wake word samples (.wav files)'
    )
    parser.add_argument(
        '--negative-dir',
        required=True,
        help='Directory containing negative (not wake word) samples (.wav files)'
    )
    parser.add_argument(
        '--output',
        required=True,
        help='Output path for the verifier model (.pkl file)'
    )
    parser.add_argument(
        '--inference-framework',
        default='onnx',
        choices=['onnx', 'tflite'],
        help='Inference framework to use (default: onnx)'
    )
    parser.add_argument(
        '--max-negative-samples',
        type=int,
        default=None,
        help='Maximum number of negative samples to use (default: use all)'
    )

    args = parser.parse_args()

    # Validate inputs
    model_path = Path(args.model)
    if not model_path.exists():
        print(f"Error: Model file not found: {args.model}")
        return 1

    positive_dir = Path(args.positive_dir)
    if not positive_dir.is_dir():
        print(f"Error: Positive directory not found: {args.positive_dir}")
        return 1

    negative_dir = Path(args.negative_dir)
    if not negative_dir.is_dir():
        print(f"Error: Negative directory not found: {args.negative_dir}")
        return 1

    # Get sample files (convert to strings for OpenWakeWord compatibility)
    positive_clips = [str(p) for p in positive_dir.glob("*.wav")]
    negative_clips = [str(p) for p in negative_dir.glob("*.wav")]

    # Limit negative samples if requested
    if args.max_negative_samples and len(negative_clips) > args.max_negative_samples:
        print(f"Limiting negative samples from {len(negative_clips)} to {args.max_negative_samples}")
        import random
        random.seed(42)  # Reproducible selection
        negative_clips = random.sample(negative_clips, args.max_negative_samples)

    if len(positive_clips) == 0:
        print(f"Error: No .wav files found in {args.positive_dir}")
        return 1

    if len(negative_clips) == 0:
        print(f"Error: No .wav files found in {args.negative_dir}")
        return 1

    print("=" * 60)
    print("OpenWakeWord Custom Verifier Training")
    print("=" * 60)
    print(f"\nPrimary model: {args.model}")
    print(f"Positive samples: {len(positive_clips)}")
    print(f"Negative samples: {len(negative_clips)}")
    print(f"Output: {args.output}")
    print(f"Inference framework: {args.inference_framework}")
    print()

    # Train verifier
    print("Training verifier model...")
    print("This will:")
    print("  1. Load the primary OpenWakeWord model")
    print("  2. Extract features from positive samples (wake word)")
    print("  3. Extract features from negative samples (not wake word)")
    print("  4. Train a logistic regression classifier")
    print("  5. Save the verifier model\n")

    try:
        train_custom_verifier(
            positive_reference_clips=positive_clips,
            negative_reference_clips=negative_clips,
            output_path=args.output,
            model_name=str(model_path.absolute()),
            inference_framework=args.inference_framework
        )
        print(f"\n✓ Verifier model saved to: {args.output}")
        print("\nTo use this verifier in Mycroft, add to your mycroft.conf:")
        print(f'''
  "custom_verifier_models": {{
    "{model_path.stem}": "{Path(args.output).absolute()}"
  }},
  "custom_verifier_threshold": 0.5
''')
        return 0

    except Exception as e:
        print(f"\n✗ Error training verifier: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    exit(main())
