# OpenWakeWord Setup Guide

This guide covers training and using OpenWakeWord models in a Mycroft/OVOS environment. The scripts in the `openwakeword/` directory provide tools for collecting audio, training models, and testing them live.

## Table of Contents
- [Overview](#overview)
- [Scripts](#scripts)
- [Training Models](#training-models)
- [Configuration](#configuration)
- [Training Tips & Lessons Learned](#training-tips--lessons-learned)
- [Alternative Setup Methods](#alternative-setup-methods)

---

## Overview

OpenWakeWord uses a pre-trained Google CNN (speech embedding model) that produces audio embeddings, followed by a small feed-forward classifier network that you train. This is fundamentally different from systems like Precise that use MFCCs (mathematical features).

**Key Difference**: Because OpenWakeWord relies on deep learned embeddings, it expects varied/augmented data during training. Raw duplicate samples will cause overfitting and poor generalization.

---

## Scripts

### oww-collect.py
Records audio samples with auto-numbering, similar to `precise-collect`.

```bash
python openwakeword/oww-collect.py <output_directory>
```

- Press **Space** to start/stop recording
- Press **ESC** to exit
- Files auto-increment: `recording-01.wav`, `recording-02.wav`, etc.

### oww-listen.py
Live testing tool that mimics `precise-listen` behavior with a scrolling bar graph.

```bash
python openwakeword/oww-listen.py <model.onnx> [--sensitivity 0.5]
```

- Shows continuous scrolling bar with 'X' marks for confidence scores
- The `--sensitivity` parameter only affects visualization: uppercase 'X' below threshold, lowercase 'x' above threshold
- Doesn't affect actual model detection behavior (that's controlled by the threshold in mycroft.conf)
- Ctrl+C to exit

### oww-train-model.py
Main training script for OpenWakeWord models.

```bash
python openwakeword/oww-train-model.py \
  --positive-dir <path_to_positive_samples> \
  --negative-dir <path_to_negative_samples> \
  --output <output_model_name.onnx> \
  --augmentation <none|simple|full> \
  --variations <number> \
  --epochs <number>
```

**Required Parameters:**
- `--positive-dir`: Directory containing wake word samples (16kHz, 16-bit WAV files)
- `--negative-dir`: Directory containing non-wake-word samples
- `--output`: Output model filename (e.g., `my_wakeword.onnx`)

**Important Parameters:**
- `--augmentation`: Augmentation type
  - `none`: No augmentation (DOES NOT WORK - Google's embedding model requires varied data; models won't trigger at all; not part of standard OVOS setup, added as experimental option)
  - `simple`: Basic augmentation (RECOMMENDED for most cases)
  - `full`: Aggressive augmentation (may be too much variation)
- `--variations`: Number of augmented variations per sample (e.g., 60)
- `--epochs`: Training epochs (see tips below for optimal range)

### oww-train-verifier.py (Optional)
Trains a custom verifier model for additional false positive reduction.

```bash
python openwakeword/oww-train-verifier.py \
  --model <base_model.onnx> \
  --positive-dir <path_to_positive_samples> \
  --negative-dir <path_to_negative_samples> \
  --output <verifier_model.pkl>
```

The verifier is a logistic regression model that runs as a second-stage filter when the primary model triggers. It can be very strict and may require careful tuning.

**Memory Warning**: Training the verifier loads all features into RAM and can use ~28 GB total (depending on dataset size). If you have 32 GB RAM, it should fit; with less RAM (e.g., 24 GB), you'll need swap space.

**Creating swap space if needed:**
```bash
# Check if swap exists
swapon --show

# If no swap, create 10-15 GB swap file
sudo fallocate -l 15G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# After training completes, optionally remove swap
sudo swapoff /swapfile
sudo rm /swapfile
```

---

## Training Models

### Why Augmentation is Required

OpenWakeWord's embedding model (Google's pre-trained CNN) expects varied audio data. Unlike Precise which uses MFCCs (mathematical transformations), OpenWakeWord uses deep learned features that need diversity to generalize properly.

**Do NOT use `--augmentation none`** even with duplicated samples. The embedding model will overfit to identical features and perform poorly on real-world audio.

### Recommended Training Settings

Based on extensive testing, here are the settings that work well:

```bash
python openwakeword/oww-train-model.py \
  --positive-dir ./wake-word \
  --negative-dir ./not-wake-word \
  --output my_wakeword_simple_60x_2000ep.onnx \
  --augmentation simple \
  --variations 60 \
  --epochs 2000
```

**Key Findings:**
- **Augmentation**: `simple` works best
  - `none`: Doesn't generalize (overfits)
  - `full`: Too aggressive, can hurt accuracy
  - `simple`: Sweet spot for most use cases

- **Epochs**: 2000-3000 is optimal
  - 1000: Insufficient false positive rejection
  - 2000-3000: Sweet spot
  - 4000+: Starts overfitting, MORE false positives (bell curve effect)

- **Variations**: 60 variations per sample works well for moderate-sized datasets

**Context for these settings**: Based on testing with ~65 positive samples (8 voice locations × 6 variations each + testing samples) and ~800+ negative samples including a 15-minute conversation recording, plus duplicated critical negative samples to balance the dataset.

### Data Balancing

If you're experiencing too many false positives, the issue is often data imbalance. The model sees too many positive samples relative to negatives per epoch.

**Strategy**: Duplicate critical negative samples to balance the dataset
- Duplicate samples that represent common false positive triggers
- Duplicate longer conversation/talking samples multiple times
- Duplicate specific problematic phrases that cause false triggers

This emphasizes negative examples during training without requiring more unique audio recordings.

### Example Training Commands

**Basic model (2000 epochs):**
```bash
python openwakeword/oww-train-model.py \
  --positive-dir ./wake-word \
  --negative-dir ./not-wake-word \
  --output my_wakeword_2000ep.onnx \
  --augmentation simple \
  --variations 60 \
  --epochs 2000
```

**Higher epochs for better false positive rejection:**
```bash
python openwakeword/oww-train-model.py \
  --positive-dir ./wake-word \
  --negative-dir ./not-wake-word \
  --output my_wakeword_3000ep.onnx \
  --augmentation simple \
  --variations 60 \
  --epochs 3000
```

---

## Configuration

### Mycroft/OVOS Integration

OpenWakeWord has been adapted to work with this Mycroft environment using the `ovos-ww-plugin-openwakeword` plugin.

Add configuration to `~/.config/mycroft/mycroft.conf`:

```json
{
  "hotwords": {
    "your_wakeword": {
      "module": "ovos-ww-plugin-openwakeword",
      "models": ["/path/to/your_model.onnx"],
      "inference_framework": "onnx",
      "threshold": 0.5
    }
  }
}
```

**Configuration Options:**
- `module`: Must be `"ovos-ww-plugin-openwakeword"` (full plugin name)
- `models`: Array of model paths (can specify multiple)
- `inference_framework`: Use `"onnx"` (default, fastest)
- `threshold`: Detection threshold (0.0-1.0)
  - Lower = more sensitive (more false positives)
  - Higher = less sensitive (may miss true positives)
  - Start with 0.5 and adjust based on testing

### Optional: Custom Verifier Model

If you've trained a verifier model for additional false positive reduction:

```json
{
  "hotwords": {
    "your_wakeword": {
      "module": "ovos-ww-plugin-openwakeword",
      "models": ["/path/to/your_model.onnx"],
      "verifier_models": ["/path/to/your_verifier.pkl"],
      "custom_verifier_threshold": 0.5,
      "inference_framework": "onnx",
      "threshold": 0.5
    }
  }
}
```

**Verifier Options:**
- `verifier_models`: Array of verifier .pkl files (matches models array)
- `custom_verifier_threshold`: When to run the verifier (controls primary model threshold for verifier activation, NOT the verifier's decision threshold)

**Note**: The verifier can be very strict. If it blocks legitimate detections even at low thresholds, you may need to retrain it with different data or focus on improving the primary model instead.

---

## Training Tips & Lessons Learned

### What Works
✓ **Simple augmentation** with 60 variations
✓ **2000-3000 epochs** (sweet spot)
✓ **Long conversation samples** (15+ minutes) in negative set
✓ **Duplicating critical negative samples** for balance
✓ **Testing with oww-listen.py** for immediate feedback

### What Doesn't Work
✗ **Raw samples** (`--augmentation none`) - models won't trigger at all; Google's embedding model fundamentally requires variation
✗ **4000+ epochs** - causes MORE false positives (overfitting)
✗ **Full augmentation** - too aggressive for most cases
✗ **Unbalanced positive:negative ratios** - leads to excessive false triggers

### Epoch Sweet Spot Explained

Training follows a bell curve pattern:
- **Too few epochs** (< 1000): Model hasn't learned to reject false positives
- **Optimal range** (2000-3000): Best balance of true positive detection and false positive rejection
- **Too many epochs** (4000+): Model starts overfitting to positive samples, becomes trigger-happy

### False Positive Debugging

If experiencing frequent false positives:

1. **Check your data ratio**: Count positive vs negative samples (after augmentation/duplication)
   - Ratio should favor negatives or be balanced
   - Too many positives per epoch → false positive prone

2. **Record false positive triggers**: Use oww-collect.py to capture phrases that falsely trigger
   - Add these to negative dataset
   - Duplicate them to emphasize during training

3. **Test different thresholds**: Before retraining, try adjusting the threshold in config
   - Higher threshold (0.6-0.8) reduces false positives
   - May need to retrain if threshold needs to be too high

4. **Consider epoch count**: Try reducing from 3000 to 2000 epochs
   - Sometimes less training is better

### Memory Considerations

- **Primary model training**: Uses memory-mapped files, very efficient
- **Verifier training**: Loads all features into RAM
  - Can use 10-20+ GB depending on dataset size
  - Add swap space if you hit OOM errors
  - Consider limiting negative samples if memory is constrained

---

## Troubleshooting

### Model triggers constantly
- Data imbalance (too many positive samples per epoch)
- Try duplicating negative samples
- Consider reducing epochs (4000 → 2000)
- Increase threshold in config

### Model rarely triggers
- Threshold too high
- Not enough epochs (< 1000)
- Positive samples may need more variation
- Check if augmentation is enabled

### Training crashes (OOM)
- For verifier training: Add swap space
- For primary model: Reduce batch size or dataset size

### Poor generalization / Model won't trigger
- Using `--augmentation none` → switch to `simple` (none doesn't work at all)
- Not enough negative sample variety
- Need longer conversation samples in negative set

---

## Quick Reference

### Typical Workflow

1. **Collect positive samples**:
   ```bash
   python openwakeword/oww-collect.py ./wake-word
   ```

2. **Collect/gather negative samples** (non-wake-word speech)

3. **Train model**:
   ```bash
   python openwakeword/oww-train-model.py \
     --positive-dir ./wake-word \
     --negative-dir ./not-wake-word \
     --output my_wakeword.onnx \
     --augmentation simple \
     --variations 60 \
     --epochs 2000
   ```

4. **Test with oww-listen.py**:
   ```bash
   python openwakeword/oww-listen.py my_wakeword.onnx --sensitivity 0.5
   ```

5. **Configure in mycroft.conf** and restart Mycroft

6. **Iterate**: Adjust data, epochs, or threshold based on real-world performance

---

## Alternative Setup Methods

This section describes alternative approaches for environments that don't use the scripts in the `openwakeword/` folder.

### Using Pre-trained Models

Check available models:
```bash
python3 << 'EOF'
from openwakeword import get_pretrained_model_paths
for model in get_pretrained_model_paths():
    print(model)
EOF
```

Available pre-trained models:
- `alexa_v0.1`
- `hey_jarvis_v0.1`
- `hey_mycroft_v0.1`
- `hey_rhasspy_v0.1`
- `timer_v0.1`
- `weather_v0.1`

### Training via Google Colab

For automated synthetic training without local scripts:

1. Visit: https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb
2. Follow the automated training notebook
3. Input your wake word phrase
4. Download the resulting `.tflite` or `.onnx` model

### Alternative Configuration Format

Some setups may use this configuration style:

```json
{
  "openwakeword": {
    "model_path": "~/.local/share/mycroft/openwakeword/models",
    "inference_framework": "tflite"
  },
  "hotwords": {
    "computer": {
      "module": "openwakeword",
      "model": "computer",
      "threshold": 0.5,
      "lang": "en-us"
    }
  },
  "listener": {
    "wake_word": "computer"
  }
}
```

---

## Additional Resources

- OpenWakeWord GitHub: https://github.com/dscripka/openWakeWord
- Training Guide: https://github.com/dscripka/openWakeWord/blob/main/docs/custom_verifier_models.md
- Colab Training: https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb
- Mycroft Forums: https://community.mycroft.ai
