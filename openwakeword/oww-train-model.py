#!/usr/bin/env python3
"""
Train OpenWakeWord Full Model for Mycroft

This script trains a complete wake word detection model from your audio samples
using OpenWakeWord's training framework with synthetic augmentation.

Based on OpenWakeWord's training methodology.
Requires PyTorch and full training dependencies.

Usage:
    python train_oww_model.py \\
        --wake-word "computer" \\
        --samples-dir /path/to/wake-word-samples \\
        --output computer.tflite \\
        --steps 5000

Or run interactively:
    python train_oww_model.py
"""

import argparse
import os
import sys
from pathlib import Path

def check_dependencies():
    """Check if all required dependencies are installed, and offer to install if missing."""
    import subprocess

    # Check if dependencies are already installed
    try:
        import torch
        import openwakeword.train
        import librosa
        import soundfile
        print(f"✓ PyTorch {torch.__version__} ({'CUDA' if torch.cuda.is_available() else 'CPU'})")
        return True
    except ImportError as e:
        print(f"⚠️  Missing training dependency: {e}")
        print("\nOpenWakeWord training requires PyTorch and additional dependencies.")
        print("These are large packages (~4-6 GB) but only needed for training custom wake words.")
        print()

        # Check if NVIDIA GPU is available for acceleration
        gpu_available = False
        try:
            result = subprocess.run(['nvidia-smi', '--query-gpu=name,compute_cap', '--format=csv,noheader'],
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                gpu_info = result.stdout.strip().split('\n')
                print("🎮 NVIDIA GPU(s) detected:")
                for gpu in gpu_info:
                    name, compute_cap = gpu.rsplit(',', 1)
                    compute_cap = float(compute_cap.strip())
                    print(f"   • {name.strip()} (Compute Capability {compute_cap})")
                    if compute_cap >= 7.0:
                        gpu_available = True

                if gpu_available:
                    print("\n✅ GPU acceleration supported! Training will be much faster with CUDA.")
                else:
                    print("\n⚠️  GPU compute capability < 7.0 - PyTorch requires 7.0+ for CUDA support.")
                    print("   Training will use CPU only.")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            print("ℹ️  No NVIDIA GPU detected - training will use CPU (slower).")

        print()
        response = input("Install training dependencies now? [Y/n]: ").strip().lower()
        if response == 'n':
            print("❌ Training dependencies required. Exiting.")
            return False

        print("\n📦 Installing training dependencies...")
        print("   This may take several minutes...")

        try:
            # Install PyTorch with CUDA support if GPU available
            if gpu_available:
                print("\n1️⃣  Installing PyTorch with CUDA 12.4 support...")
                subprocess.check_call([
                    sys.executable, '-m', 'pip', 'install',
                    'torch', 'torchvision', 'torchaudio',
                    '--index-url', 'https://download.pytorch.org/whl/cu124'
                ])
            else:
                print("\n1️⃣  Installing PyTorch (CPU-only)...")
                subprocess.check_call([
                    sys.executable, '-m', 'pip', 'install',
                    'torch', 'torchvision', 'torchaudio'
                ])

            # Install training-specific dependencies
            print("\n2️⃣  Installing audio processing libraries...")
            subprocess.check_call([
                sys.executable, '-m', 'pip', 'install',
                'torchinfo', 'torchmetrics', 'soundfile', 'librosa'
            ])

            print("\n3️⃣  Installing audio augmentation libraries...")
            subprocess.check_call([
                sys.executable, '-m', 'pip', 'install',
                'audiomentations', 'torch-audiomentations'
            ])

            print("\n4️⃣  Installing additional training utilities...")
            subprocess.check_call([
                sys.executable, '-m', 'pip', 'install',
                'speechbrain', 'pronouncing', 'webrtcvad',
                'pydub', 'mutagen', 'acoustics', 'matplotlib', 'pandas'
            ])

            print("\n✅ All dependencies installed successfully!")

            # Verify installation
            import torch
            print(f"\n✓ PyTorch {torch.__version__} ({'CUDA' if torch.cuda.is_available() else 'CPU'})")
            if gpu_available and not torch.cuda.is_available():
                print("\n⚠️  Warning: GPU was detected but PyTorch can't access CUDA.")
                print("   Make sure NVIDIA drivers are installed and CUDA runtime libraries are available.")
                print("   Training will continue on CPU.")

            return True

        except subprocess.CalledProcessError as e:
            print(f"\n❌ Installation failed: {e}")
            print("\nYou can try installing manually with:")
            print("  pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124")
            print("  pip install torchinfo torchmetrics soundfile librosa")
            print("  pip install audiomentations torch-audiomentations")
            print("  pip install speechbrain pronouncing webrtcvad")
            print("  pip install pydub mutagen acoustics matplotlib pandas")
            return False

def prepare_training_data(positive_dir, negative_dir, wake_word, clip_length=3,
                         augmentation_type='full', variations_per_sample=1):
    """
    Prepare training data from positive and negative samples.

    Args:
        positive_dir: Directory containing positive wake word samples
        negative_dir: Directory containing negative samples
        wake_word: The wake word phrase
        clip_length: Length of clips in seconds (default: 3)
        augmentation_type: 'full' (all augmentations), 'simple' (mix_clips_batch like notebook), or 'none'
        variations_per_sample: How many augmented variations to generate per sample (default: 1)

    Returns:
        Tuple of (positive_features_file, negative_features_file)
    """
    import numpy as np
    from numpy.lib.format import open_memmap
    import openwakeword
    import openwakeword.data
    import openwakeword.utils
    import scipy.io.wavfile
    from tqdm import tqdm

    positive_dir = Path(positive_dir)
    negative_dir = Path(negative_dir)

    # Get positive samples
    positive_files = list(positive_dir.glob("*.wav"))
    if len(positive_files) < 3:
        raise ValueError(f"Need at least 3 wake word samples, found {len(positive_files)}")

    # Get negative samples
    negative_files = list(negative_dir.glob("*.wav"))
    if len(negative_files) == 0:
        raise ValueError(f"Need at least 1 negative sample file")

    print(f"\n=== Preparing Training Data ===")
    print(f"Wake word: {wake_word}")
    print(f"Positive samples: {len(positive_files)}")
    print(f"Negative samples: {len(negative_files)}")
    print(f"Clip length: {clip_length} seconds")
    print(f"Augmentation: {augmentation_type}")
    if variations_per_sample > 1:
        print(f"Generating {variations_per_sample} variations per sample = {len(positive_files) * variations_per_sample} total positive samples")

    # Create output directory
    output_dir = Path("/tmp/oww_training") / wake_word.replace(" ", "_")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Filter audio files
    print("\nFiltering positive clips...")
    positive_clips, positive_durations = openwakeword.data.filter_audio_paths(
        [str(positive_dir)],
        min_length_secs=0.5,
        max_length_secs=2.0,
        duration_method="header"
    )
    print(f"  {len(positive_clips)} positive clips after filtering")

    print("\nFiltering negative clips...")
    negative_clips, negative_durations = openwakeword.data.filter_audio_paths(
        [str(negative_dir)],
        min_length_secs=1.0,
        max_length_secs=60*30,  # 30 minutes max
        duration_method="header"
    )
    print(f"  {len(negative_clips)} negative clips after filtering (~{sum(negative_durations)//3600} hours)")

    # Initialize audio feature extractor with GPU acceleration
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"\nFeature extraction device: {device}")

    F = openwakeword.utils.AudioFeatures(
        device=device,
        ncpu=8,  # Use multiple CPUs for audio preprocessing (GPU only helps with embedding)
        inference_framework="onnx"  # ONNX supports CUDA better than TFLite
    )

    # --- Process Negative Clips First ---
    print("\nProcessing negative clips...")
    batch_size = 64
    N_total = int(sum(negative_durations) // clip_length)
    n_feature_cols = F.get_embedding_shape(clip_length)

    negative_features_file = output_dir / "negative_features.npy"
    fp = open_memmap(str(negative_features_file), mode='w+', dtype=np.float32,
                     shape=(N_total, n_feature_cols[0], n_feature_cols[1]))

    row_counter = 0
    for i in tqdm(np.arange(0, len(negative_clips), batch_size)):
        # Load batch manually with soundfile (matching notebook's approach)
        import soundfile as sf
        batch_clips = negative_clips[i:i+batch_size]
        wav_data = []
        for clip_path in batch_clips:
            # Load as float (default), then convert to int16 (matches datasets behavior)
            audio, sr = sf.read(clip_path)

            # Convert stereo to mono if needed
            if audio.ndim > 1:
                audio = audio.mean(axis=1)

            if sr != 16000:
                # Resample if needed
                import librosa
                audio = librosa.resample(audio.astype(np.float32), orig_sr=sr, target_sr=16000)
            # Convert float to int16 (same as notebook: array*32767)
            audio = (audio * 32767).astype(np.int16)
            wav_data.append(audio)

        wav_data = openwakeword.data.stack_clips(wav_data, clip_size=16000*clip_length).astype(np.int16)

        # Extract features (use 8 CPUs for preprocessing even with GPU)
        features = F.embed_clips(x=wav_data, batch_size=1024, ncpu=8)

        # Save to memory-mapped file
        if row_counter + features.shape[0] > N_total:
            fp[row_counter:min(row_counter+features.shape[0], N_total), :, :] = features[0:N_total - row_counter, :, :]
            fp.flush()
            break
        else:
            fp[row_counter:row_counter+features.shape[0], :, :] = features
            row_counter += features.shape[0]
            fp.flush()

    openwakeword.data.trim_mmap(str(negative_features_file))
    print(f"✓ Negative features: {negative_features_file}")

    # --- Process Positive Clips with Augmentation ---
    sr = 16000
    total_length = int(sr * clip_length)
    batch_size = 8

    # Process augmented clips - generate multiple variations per sample
    N_total = len(positive_clips) * variations_per_sample
    positive_features_file = output_dir / "positive_features.npy"
    fp = open_memmap(str(positive_features_file), mode='w+', dtype=np.float32,
                     shape=(N_total, n_feature_cols[0], n_feature_cols[1]))

    row_counter = 0

    if augmentation_type == 'simple':
        print("\nProcessing positive clips with simple background mixing (notebook style)...")
        print(f"  (Background mixing at SNR 5-15, volume augmentation)")
        print(f"  Generating {variations_per_sample} variations per sample...")

        # Generate multiple variations by running the generator multiple times
        for variation_num in range(variations_per_sample):
            # Calculate clip alignment with random jitter for each variation
            jitters = (np.random.uniform(0, 0.2, len(positive_clips))*sr).astype(np.int32)
            starts = [total_length - (int(np.ceil(i*sr))+j) for i,j in zip(positive_durations, jitters)]

            # Create generator for this variation
            augmentation_generator = openwakeword.data.mix_clips_batch(
                foreground_clips=positive_clips,
                background_clips=negative_clips,
                combined_size=total_length,
                batch_size=batch_size,
                snr_low=5,
                snr_high=15,
                start_index=starts,
                volume_augmentation=True
            )

            # Process batches for this variation
            for batch in tqdm(augmentation_generator,
                            total=len(positive_clips)//batch_size,
                            desc=f"Variation {variation_num+1}/{variations_per_sample}"):
                augmented_audio = batch[0]

                # Extract features
                features = F.embed_clips(augmented_audio, batch_size=256)

                # Save
                fp[row_counter:row_counter+features.shape[0], :, :] = features
                row_counter += features.shape[0]
                fp.flush()

    elif augmentation_type == 'full':
        print("\nProcessing positive clips with advanced augmentation...")
        print("  (PitchShift, EQ, Distortion, Noise, Reverb, Background mixing)")
        print(f"  Generating {variations_per_sample} variations per sample...")

        # Generate multiple variations by running the generator multiple times
        for variation_num in range(variations_per_sample):
            # Create generator for this variation (random augmentations each time)
            augmentation_generator = openwakeword.data.augment_clips(
                clip_paths=positive_clips,
                total_length=total_length,
                sr=sr,
                batch_size=batch_size,
                background_clip_paths=negative_clips,
                augmentation_probabilities={
                    'SevenBandParametricEQ': 0.25,
                    'TanhDistortion': 0.25,
                    'PitchShift': 0.25,
                    'BandStopFilter': 0.25,
                    'AddColoredNoise': 0.25,
                    'AddBackgroundNoise': 0.75,
                    'Gain': 1.0,
                    'RIR': 0.5  # Room impulse response (reverb)
                }
            )

            # Process batches for this variation
            for batch in tqdm(augmentation_generator,
                            total=len(positive_clips)//batch_size,
                            desc=f"Variation {variation_num+1}/{variations_per_sample}"):
                # Convert float to int16
                augmented_audio = (batch * 32767).astype(np.int16)

                # Extract features
                features = F.embed_clips(augmented_audio, batch_size=256)

                # Save
                fp[row_counter:row_counter+features.shape[0], :, :] = features
                row_counter += features.shape[0]
                fp.flush()
    else:
        # No augmentation - just load raw samples (with optional duplication)
        print("\nProcessing positive clips with NO augmentation (raw samples only)...")
        if variations_per_sample > 1:
            print(f"  Duplicating each sample {variations_per_sample} times for better data balance")
            print(f"  Total positive samples: {len(positive_clips)} × {variations_per_sample} = {len(positive_clips) * variations_per_sample}")
        else:
            print("  Using original samples without any modifications")

        import soundfile as sf

        # Duplicate samples if requested (variations_per_sample times)
        for duplicate_num in range(variations_per_sample):
            desc = f"Duplicate {duplicate_num+1}/{variations_per_sample}" if variations_per_sample > 1 else "Processing raw samples"
            for clip_path in tqdm(positive_clips, desc=desc):
                # Load audio
                audio, sr_file = sf.read(clip_path)

                # Convert stereo to mono if needed
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)

                # Resample if needed
                if sr_file != sr:
                    import librosa
                    audio = librosa.resample(audio.astype(np.float32), orig_sr=sr_file, target_sr=sr)

                # Pad or trim to exact length
                if len(audio) < total_length:
                    # Pad with silence
                    audio = np.pad(audio, (0, total_length - len(audio)), mode='constant')
                else:
                    # Trim to length
                    audio = audio[:total_length]

                # Convert to int16
                audio = (audio * 32767).astype(np.int16)

                # Extract features
                features = F.embed_clips(np.array([audio]), batch_size=1)

                # Save
                fp[row_counter:row_counter+features.shape[0], :, :] = features
                row_counter += features.shape[0]
                fp.flush()

    openwakeword.data.trim_mmap(str(positive_features_file))
    print(f"✓ Positive features: {positive_features_file}")
    print(f"   Total samples generated: {row_counter}")

    return positive_features_file, negative_features_file

def train_model(positive_features, negative_features, wake_word, n_epochs=10, output_path=None):
    """
    Train the wake word detection model.

    Args:
        positive_features: Path to positive training features
        negative_features: Path to negative training features
        wake_word: The wake word phrase
        n_epochs: Number of training epochs (default: 10)
        output_path: Where to save the final model
    """
    import torch
    from torch import nn
    import numpy as np
    import collections
    from tqdm import tqdm

    print(f"\n=== Training Model ===")
    print(f"Epochs: {n_epochs}")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")

    # Load training features
    print(f"\nLoading features...")
    negative_X = np.load(str(negative_features))
    positive_X = np.load(str(positive_features))

    print(f"  Negative: {negative_X.shape}")
    print(f"  Positive: {positive_X.shape}")

    # Combine and create labels
    X = np.vstack((negative_X, positive_X))
    y = np.array([0]*len(negative_X) + [1]*len(positive_X)).astype(np.float32)[...,None]

    print(f"  Combined: {X.shape}")

    # Create PyTorch dataloader
    batch_size = 512
    training_data = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(torch.from_numpy(X), torch.from_numpy(y)),
        batch_size=batch_size,
        shuffle=True
    )

    # Define model architecture (simple fully-connected network)
    # Note: 32 neurons works well with ~100-200 training samples
    #       Larger models (128+) need significantly more data to avoid overfitting
    layer_dim = 32
    model = nn.Sequential(
        nn.Flatten(),
        nn.Linear(X.shape[1]*X.shape[2], layer_dim),  # timesteps * features
        nn.LayerNorm(layer_dim),
        nn.ReLU(),
        nn.Linear(layer_dim, layer_dim),
        nn.LayerNorm(layer_dim),
        nn.ReLU(),
        nn.Linear(layer_dim, 1),
        nn.Sigmoid(),
    )
    model = model.to(device)

    print("\nModel architecture:")
    print(model)

    # Setup training
    loss_function = torch.nn.functional.binary_cross_entropy
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

    # Train
    print(f"\nTraining for {n_epochs} epochs...")
    history = collections.defaultdict(list)

    for epoch in tqdm(range(n_epochs), total=n_epochs):
        for batch in training_data:
            x, labels = batch[0].to(device), batch[1].to(device)

            # Weight negative class higher to reduce false positives
            weights = torch.ones(labels.shape[0]).to(device)
            weights[labels.flatten() == 1] = 0.1

            # Forward pass
            optimizer.zero_grad()
            predictions = model(x)

            # Backward pass
            loss = loss_function(predictions, labels, weights[..., None])
            loss.backward()
            optimizer.step()

            # Log metrics
            history['loss'].append(float(loss.detach().cpu().numpy()))

            tp = sum(predictions.flatten()[labels.flatten() == 1] >= 0.5)
            fn = sum(predictions.flatten()[labels.flatten() == 1] < 0.5)
            # Avoid division by zero if no positive samples in batch
            if (tp + fn) > 0:
                recall = float((tp/(tp+fn)).detach().cpu().numpy())
            else:
                recall = 0.0
            history['recall'].append(recall)

    print("\n✓ Training complete!")
    print(f"  Final loss: {history['loss'][-1]:.4f}")
    print(f"  Final recall: {history['recall'][-1]:.4f}")

    # Export model
    if output_path is None:
        output_path = f"{wake_word.replace(' ', '_')}.onnx"

    output_path = Path(output_path)
    onnx_path = output_path.with_suffix('.onnx')

    # Export to ONNX
    print(f"\nExporting model to {onnx_path}...")
    torch.onnx.export(
        model,
        args=torch.zeros((1, X.shape[1], X.shape[2])).to(device),
        f=str(onnx_path)
    )
    print(f"✓ ONNX model: {onnx_path}")

    # Convert to TFLite (optional)
    tflite_path = output_path.with_suffix('.tflite')
    try:
        from openwakeword.train import convert_onnx_to_tflite
        convert_onnx_to_tflite(str(onnx_path), str(tflite_path))
        print(f"✓ TFLite model: {tflite_path}")
    except Exception as e:
        print(f"  (TFLite conversion failed: {e})")
        tflite_path = None

    return onnx_path, tflite_path

def interactive_mode():
    """Interactive mode for gathering training parameters."""
    print("\n=== Interactive Training Mode ===\n")

    # Get wake word
    wake_word = input("Wake word phrase: ").strip()
    if not wake_word:
        print("ERROR: Wake word is required")
        sys.exit(1)

    # Get positive samples directory
    positive_dir = input(f"Path to '{wake_word}' positive samples directory: ").strip()
    positive_dir = os.path.expanduser(positive_dir)
    if not os.path.isdir(positive_dir):
        print(f"ERROR: Directory not found: {positive_dir}")
        sys.exit(1)

    # Get negative samples directory
    negative_dir = input(f"Path to negative/background samples directory: ").strip()
    negative_dir = os.path.expanduser(negative_dir)
    if not os.path.isdir(negative_dir):
        print(f"ERROR: Directory not found: {negative_dir}")
        sys.exit(1)

    # Get augmentation type
    print("\nAugmentation options:")
    print("  1. Simple mixing (like OpenWakeWord notebook - recommended by developers)")
    print("     - Mixes wake word with background at random volumes")
    print("     - Aligns wake word to end of window (reduces latency)")
    print("     - What OpenWakeWord developers say works best")
    print()
    print("  2. Full augmentation (all techniques)")
    print("     - Everything from option 1, PLUS:")
    print("     - Pitch shifting, EQ changes, distortion")
    print("     - Colored noise, reverb effects")
    print("     - More variety but may change voice too much")
    print()
    aug_choice = input("Choose augmentation [1/2, default=1]: ").strip()
    augmentation_type = 'simple' if aug_choice != '2' else 'full'

    # Get variations per sample
    print("\nHow many augmented variations to generate per sample?")
    print("  You have recordings - each will be augmented multiple times")
    print("  More variations = more training data, better model (but slower training)")
    print("  Examples:")
    print("    1  = No multiplication (62 samples → 62 training examples)")
    print("    10 = 10x multiplication (62 samples → 620 training examples)")
    print("    20 = 20x multiplication (62 samples → 1,240 training examples)")
    variations = input("Variations per sample [10]: ").strip()
    variations_per_sample = int(variations) if variations else 10

    # Get training parameters
    print(f"\nWith {variations_per_sample}x variations, recommended epochs are lower")
    print(f"  (You'll have {variations_per_sample}x more data to train on)")
    default_epochs = max(10, 300 // variations_per_sample)
    n_epochs = input(f"Training epochs [{default_epochs}]: ").strip()
    n_epochs = int(n_epochs) if n_epochs else default_epochs

    clip_length = input("Clip length in seconds [3]: ").strip()
    clip_length = int(clip_length) if clip_length else 3

    # Get output path
    default_output = f"{wake_word.replace(' ', '_')}.onnx"
    output_path = input(f"Output path [{default_output}]: ").strip()
    if not output_path:
        output_path = default_output

    # Confirm
    print("\n" + "=" * 60)
    print("Configuration:")
    print("=" * 60)
    print(f"Wake word:          {wake_word}")
    print(f"Positive dir:       {positive_dir}")
    print(f"Negative dir:       {negative_dir}")
    print(f"Augmentation:       {augmentation_type}")
    print(f"Variations/sample:  {variations_per_sample}x")
    print(f"Clip length:        {clip_length}s")
    print(f"Training epochs:    {n_epochs}")
    print(f"Output:             {output_path}")
    print("=" * 60)

    confirm = input("\nProceed with training? [y/N]: ").strip().lower()
    if confirm != 'y':
        print("Training cancelled.")
        sys.exit(0)

    return wake_word, positive_dir, negative_dir, clip_length, n_epochs, output_path, augmentation_type, variations_per_sample

def main():
    """Main training workflow."""
    parser = argparse.ArgumentParser(
        description="Train OpenWakeWord full model for Mycroft",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode
  python train_oww_model.py

  # Command line mode
  python train_oww_model.py \\
      --wake-word "computer" \\
      --positive-dir ~/samples/wake-word \\
      --negative-dir ~/samples/not-wake-word \\
      --output computer.onnx \\
      --epochs 10
        """
    )

    parser.add_argument('--wake-word', help='Wake word phrase (e.g., "computer")')
    parser.add_argument('--positive-dir', help='Directory containing wake word samples')
    parser.add_argument('--negative-dir', help='Directory containing negative/background samples')
    parser.add_argument('--clip-length', type=int, default=3, help='Clip length in seconds (default: 3)')
    parser.add_argument('--epochs', type=int, default=30, help='Training epochs (default: 30)')
    parser.add_argument('--augmentation', choices=['simple', 'full', 'none'], default='simple',
                       help='Augmentation type: simple (notebook style), full (all techniques), or none (raw samples only)')
    parser.add_argument('--variations', type=int, default=10,
                       help='Variations to generate per sample (default: 10)')
    parser.add_argument('--output', help='Output model file path')

    args = parser.parse_args()

    print("=" * 60)
    print("OpenWakeWord Full Model Training")
    print("=" * 60)

    # Check dependencies
    if not check_dependencies():
        sys.exit(1)

    # Interactive or command-line mode
    if not all([args.wake_word, args.positive_dir, args.negative_dir]):
        wake_word, positive_dir, negative_dir, clip_length, n_epochs, output_path, augmentation_type, variations_per_sample = interactive_mode()
    else:
        wake_word = args.wake_word
        positive_dir = args.positive_dir
        negative_dir = args.negative_dir
        clip_length = args.clip_length
        n_epochs = args.epochs
        output_path = args.output
        augmentation_type = args.augmentation
        variations_per_sample = args.variations

    # Prepare training data
    positive_features, negative_features = prepare_training_data(
        positive_dir, negative_dir, wake_word, clip_length,
        augmentation_type, variations_per_sample
    )

    # Train model
    onnx_path, tflite_path = train_model(
        positive_features, negative_features, wake_word, n_epochs, output_path
    )

    print("\n" + "=" * 60)
    print("Training Complete!")
    print("=" * 60)
    print(f"\nModels saved:")
    print(f"  - ONNX:  {onnx_path}")
    if tflite_path:
        print(f"  - TFLite: {tflite_path}")
    print("\nBoth formats work with OpenWakeWord.")
    print("ONNX may be faster on desktop GPUs, TFLite is smaller for distribution.")
    print("\nNext steps:")
    print("1. Update ~/.config/mycroft/mycroft.conf:")
    print(f"""
  "hotwords": {{
    "{wake_word}": {{
      "module": "openwakeword",
      "models": ["{onnx_path.absolute()}"],
      "inference_framework": "onnx",  // or "tflite"
      "threshold": 0.5
    }}
  }}
""")
    print("2. Restart Mycroft and test: mycroft-say-to 'computer'")
    print()

if __name__ == "__main__":
    main()
