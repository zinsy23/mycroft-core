# Custom Wake Word Setup Guide

## Overview

This guide covers setting up custom wake words with Mycroft Precise 0.3.0 on Python 3.11+. 

**⚠️ Important**: This guide is ONLY needed if you want to use **custom wake word models** (like "computer", "jarvis", etc.). The default "hey mycroft" wake word works out-of-the-box without any of these steps.

## When Do You Need This Guide?

**You DON'T need this if:**
- ✅ Using default "hey mycroft" wake word
- ✅ You answered "NO" to custom wake words during `dev_setup.sh`

**You DO need this if:**
- ❌ Using a custom wake word model (`.pb` file)
- ❌ You answered "YES" to custom wake words during `dev_setup.sh`
- ❌ You want to train your own wake word

## Prerequisites

- Mycroft installed via `dev_setup.sh` **with custom wake words enabled**
- Custom wake word model file (`.pb` format)
- Python 3.11+ virtual environment

## Known Issues & Solutions

### Issue 1: Precise Binary Download Override

**Problem**: Mycroft automatically downloads an old, broken Precise 0.2.0 binary (from 2018) that overwrites any working installation.

**Solution**: Use a symlink to prevent the override:

```bash
# Stop Mycroft
cd ~/mycroft-core
./stop-mycroft.sh all

# Remove the broken downloaded binary
rm -f ~/.local/share/mycroft/precise/precise-engine/precise-engine

# Create symlink to venv version
ln -s ~/mycroft-core/.venv/bin/precise-engine ~/.local/share/mycroft/precise/precise-engine/precise-engine

# Restart Mycroft
./start-mycroft.sh all
```

### Issue 2: Precise 0.3.0 Dependencies

Precise 0.3.0 requires specific dependencies that aren't automatically installed:

```bash
cd ~/mycroft-core
source .venv/bin/activate

# Install Precise with all dependencies
pip install mycroft-precise==0.3.0 --no-deps
pip install attrs bbopt fitipy pyache sonopy keras speechpy-fast wavio prettyparse==1.0.0

# Patch the prettyparse compatibility issue
sed -i '18s/from prettyparse import create_parser/from prettyparse import parse_args as create_parser/' \
  .venv/lib/python3.11/site-packages/precise/scripts/engine.py
```

**Note**: TensorFlow 2.12.0 works despite Precise claiming it needs 1.13.

### Issue 3: Model File Location

Use the correct path for modern Mycroft installations:

- ✅ **Correct**: `~/.local/share/mycroft/precise/your-model.pb`
- ❌ **Old/Wrong**: `~/.mycroft/precise/your-model.pb`

## Configuration

### Example 1: Single Custom Wake Word

Edit `~/.config/mycroft/mycroft.conf`:

```json
{
  "max_allowed_core_version": 21.2,
  "precise": {
    "executable": "/home/YOUR_USERNAME/mycroft-core/.venv/bin/precise-engine"
  },
  "hotwords": {
    "computer": {
      "module": "precise",
      "local_model_file": "~/.local/share/mycroft/precise/computer.pb",
      "sensitivity": 0.31,
      "trigger_level": 3
    }
  },
  "listener": {
    "wake_word": "computer"
  }
}
```

### Example 2: Multiple Wake Words

```json
{
  "max_allowed_core_version": 21.2,
  "precise": {
    "executable": "/home/YOUR_USERNAME/mycroft-core/.venv/bin/precise-engine"
  },
  "hotwords": {
    "hey mycroft": {
      "module": "precise",
      "phonemes": "HH EY . M AY K R AO F T",
      "threshold": 1e-90,
      "lang": "en-us"
    },
    "computer": {
      "module": "precise",
      "local_model_file": "~/.local/share/mycroft/precise/computer.pb",
      "sensitivity": 0.31,
      "trigger_level": 3
    }
  },
  "listener": {
    "wake_word": "computer"
  }
}
```

## Configuration Parameters

- **`module`**: Always `"precise"` for custom wake words
- **`local_model_file`**: Full path to your `.pb` model file
  - Use `~/.local/share/mycroft/precise/` for model storage
  - Tilde (`~`) expansion works in config
- **`sensitivity`**: Detection sensitivity (0.1 to 1.0)
  - Lower = more sensitive (more false positives)
  - Higher = less sensitive (fewer false positives)
  - Recommended: 0.3-0.5
- **`trigger_level`**: Consecutive detections needed (1-5)
  - Higher = more reliable, but slower response
  - Recommended: 3
- **`threshold`**: Alternative to sensitivity (for pocketsphinx compatibility)
  - Only used if sensitivity not specified

## Training Custom Wake Words

If you want to train your own models:

```bash
cd ~/mycroft-core
source .venv/bin/activate

# Collect audio samples (need ~50+ recordings of wake word)
precise-collect

# Train the model
precise-train -e 60 your-phrase.net /path/to/audio/samples/

# Convert to .pb format
precise-convert your-phrase.net

# Test the model
precise-listen your-phrase.pb
```

**Note**: Training requires significant audio samples and time. Consider using pre-trained models if available.

## Troubleshooting

### Wake Word Not Detected

1. **Check if precise-engine is running**:
   ```bash
   ps aux | grep precise-engine
   ```
   Should show 2 processes with your model file path.

2. **Check logs**:
   ```bash
   tail -f /var/log/mycroft/voice.log
   ```
   Look for: `Loading "your-phrase" wake word via precise`

3. **Verify model file exists**:
   ```bash
   ls -la ~/.local/share/mycroft/precise/*.pb
   ```

4. **Test model directly**:
   ```bash
   cd ~/mycroft-core
   source .venv/bin/activate
   # Speak into mic while this runs
   arecord -f S16_LE -r 16000 -c 1 -t raw | \
     precise-engine ~/.local/share/mycroft/precise/your-model.pb 2048
   ```
   Should output confidence scores (0.0 to 1.0) as you speak.

### "Could not create hotword" Error

This usually means:
1. Model file path is wrong
2. Precise dependencies missing
3. Old binary being used instead of venv version

**Fix**: Follow the symlink solution in Issue 1 above.

### Precise Engine Crashes

Check for:
1. TensorFlow compatibility: `pip list | grep tensorflow`
   - Should be 2.12.0
2. Missing dependencies: Run the full dependency install from Issue 2
3. Corrupted model file: Re-download or retrain

## First-Time Setup Checklist

**If you answered YES to custom wake words during `dev_setup.sh`:**
- [x] Install Precise dependencies in venv *(done by setup script)*
- [x] Patch prettyparse compatibility *(done by setup script)*
- [x] Create symlink to prevent binary override *(done by setup script)*
- [ ] Copy your custom model file to `~/.local/share/mycroft/precise/`
- [ ] Update `~/.config/mycroft/mycroft.conf` with model path
- [ ] Restart Mycroft services
- [ ] Verify precise-engine is running
- [ ] Test wake word detection

**If you answered NO but now want custom wake words:**
- [ ] Follow manual installation steps in "Issue 2" above
- [ ] Follow remaining checklist items

## Why These Issues Happen

- **Precise 0.2.0 Download**: Mycroft's default config points to an old repository that has broken binaries
- **TensorFlow Incompatibility**: Precise was built for TensorFlow 1.13 (Python 3.5/3.6 era), but works with 2.12.0 despite warnings
- **prettyparse Issue**: The PyPI version of prettyparse changed its API, breaking Precise 0.3.0's imports
- **Path Changes**: Mycroft moved from `~/.mycroft/` to XDG-compliant `~/.local/share/mycroft/` in newer versions

## Success Indicators

When everything is working, you should see:

```
INFO | Loading "your-phrase" wake word via precise
INFO | Speech client is ready.
INFO | Emitted mycroft.ready event - Mycroft is all loaded and ready to roll!
```

And `ps aux | grep precise-engine` should show your model loaded.

