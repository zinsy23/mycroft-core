#!/bin/bash
# Quick validation test for Mycroft on clean systems
# Run this after dev_setup.sh completes

echo "=== Quick Mycroft Validation Test ==="
echo ""

# Clear old logs to ensure we're testing fresh
echo "Clearing old logs..."
sudo rm -f /var/log/mycroft/voice.log
sudo rm -f /var/log/mycroft/skills.log
sudo rm -f /var/log/mycroft/audio.log

# Start services
echo "Starting Mycroft services..."
cd /home/joseph/mycroft-core
./stop-mycroft.sh all > /dev/null 2>&1
sleep 2
./start-mycroft.sh all > /dev/null 2>&1

# Wait for initialization
echo "Waiting 20 seconds for services to initialize..."
sleep 20

# Check for errors
echo ""
echo "=== Test Results ==="

ERRORS=0

# Test 1: Check for the old AttributeError
if grep -q "AttributeError.*key_phrase" /var/log/mycroft/voice.log 2>/dev/null; then
    echo "❌ FAIL: Found key_phrase AttributeError"
    ERRORS=$((ERRORS + 1))
else
    echo "✅ PASS: No key_phrase AttributeError"
fi

# Test 2: Check for empty model name error
if grep -q 'Loading "" wake word' /var/log/mycroft/voice.log 2>/dev/null; then
    echo "❌ FAIL: Tried to load empty wake word name"
    ERRORS=$((ERRORS + 1))
else
    echo "✅ PASS: No empty wake word loading attempts"
fi

# Test 3: Check if voice service started
if grep -q "Speech client is ready" /var/log/mycroft/voice.log 2>/dev/null; then
    echo "✅ PASS: Voice service started successfully"
else
    echo "❌ FAIL: Voice service did not start"
    ERRORS=$((ERRORS + 1))
fi

# Test 4: Check if our fix is working
if grep -q "stand_up_word is empty - skipping wakeup recognizer" /var/log/mycroft/voice.log 2>/dev/null; then
    echo "✅ PASS: Wakeup recognizer properly skipped (pocketsphinx not available)"
else
    echo "⚠️  INFO: Wakeup recognizer log not found (may have pocketsphinx)"
fi

# Test 5: Check if Precise loaded
if grep -q "Loading \"hey mycroft\" wake word via precise" /var/log/mycroft/voice.log 2>/dev/null; then
    echo "✅ PASS: Precise wake word engine loaded"
else
    echo "❌ FAIL: Precise wake word engine did not load"
    ERRORS=$((ERRORS + 1))
fi

# Test 6: Check if processes are running
if ps aux | grep -v grep | grep -q "mycroft.client.speech"; then
    echo "✅ PASS: Voice process is running"
else
    echo "❌ FAIL: Voice process is not running"
    ERRORS=$((ERRORS + 1))
fi

echo ""
if [ $ERRORS -eq 0 ]; then
    echo "🎉 ALL TESTS PASSED! Mycroft is working correctly."
    echo ""
    echo "Next steps:"
    echo "  1. Run: mycroft-cli-client"
    echo "  2. Say: 'hey mycroft'"
    echo "  3. Check if mic levels respond and wake word is detected"
    exit 0
else
    echo "⚠️  $ERRORS test(s) failed. Check logs:"
    echo "  sudo tail -100 /var/log/mycroft/voice.log"
    exit 1
fi

