# Custom Wake Word Setup Guide

This guide covers using **existing Precise `.pb` model files** with Mycroft.

For **training new wake words**, see [OPENWAKEWORD_SETUP.md](OPENWAKEWORD_SETUP.md) (recommended - works in Python 3.11).

## Quick Start (TL;DR)

### 1. Run Setup Script
```bash
./dev_setup.sh
# Answer YES when asked: "Do you plan to use custom wake word models?"
# Select "Precise" if you have an existing .pb model file
```

### 2. Add Your Model File
```bash
# Replace "computer" with your custom wake word phrase (e.g., "jarvis", "hey robot", etc.)
cp /path/to/your/computer.pb ~/.local/share/mycroft/precise/
```

### 3. Configure Mycroft
Edit `~/.config/mycroft/mycroft.conf` (replace "computer" with your wake word):

```json
{
  "max_allowed_core_version": 21.2,
  "precise": {
    "executable": "~/.local/share/mycroft/precise/precise-engine/precise-engine"
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

**Note**: Replace all instances of `"computer"` with your custom wake word phrase.

### 4. Start and Test
```bash
cd ~/mycroft-core
./start-mycroft.sh all

# Verify it's working (replace "computer" with your wake word)
ps aux | grep precise-engine
# Should see: precise-engine /home/user/.local/share/mycroft/precise/computer.pb 2048
```

Say your custom wake word and it should respond!

---

## Why This Setup is Needed

**The Problem**: The `mycroft-precise` Python package (0.3.0) requires TensorFlow 1.13, which doesn't work on Python 3.11.

**The Solution**: Use the pre-built Precise 0.3.0 binary, which has TensorFlow bundled inside.

**What Changed from Old Mycroft**:
- ❌ **OLD**: Install `mycroft-precise` in venv, use Python script
- ✅ **NEW**: Download pre-built binary, configure Mycroft to use it

---

## Detailed Setup Instructions

### Prerequisites

- Mycroft installed via `dev_setup.sh`
- Custom wake word model file (`.pb` format)
- Python 3.11+ virtual environment

### Step 1: Download Precise 0.3.0 Binary

**If you answered YES during setup**, this is already done. Otherwise:

```bash
cd ~/.local/share/mycroft/precise

# For x86_64 (most desktops/laptops)
wget https://github.com/MycroftAI/mycroft-precise/releases/download/v0.3.0/precise-engine_0.3.0_x86_64.tar.gz
tar -xzf precise-engine_0.3.0_x86_64.tar.gz

# For aarch64 (Raspberry Pi)
wget https://github.com/MycroftAI/mycroft-precise/releases/download/v0.3.0/precise-engine_0.3.0_aarch64.tar.gz
tar -xzf precise-engine_0.3.0_aarch64.tar.gz

# Verify
~/.local/share/mycroft/precise/precise-engine/precise-engine --version
# Should output: 0.3.0
```

### Step 2: Place Your Model File

```bash
# Copy your custom wake word model
cp /path/to/your-model.pb ~/.local/share/mycroft/precise/

# Verify it exists
ls -lh ~/.local/share/mycroft/precise/*.pb
```

### Step 3: Configure Mycroft

Edit `~/.config/mycroft/mycroft.conf` (replace "computer" with your custom wake word):

**Single Custom Wake Word Example**:
```json
{
  "max_allowed_core_version": 21.2,
  "precise": {
    "executable": "~/.local/share/mycroft/precise/precise-engine/precise-engine"
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

**Multiple Wake Words Example** (using both default "hey mycroft" and custom "computer"):
```json
{
  "max_allowed_core_version": 21.2,
  "precise": {
    "executable": "~/.local/share/mycroft/precise/precise-engine/precise-engine"
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

**Note**: In both examples, replace `"computer"` with your custom wake word phrase.

**Configuration Parameters**:
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

### Step 4: Start Mycroft

```bash
cd ~/mycroft-core
./start-mycroft.sh all
```

### Step 5: Verify It's Working

```bash
# Check precise-engine is running
ps aux | grep precise-engine
# Should see TWO processes:
# /home/user/.local/share/mycroft/precise/precise-engine/precise-engine /home/user/.local/share/mycroft/precise/computer.pb 2048

# Check logs
tail -f /var/log/mycroft/voice.log
# Should see: "Using custom Precise executable: ~/.local/share/mycroft/precise/precise-engine/precise-engine"
# Should see: "Loading 'computer' wake word via precise"
# Should see: "Speech client is ready."
```

---

## Troubleshooting

### Wake Word Not Detected

**Check if precise-engine is running**:
```bash
ps aux | grep precise-engine
```
Should show 2 processes with your model file path.

**Check logs for errors**:
```bash
tail -50 /var/log/mycroft/voice.log
```
Look for: `Loading "your-phrase" wake word via precise`

**Verify model file exists**:
```bash
ls -la ~/.local/share/mycroft/precise/*.pb
```

**Test model directly**:
```bash
# Speak into mic while this runs
arecord -f S16_LE -r 16000 -c 1 -t raw | \
  ~/.local/share/mycroft/precise/precise-engine/precise-engine \
  ~/.local/share/mycroft/precise/your-model.pb 2048
```
Should output confidence scores (0.0 to 1.0) as you speak.

### "Could not create hotword" Error

**For "wake up" stand_up_word**:
This is expected if pocketsphinx didn't compile on your system. It only affects the secondary "wake up" word, not your main wake word. Safe to ignore.

**For your main wake word**:
- Check that `"executable"` path is correct in config
- Verify the binary exists and is executable
- Check that model file path is correct

### Precise Engine Crashes / Zombie Process

**Symptoms**: `ps aux` shows `[precise-engine] <defunct>`

**Cause**: Using the venv Python package instead of the binary

**Fix**: Make sure your config points to the binary:
```json
"precise": {
  "executable": "~/.local/share/mycroft/precise/precise-engine/precise-engine"
}
```

NOT the venv version:
```json
"precise": {
  "executable": "/home/user/mycroft-core/.venv/bin/precise-engine"  // ❌ DON'T USE THIS
}
```

### Mycroft Downloads Old 0.2.0 Binary

**Symptoms**: Logs show "Downloading Precise executable..." and it stops working

**Cause**: The `"executable"` config isn't being respected (old Mycroft version)

**Fix**: Make sure you have the updated `hotword_factory.py` with executable config support. This was added as new functionality.

---

## Training Custom Wake Words

⚠️ **IMPORTANT**: Precise training is NOT supported in the Python 3.11 Mycroft environment.

### Why Precise Training Doesn't Work Here

The Precise training tools (`precise-train`, `precise-collect`, `precise-convert`) require:
- Python 3.7 (or 3.6-3.8)
- TensorFlow 1.13 (incompatible with Python 3.11)

The Mycroft environment uses Python 3.11+ with only the Precise **runtime** binary (no training tools).

### Options for Training Precise Models

**Option 1: Use OpenWakeWord Instead (Recommended)**

OpenWakeWord fully supports training in the Python 3.11 environment with GPU acceleration:
```bash
cd ~/mycroft-core
source .venv/bin/activate
oww-train-model  # Interactive mode, prompts for all parameters
```

See [OPENWAKEWORD_SETUP.md](OPENWAKEWORD_SETUP.md) for complete training guide.

**Option 2: Separate Python 3.7 Environment for Precise Training (Advanced)**

If you specifically need to train Precise models, you'll need a separate Python 3.7 environment.

See the official Precise training repository:
- **GitHub**: https://github.com/MycroftAI/mycroft-precise
- **Training Guide**: https://github.com/MycroftAI/mycroft-precise/wiki/Training-your-own-wake-word

Basic setup outline:
```bash
# Create separate Python 3.7 environment (outside mycroft-core)
cd ~
python3.7 -m venv precise-training-env
source precise-training-env/bin/activate

# Install Precise training tools
pip install mycroft-precise==0.3.0

# Now you can use training commands
precise-collect
precise-train -e 60 your-phrase.net /path/to/audio/samples/
precise-convert your-phrase.net
```

Once trained, copy the `.pb` model to `~/.local/share/mycroft/precise/` and configure it in mycroft.conf.

**Note**: This requires Python 3.7 installation, which may not be available on modern systems. OpenWakeWord is recommended for most users.

---

## Architecture Support

- **x86_64**: ✅ Fully supported
- **aarch64** (Raspberry Pi): ✅ Fully supported  
- **Other architectures**: May require manual binary compilation

---

## Technical Details

### Why the Python Package Doesn't Work

The `mycroft-precise` Python package (0.3.0) has these dependencies:
- TensorFlow 1.13 (incompatible with Python 3.11)
- Uses `tensorflow.GraphDef()` which doesn't exist in TensorFlow 2.x
- `prettyparse` API changed, breaking imports

The pre-built binary:
- Has TensorFlow 1.13 bundled inside (isolated from system Python)
- Works on Python 3.11 systems
- No dependency conflicts

### What dev_setup.sh Does

When you answer YES to custom wake words:
1. Downloads Precise 0.3.0 pre-built binary for your architecture
2. Extracts to `~/.local/share/mycroft/precise/precise-engine/`
3. Makes it executable

It does NOT:
- Install `mycroft-precise` Python package
- Install TensorFlow
- Create symlinks

### Key Code Changes

**`hotword_factory.py`** (lines 260-276):
- Added support for `"precise": {"executable": "..."}` config
- This was NEW functionality - never existed in upstream Mycroft
- Prevents automatic download/override when custom executable is specified

**`listener.py`** (lines 377-383):
- Fixed handling of empty `stand_up_word` when pocketsphinx unavailable
- Returns `None` instead of trying to load invalid model

---

## Success Indicators

When everything is working correctly:

**Logs show**:
```
INFO | Using custom Precise executable: ~/.local/share/mycroft/precise/precise-engine/precise-engine
INFO | Loading "computer" wake word via precise
INFO | Speech client is ready.
INFO | Emitted mycroft.ready event - Mycroft is all loaded and ready to roll!
```

**Process list shows**:
```bash
$ ps aux | grep precise-engine
/home/user/.local/share/mycroft/precise/precise-engine/precise-engine /home/user/.local/share/mycroft/precise/computer.pb 2048
```

**Wake word detection works**: Say your wake word and Mycroft responds!
