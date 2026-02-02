# Mycroft Core - Offline Voice Assistant

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE.md)

## 🎯 **Project Status: FULLY WORKING OFFLINE**

**Mycroft Core has been restored to full functionality** after the Mycroft.ai backend shutdown. This fork provides a **completely offline voice assistant** that works without any external services.

### ✨ **What Works Now:**
- ✅ **Voice Commands**: "Hey Mycroft, tell me a joke"
- ✅ **Local STT**: OVOS FasterWhisper (Whisper model)
- ✅ **Local TTS**: eSpeak (reliable offline speech)
- ✅ **Wake Word**: Precise engine (default "hey mycroft")
- ✅ **Skills**: Offline-compatible skills auto-installed
- ✅ **No Backend**: Completely independent operation

### 🔧 **What Was Fixed:**
- **Backend Bypass**: All Mycroft.ai API calls bypassed
- **Threading Issues**: Python 3.11+ compatibility restored
- **Service Readiness**: Enclosure service timeout resolved
- **Dependencies**: Local alternatives for all external services

### ℹ️ **Known Installation Notes:**
- **Pocketsphinx**: May fail on x86_64 with modern GCC (C23 issue) - this is OK, Precise wake word engine is used instead
- **Python 3.11+**: Installed via `uv` if not present (no system packages needed)
- **TensorFlow**: Optional, only needed for custom wake word training
- **Precise Model**: Pre-downloaded during setup to avoid runtime network issues

---

## 🚀 **Quick Start (Offline Setup)**

### **1. Clone and Setup**
```bash
cd ~/
git clone --depth=1 https://github.com/YOUR_USERNAME/mycroft-core.git
cd mycroft-core
bash dev_setup.sh
```

### **2. Start Mycroft**
```bash
./start-mycroft.sh all
```

**Note**: Mycroft boot time varies by system:
- **Fast systems**: ~15-30 seconds
- **Raspberry Pi**: ~1-2 minutes
- **Slow systems**: May take longer

**How to know when it's ready**: Look for the messagebus event "Mycroft is all loaded and ready to roll!" which appears in:
- **CLI mode**: `./start-mycroft.sh cli` (shows real-time messages)
- **Log files**: `tail -f /var/log/mycroft/messagebus.log`
- **Background mode**: Check logs with `tail -f /var/log/mycroft/*.log`

**No audio confirmation** - this is a text message from the messagebus service.

### **3. Test Voice Commands**
Say: **"Hey Mycroft, tell me a joke"**

---

## 📋 **Installation Process**

### **Automated Setup (`dev_setup.sh`)**

The setup script handles everything automatically:

#### **Phase 0: Configuration Questions**
- 🎤 **Custom Wake Words**: Install TensorFlow for training custom models?
- 🔄 **GPIO Support**: Install Raspberry Pi hardware libraries?
- 🐍 **Python 3.11+**: Install DeadSnakes PPA for Ubuntu users?

#### **Phase 1: System Dependencies**
- OS detection and package manager selection
- System packages (build tools, audio libraries, etc.)
- Python 3.11+ installation (if requested)

#### **Phase 2: Python Environment**
- Virtual environment creation with optimal Python version
- Version stability verification and automatic upgrades
- Python package installation

#### **Phase 3: Mycroft Setup**
- Configuration creation (`~/.config/mycroft/mycroft.conf`)
- Skills installation (joke, date/time, hello-world, alarm, weather)
- Conditional package installation (TensorFlow, GPIO)

#### **Phase 4: Completion**
- All services stopped cleanly
- Ready for manual startup

---

## 📝 **Technical Notes**

### **Setup Time vs. Functionality Trade-off**

This setup takes longer than the original Mycroft installation because we're building a **robust, completely offline system** rather than a minimal setup that depended on external services.

#### **Original Setup (2018-era):**
- **Setup time**: ~5-10 minutes on Pi
- **Dependencies**: Minimal, relied on Mycroft.ai backend
- **STT/TTS**: Required internet for Google/cloud services
- **Functionality**: Limited without backend pairing

#### **Current Setup (Offline-first):**
- **Setup time**: ~15-25 minutes on Pi, ~2-5 minutes on fast x86_64
- **Dependencies**: Comprehensive, all local alternatives
- **STT/TTS**: Completely offline (OVOS FasterWhisper + eSpeak)
- **Functionality**: Full voice assistant without internet

#### **Why the Difference:**
- **More packages**: Installing robust alternatives to cloud services
- **Local compilation**: Some packages need ARM64 compilation
- **Offline dependencies**: Building a self-contained system
- **Future-proofing**: Modern Python packages for better compatibility

**Result**: Longer initial setup for **completely independent operation** - no internet required for any functionality.

---

## 🎵 **Current Technology Stack**

### **Speech-to-Text (STT)**
- **Engine**: OVOS FasterWhisper
- **Model**: Whisper (local, ~300MB)
- **Quality**: High accuracy, offline operation
- **Language**: English (expandable)

### **Text-to-Speech (TTS)**
- **Engine**: eSpeak
- **Quality**: Clear, reliable, fast
- **Languages**: Multi-language support
- **Offline**: Completely local

### **Wake Word Detection**
- **Engine**: Precise (Mycroft's neural network)
- **Default**: "Hey Mycroft"
- **Custom**: Train your own (requires TensorFlow)
- **Accuracy**: High with proper training

### **Skills System**
- **Manager**: MSM (Mycroft Skills Manager)
- **Auto-install**: Offline-compatible skills
- **Updates**: Disabled (offline operation)
- **Development**: Full skill creation support

---

## 🎮 **Usage Instructions**

### **Starting Mycroft**
```bash
# Start all services
./start-mycroft.sh all

# Start with CLI (debug mode)
./start-mycroft.sh debug

# Start CLI only (if services already running)
./start-mycroft.sh cli
```

### **Stopping Mycroft**
```bash
# Stop all services
./stop-mycroft.sh all

# Stop specific service
./stop-mycroft.sh skills
```

### **Voice Commands**
- **"Hey Mycroft, tell me a joke"** - Joke skill
- **"Hey Mycroft, what time is it"** - Date/time skill
- **"Hey Mycroft, hello"** - Hello world skill
- **"Hey Mycroft, set an alarm for 8 AM"** - Alarm skill
- **"Hey Mycroft, what's the weather like"** - Weather skill (requires location config)

### **Custom Wake Word Configuration**

⚠️ **IMPORTANT**: Custom wake words require special setup on Python 3.11+.

**See [CUSTOM_WAKE_WORDS.md](CUSTOM_WAKE_WORDS.md)** for complete setup instructions, including:
- Quick start guide (TL;DR)
- Step-by-step setup
- Configuration examples
- Troubleshooting

#### **Quick Configuration Example**

```json
{
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

**Note**: The old path `/home/pi/.mycroft/precise/` is deprecated. Use `~/.local/share/mycroft/precise/` instead.

### **Configuration Management**
```bash
# Edit user configuration
./bin/mycroft-config edit user

# View current configuration
./bin/mycroft-config show user

# List all configuration options
./bin/mycroft-config show
```

### **Skill Development**
```bash
# Create new skill (MSK functionality restored)
./bin/mycroft-msk create my-skill

# Install skill from directory
./bin/mycroft-msk install /path/to/skill

# List installed skills
./bin/mycroft-msk list
```

---

## ⚙️ **Configuration**

### **Main Configuration File**
Location: `~/.config/mycroft/mycroft.conf`

#### **Key Settings**
```json
{
  "hotwords": {
    "hey mycroft": {
      "module": "precise",
      "threshold": 1e-90
    }
  },
  "stt": {
    "module": "ovos-stt-plugin-fasterwhisper",
    "ovos-stt-plugin-fasterwhisper": {
      "model": "base.en",
      "use_cuda": false
    }
  },
  "tts": {
    "module": "espeak"
  },
  "skills": {
    "auto_update": false,
    "upload_skill_manifest": false
  }
}
```

### **Location Configuration (Optional)**
Add to `mycroft.conf` for weather skill:
```json
{
  "location": {
    "city": {
      "name": "Your City",
      "state": {
        "name": "Your State",
        "country": {
          "name": "Your Country"
        }
      }
    },
    "coordinate": {
      "latitude": YOUR_LAT,
      "longitude": YOUR_LONG
    },
    "system_unit": "metric"
  }
}
```

---

## 🔧 **Troubleshooting**

### **Common Issues**

#### **Microphone Not Working**

**Most common issue: Wrong default microphone selected**

```bash
# 1. List all available microphones
pactl list sources short

# 2. Check which one is currently default
pactl info | grep "Default Source"

# 3. Set the correct microphone as default
pactl set-default-source SOURCE_NAME_FROM_STEP_1

# Example:
pactl set-default-source alsa_input.usb-046d_C922_Pro_Stream_Webcam_46FC0ADF-02.analog-stereo

# 4. Restart Mycroft
cd ~/mycroft-core
./stop-mycroft.sh && ./start-mycroft.sh all
```

**To make it permanent**, edit `~/.config/mycroft/mycroft.conf` and add:
```json
{
  "listener": {
    "device_name": "C922",
    "sample_rate": 16000
  }
}
```
(Use a substring that matches your device name)

**Other issues:**
- **Voice service crashed**: Check `/var/log/mycroft/voice.log` for pocketsphinx errors
- **Microphone muted**: Run `pactl list sources | grep -A 10 "YOUR_DEVICE" | grep Mute`

#### **Wake Word Not Responding**
```bash
# Check Precise engine
ls -la ~/.local/share/mycroft/precise/

# Verify wake word configuration
./bin/mycroft-config show user | grep -A 10 hotwords

# Check which wake word engine is loaded
tail -f /var/log/mycroft/voice.log | grep -i "wake word"
```

**Note:** If you see pocketsphinx installation failures during setup, this is expected on modern x86_64 systems and won't affect functionality. Precise is the primary wake word engine and works perfectly.

#### **Skills Not Loading**
```bash
# Check skills directory
ls -la /opt/mycroft/skills/

# View skills service logs
tail -f /var/log/mycroft/skills.log
```

### **Log Files**
- **Skills**: `/var/log/mycroft/skills.log`
- **Audio**: `/var/log/mycroft/audio.log`
- **Voice**: `/var/log/mycroft/voice.log`
- **Message Bus**: `/var/log/mycroft/messagebus.log`

---

## 🎤 **Wake Word Engines**

### **Understanding Wake Word vs. Speech-to-Text**

Mycroft uses **two separate systems** for voice recognition:

| Component | Purpose | Engine Used | When It Runs |
|-----------|---------|-------------|--------------|
| **Wake Word Detection** | Listens for "Hey Mycroft" | Precise (default) | Continuously (24/7) |
| **Speech-to-Text (STT)** | Converts commands to text | FasterWhisper | After wake word detected |

**Important:** These are **independent systems**. FasterWhisper cannot be used for wake word detection.

### **Available Wake Word Engines**

#### **1. Precise (Default - Recommended)** ✅
- **Status**: Fully working, installed by default
- **Quality**: Excellent accuracy for "hey mycroft"
- **Requirements**: precise-runner (automatically installed)
- **Configuration**: Already configured in `~/.config/mycroft/mycroft.conf`

```json
"hotwords": {
  "hey mycroft": {
    "module": "precise",
    "threshold": 1e-90
  }
}
```

#### **2. Pocketsphinx (Fallback)** ⚠️
- **Status**: Optional, may fail on modern x86_64 systems
- **Quality**: Fair accuracy, higher false positive rate
- **Known Issue**: Fails to compile on x86_64 with GCC 13+ (C23 `bool` typedef conflict)
- **Works On**: ARM/Raspberry Pi systems (compiles successfully)
- **Fallback**: Mycroft automatically falls back to pocketsphinx if Precise fails

**Why pocketsphinx may fail:**
```
Error: 'bool' cannot be defined via 'typedef'
Cause: Modern GCC defaults to C23 standard where 'bool' is a keyword
Impact: Installation fails on x86_64 Debian/Ubuntu with GCC 13+
```

**This is OK because:**
- ✅ Precise is the primary engine (better quality)
- ✅ Setup continues without pocketsphinx (marked as optional)
- ✅ On Raspberry Pi, pocketsphinx installs successfully as backup
- ✅ Mycroft works perfectly with just Precise

#### **3. Alternative Wake Word Engines**

If Precise doesn't work for you, consider these alternatives:

| Engine | License | Quality | Installation |
|--------|---------|---------|--------------|
| **Porcupine** | Free tier available | Excellent | `pip install pvporcupine` |
| **openWakeWord** | Open source | Excellent | `pip install openwakeword` |
| **Snowboy** | Deprecated | Good | No longer maintained |

### **Wake Word Engine Fallback Chain**

Mycroft automatically tries engines in this order:

```
1. Precise (configured)
   ↓ (if fails)
2. Pocketsphinx (backup)
   ↓ (if fails)
3. Basic Pocketsphinx (last resort)
```

### **Testing Your Wake Word**

```bash
# Check which engine is loaded
tail -f /var/log/mycroft/voice.log | grep -i "wake word"

# Verify Precise model exists
ls -la ~/.local/share/mycroft/precise/

# Test wake word sensitivity
# Say "Hey Mycroft" and watch for detection in logs
tail -f /var/log/mycroft/voice.log
```

### **Pocketsphinx Installation Status**

During setup, you'll see one of these messages:

**On x86_64 (Debian/Ubuntu with GCC 13+):**
```
⚠️  pocketsphinx installation failed (known issue on x86_64 with GCC 13+)
   This is OK - Precise wake word engine will be used instead
   Primary wake word 'hey mycroft' (Precise) will still work perfectly
```

**On Raspberry Pi (ARM):**
```
✅ pocketsphinx installed - 'wake up' alternative wake word available
```

---

## 🐍 **Python Environment**

### **Virtual Environment**
- **Location**: `.venv/` in project directory
- **Python**: Automatically selected (3.11+ preferred)
- **Activation**: `source .venv/bin/activate`

### **Key Dependencies**
- **Core**: mycroft-messagebus-client, lingua-franca
- **Audio**: PyAudio, SpeechRecognition
- **ML**: precise-runner, tensorflow (if custom wake words)
- **STT**: ovos-stt-plugin-fasterwhisper
- **TTS**: espeak integration

---

## 🌟 **Advanced Features**

### **Custom Wake Word Training**
If you installed TensorFlow during setup:
```bash
# Train custom wake word
precise-train -w "your phrase" /path/to/audio/samples

# Test wake word
precise-listen -w "your phrase" /path/to/model.pb
```

### **GPIO Integration (Raspberry Pi)**
If you installed GPIO support:
```python
# Example skill with GPIO
import RPi.GPIO as GPIO
GPIO.setmode(GPIO.BCM)
GPIO.setup(18, GPIO.OUT)
GPIO.output(18, GPIO.HIGH)
```

### **Skill Development**
```bash
# Create skill structure
./bin/mycroft-msk create my-skill

# Install development dependencies
pip install -r requirements/requirements.txt

# Test skill
./start-mycroft.sh debug
```

---

## 📚 **Documentation & Resources**

### **Original Mycroft Resources**
- [Skill Development Guide](https://mycroft-ai.gitbook.io/docs/skill-development/your-first-skill)
- [API Documentation](https://mycroft-core.readthedocs.io/)
- [Community Forum](https://community.mycroft.ai/)

### **Offline Operation Notes**
- **No Internet Required**: All services run locally
- **No Backend Dependencies**: Completely self-contained
- **No API Keys**: All functionality works offline
- **No Updates**: Skills and core are static

---

## ⚡ **Performance Optimization (GPU Acceleration)**

### **Understanding the Voice Processing Pipeline**

When you speak to Mycroft, your voice goes through several stages:

1. **🎤 Audio Capture** (PyAudio) - Microphone input, typically ~30ms latency
2. **👂 Wake Word Detection** (Precise) - Listens for "Hey Mycroft" using a lightweight neural network, ~10-50ms per audio chunk
3. **🔴 Recording** - Captures your command after wake word detected
4. **🔇 Voice Activity Detection (VAD)** - Waits for silence (~0.5-1.0s) to know you're done speaking
5. **💬 Speech-to-Text (STT)** - **BOTTLENECK: ~0.9-1.4s on CPU** - Transcribes audio using FasterWhisper (Whisper neural network)
6. **🧠 Intent Matching** (Padatious) - Determines what skill to use, ~10-50ms
7. **🔊 Text-to-Speech (TTS)** (eSpeak) - Generates response audio, ~100-300ms

**The slowest component is Speech-to-Text (STT)**, which runs the Whisper neural network to transcribe your voice command. This is where GPU acceleration makes the biggest difference.

### **GPU Acceleration for Speech-to-Text**

FasterWhisper (the STT engine) can use NVIDIA GPU acceleration to **significantly reduce transcription time**. Performance gains vary depending on your GPU model and the Whisper model size you choose.

#### **Requirements:**

✅ **NVIDIA GPU** with compute capability **6.0+** (Pascal architecture or newer)
   - ✓ Works: GTX 1000 series, RTX 2000/3000/4000 series, Tesla P/V/A series
   - ✗ Too old: GTX 700/900 series (Maxwell/Kepler architecture)

✅ **System CUDA libraries** installed at `/usr/local/cuda`
   - Cannot be installed via pip - must be system-wide installation
   - Ubuntu/Debian: `sudo apt install nvidia-cuda-toolkit`
   - Check installation: `nvcc --version` should show CUDA 11.x or 12.x

✅ **Sufficient VRAM** for the model:
   - `tiny.en` - ~100 MB (fast, lower accuracy)
   - `base.en` - ~220 MB (default, good balance)
   - `small.en` - ~500 MB (better accuracy)
   - `medium.en` - ~870 MB (best accuracy for English)

**Note:** The setup script already includes GPU support in `start-mycroft.sh`. If you don't have NVIDIA/CUDA, the path is simply ignored (no errors).

**For GPU-accelerated training** (custom wake words), see the [GPU Acceleration for Training](#gpu-acceleration-for-training-pytorchcuda) section below.

#### **Setup Steps:**

1. **Verify CUDA installation:**
   ```bash
   # Check if CUDA is installed
   ls /usr/local/cuda/lib64/libcudart.so

   # Check GPU compute capability
   nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader
   ```

2. **Edit Mycroft configuration** (`~/.config/mycroft/mycroft.conf`):
   ```json
   "stt": {
     "module": "ovos-stt-plugin-fasterwhisper",
     "ovos-stt-plugin-fasterwhisper": {
       "model": "base.en",
       "language": "en",
       "use_cuda": true
     }
   }
   ```

3. **Restart Mycroft:**
   ```bash
   ./stop-mycroft.sh all
   ./start-mycroft.sh all
   ```

4. **Verify GPU usage** (say a voice command, then immediately check):
   ```bash
   nvidia-smi --query-compute-apps=pid,used_memory --format=csv
   ```

   You should see the Mycroft voice process using GPU memory (~220 MB for base.en, ~870 MB for medium.en).

#### **Performance Example (RTX 3060):**

Tested on NVIDIA GeForce RTX 3060 with i7-4790 CPU:

| Model | CPU Time | GPU Time | Speedup | Accuracy | VRAM |
|-------|----------|----------|---------|----------|------|
| base.en | ~0.9-1.0s | ~0.09-0.11s | ~10x | Good | ~220 MB |
| medium.en | ~6.0s* | ~0.3-0.5s | ~15x+ | Best | ~870 MB |

*CPU time for medium.en is estimated; actual GPU time was measured at 0.3-0.5s for 2-3 second audio clips.

**Your mileage will vary** based on:
- GPU model and VRAM
- CPU speed (affects CPU baseline)
- Audio length and complexity
- Background noise and accent clarity

**General guidance:**
- **Low-end GPUs** (GTX 1050, 1650): Expect 3-5x speedup with base.en
- **Mid-range GPUs** (GTX 1660, RTX 2060): Expect 5-8x speedup with small.en/medium.en
- **High-end GPUs** (RTX 3060+, 4070+): Expect 10x+ speedup, can comfortably run medium.en

**Finding Your Balance:**

The best model depends on your GPU and accuracy needs. We recommend experimenting:

1. **Start with `base.en`** - Good baseline for testing GPU acceleration
2. **Check the speed** - Say a few commands and note the response time
3. **Evaluate accuracy** - Does it correctly transcribe your commands?
4. **If happy with speed but want better accuracy** → Try the next larger model (`small.en` → `medium.en`)
5. **If too slow** → Drop back to the previous smaller model
6. **Repeat** until you find the sweet spot between speed and accuracy

**Example progression:**
- `base.en` → Fast but occasionally mishears? → Try `small.en`
- `small.en` → Good accuracy, still fast? → Try `medium.en`
- `medium.en` → Noticeably slower? → Stick with `small.en`

The goal is **instant-feeling responses** with **accurate transcription** for your voice/environment.

#### **Troubleshooting:**

**GPU not being used?**
```bash
# Check if voice service has correct library path
ps aux | grep "mycroft.client.speech"  # Get PID
cat /proc/PID/environ | tr '\0' '\n' | grep LD_LIBRARY_PATH
# Should include: /usr/local/cuda/lib64
```

**CUDA errors?**
- Verify compute capability is 6.0+ with `nvidia-smi`
- Ensure CUDA version matches driver version
- Check `/var/log/mycroft/voice.log` for error messages

---

### **GPU Acceleration for Training (PyTorch/CUDA)**

**Important Distinction:** There are two separate GPU acceleration systems in Mycroft:

| Feature | Runtime Inference (STT) | Training (Wake Words) |
|---------|------------------------|----------------------|
| **Status** | ✅ Included by default | ⚠️ Optional (requires setup) |
| **Library** | `ctranslate2` | `PyTorch` |
| **Installed?** | Yes (with FasterWhisper) | No (manual installation) |
| **Purpose** | GPU speech-to-text | Train custom wake word models |
| **CUDA Source** | System libs (`/usr/lib/`) | Bundled with PyTorch |
| **Venv Packages** | `faster-whisper`, `ctranslate2` | `torch`, `torchvision`, `torchaudio` |
| **Config** | `"use_cuda": true` in STT | Install via pip (see below) |
| **When Needed** | Every voice command | Only when training models |

**Key Points:**
- ✅ **Runtime STT GPU acceleration works immediately** after enabling in config - no extra packages needed
- ❌ **Training GPU acceleration requires PyTorch installation** - not included by default
- 🎯 **If you only use Mycroft (not training), you don't need PyTorch**
- 📦 **Both can coexist** - ctranslate2 for inference, PyTorch for training

If you want to train custom wake word models (OpenWakeWord or Precise), you'll benefit from GPU acceleration during training. This requires installing **PyTorch with CUDA support**.

#### **Determining Your CUDA Version**

**Step 1: Check your NVIDIA driver version**
```bash
nvidia-smi
```

Look at the top right corner for driver version:
```
Driver Version: 535.154.05    CUDA Version: 12.2
```

**Step 2: Match driver to compatible CUDA versions**

Your **driver version** determines which CUDA versions you can use:

| Driver Version | Supported CUDA Versions | Recommended PyTorch CUDA |
|----------------|-------------------------|--------------------------|
| 450.x - 524.x  | CUDA 11.x only          | `cu118` (CUDA 11.8)      |
| 525.x - 536.x  | CUDA 11.x or 12.x       | `cu121` or `cu124`       |
| 537.x+         | CUDA 11.x or 12.x       | `cu124` (CUDA 12.4)      |

**Important:** The "CUDA Version" shown by `nvidia-smi` is the **maximum** CUDA version your driver supports, not what's currently installed. You can use any CUDA version up to that maximum.

**Step 3: Check what system CUDA is installed (if any)**
```bash
nvcc --version  # Shows installed CUDA toolkit version (if installed)
```

**Note:** You don't need to match PyTorch's CUDA version with system CUDA. PyTorch bundles its own CUDA runtime libraries.

#### **Installing PyTorch with CUDA**

Activate your virtual environment first:
```bash
source .venv/bin/activate
```

**For modern drivers (525+) - Use CUDA 12.4:**
```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
```

**For older drivers (450-524) - Use CUDA 11.8:**
```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
```

**For CPU-only (no GPU):**
```bash
pip install torch torchvision torchaudio
```

#### **Verifying PyTorch CUDA Installation**

```bash
python -c "import torch; print(f'PyTorch version: {torch.__version__}'); print(f'CUDA available: {torch.cuda.is_available()}'); print(f'CUDA version: {torch.version.cuda if torch.cuda.is_available() else \"N/A\"}')"
```

Expected output with GPU:
```
PyTorch version: 2.6.0+cu124
CUDA available: True
CUDA version: 12.4
```

#### **Complete Setup for OpenWakeWord Training**

If you want to train custom OpenWakeWord models with GPU acceleration, you need:

**1. Install PyTorch with CUDA** (see instructions above)

**2. Install additional dependencies:**
```bash
source .venv/bin/activate
pip install speechbrain  # Audio processing for training
```

**3. Copy training scripts** (if not already present):
```bash
# Training scripts should be in openwakeword/ directory
# If missing, you need the oww-train-model.py, oww-collect.py, etc.
```

**4. Verify GPU is available:**
```bash
python -c "import torch; print(f'GPU available: {torch.cuda.is_available()}')"
# Should print: GPU available: True
```

**5. Start training:**
```bash
# See OPENWAKEWORD_SETUP.md for complete training workflow
oww-train-model --help
```

#### **GPU Requirements for Training**

**OpenWakeWord Training (recommended for custom wake words):**
- **Minimum:** 4GB VRAM (GTX 1650, RTX 2060)
- **Recommended:** 6GB+ VRAM (RTX 3060, RTX 4060)
- Training time: 3-10 minutes per 1000 epochs with GPU vs 30-60 minutes on CPU
- Dependencies: PyTorch + CUDA, speechbrain
- See `OPENWAKEWORD_SETUP.md` for complete training instructions

**Precise Training (TensorFlow-based):**
- **Minimum:** 6GB VRAM (GTX 1660, RTX 2060)
- **Recommended:** 8GB+ VRAM (RTX 3070, RTX 4070)
- Training time: 10-30 minutes per session with GPU vs 2-4 hours on CPU
- Dependencies: TensorFlow (installed if you chose Precise during setup)
- See `CUSTOM_WAKE_WORDS.md` for training instructions

#### **CUDA Version Compatibility Matrix**

| Component | CUDA 11.8 | CUDA 12.1 | CUDA 12.4 | Notes |
|-----------|-----------|-----------|-----------|-------|
| **PyTorch 2.6+** | ✅ | ✅ | ✅ | All versions supported |
| **ONNX Runtime** | ✅ | ✅ | ✅ | Runtime for OpenWakeWord models |
| **TensorFlow 2.12** | ✅ | ⚠️ | ⚠️ | Prefers CUDA 11.8 (Precise training) |
| **FasterWhisper** | ✅ | ✅ | ✅ | Uses system CUDA libs, not PyTorch |

**Recommendations:**
- **New setups with driver 525+:** Use CUDA 12.4 (best performance, latest features)
- **Older drivers (450-524):** Use CUDA 11.8 (maximum compatibility)
- **Mixed workloads (Precise + OpenWakeWord):** Use CUDA 11.8 (best TensorFlow compatibility)

#### **Troubleshooting CUDA Installation**

**"CUDA out of memory" errors during training:**
- Reduce batch size in training scripts
- Use a smaller model architecture
- Close other GPU applications (browsers, games)
- Check VRAM usage: `nvidia-smi`

**PyTorch can't find CUDA:**
```bash
# Check if you accidentally installed CPU-only version
pip show torch | grep Version
# Should show: Version: 2.6.0+cu124 (or cu118/cu121)
# If just "2.6.0" with no +cu*, reinstall with correct index URL
```

**Multiple CUDA versions causing conflicts:**
- PyTorch bundles its own CUDA runtime - this is normal and expected
- Different packages can use different CUDA versions without conflicts
- System CUDA (`/usr/local/cuda`) is only needed for FasterWhisper STT

**AMD GPU users:**
- AMD GPUs don't support CUDA (NVIDIA proprietary)
- Limited support via ROCm (experimental, not covered here)
- Recommend CPU-only training or cloud GPU services

---

## 🤝 **Contributing**

This is a **working fork** of Mycroft Core. Contributions are welcome:

1. **Fork** this repository
2. **Create** a feature branch
3. **Make** your changes
4. **Test** thoroughly
5. **Submit** a pull request

### **Development Setup**
```bash
# Clone your fork
git clone --depth=1 https://github.com/YOUR_USERNAME/mycroft-core.git
cd mycroft-core

# Setup development environment
bash dev_setup.sh

# Make changes and test
./start-mycroft.sh debug
```

---

## 📄 **License**

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE.md) file for details.

---

## 🙏 **Acknowledgments**

- **Mycroft AI Team**: Original Mycroft Core development
- **Open Voice OS**: Inspiration for offline operation
- **Community Contributors**: Bug fixes and improvements
- **AI Assistants**: Help with restoration and documentation

---

**🎉 Mycroft Core is alive and working offline! Enjoy your voice assistant! 🎉**
