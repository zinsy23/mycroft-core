#!/bin/bash
# Quick test script for Mycroft first-boot and restart scenarios

echo "=== Mycroft Startup Test ==="
echo ""

# Test 1: Check if services start
echo "Test 1: Starting all services..."
cd /home/joseph/mycroft-core
./start-mycroft.sh all > /dev/null 2>&1
sleep 15

# Test 2: Check for critical errors in logs
echo "Test 2: Checking for errors in voice.log..."
if grep -q "AttributeError.*key_phrase" /var/log/mycroft/voice.log 2>/dev/null; then
    echo "❌ FAIL: Found key_phrase AttributeError"
    exit 1
else
    echo "✅ PASS: No key_phrase errors"
fi

if grep -q 'Could not found find model for  on precise' /var/log/mycroft/voice.log 2>/dev/null; then
    echo "❌ FAIL: Found empty model name error"
    exit 1
else
    echo "✅ PASS: No empty model name errors"
fi

# Test 3: Check if our fixes are working
echo "Test 3: Checking if fixes are active..."
if grep -q "stand_up_word is empty - skipping wakeup recognizer" /var/log/mycroft/voice.log 2>/dev/null; then
    echo "✅ PASS: Wakeup recognizer fix is working"
else
    echo "⚠️  WARNING: Wakeup recognizer fix log not found (may not be needed)"
fi

# Test 4: Check if voice service is running
echo "Test 4: Checking if voice service is responsive..."
if ps aux | grep -v grep | grep -q "mycroft.client.speech"; then
    echo "✅ PASS: Voice service is running"
else
    echo "❌ FAIL: Voice service is not running"
    exit 1
fi

# Test 5: Check mic level file (indicates listener is active)
echo "Test 5: Checking if listener is active..."
sleep 5
if [ -f "/tmp/mycroft_mic_level" ] || [ -f "/run/user/$(id -u)/mycroft/ipc/mic_level" ]; then
    echo "✅ PASS: Mic level file exists (listener active)"
else
    echo "⚠️  WARNING: Mic level file not found (may take longer to initialize)"
fi

echo ""
echo "=== Test Summary ==="
echo "If all tests passed, Mycroft should be working correctly!"
echo "Check 'mycroft-cli-client' to verify wake word detection."
