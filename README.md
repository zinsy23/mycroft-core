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

If you installed TensorFlow during setup, you can train and use custom wake words. Here's how to configure them:

#### **Training Custom Wake Words**
```bash
# Train a custom wake word (requires audio samples)
precise-train -w "your phrase" /path/to/audio/samples

# Test the trained model
precise-listen -w "your phrase" /path/to/model.pb
```

#### **Configuration Examples**

**Example 1: "Computer" Wake Word**
```json
{
  "hotwords": {
    "computer": {
      "module": "precise",
      "local_model_file": "/home/pi/.mycroft/precise/computer.pb",
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
