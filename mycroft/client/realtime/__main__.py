# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from threading import Lock
import os

from mycroft import dialog
from mycroft.enclosure.api import EnclosureAPI
from mycroft.client.realtime.realtime_loop import RealtimeRecognizerLoop
from mycroft.configuration import Configuration
from mycroft.identity import IdentityManager
from mycroft.lock import Lock as PIDLock  # Create/Support PID locking file
from mycroft.messagebus.message import Message
from mycroft.util import (
    create_daemon,
    reset_sigint_handler,
    start_message_bus_client,
    wait_for_exit_signal
)
from mycroft.util.log import LOG
from mycroft.util.process_utils import ProcessStatus, StatusCallbackMap

bus = None  # Mycroft messagebus connection
lock = Lock()
loop = None
config = None


def _initialize_mic_level_file_early():
    """Initialize the mic level file before engine loading so CLI can display it immediately."""
    try:
        mic_level_file = "/tmp/mycroft/ipc/mic_level"
        os.makedirs(os.path.dirname(mic_level_file), exist_ok=True)

        # Write initial mic level data with default threshold (300 from speech_recognition.Recognizer)
        # Format matches ResponsiveRecognizer._initialize_mic_level_file()
        with open(mic_level_file, 'w') as f:
            f.write('Energy:  cur=0 thresh={:.3f} muted=0'.format(300))

        LOG.debug("Initialized mic level file early for CLI display")
    except Exception as e:
        LOG.debug(f"Could not initialize mic level file early: {e}")


def handle_record_begin():
    """Forward internal bus message to external bus."""
    LOG.info("Begin Recording...")
    context = {'client_name': 'mycroft_listener',
               'source': 'audio'}
    bus.emit(Message('recognizer_loop:record_begin', context=context))


def handle_record_end():
    """Forward internal bus message to external bus."""
    LOG.info("End Recording...")
    context = {'client_name': 'mycroft_listener',
               'source': 'audio'}
    bus.emit(Message('recognizer_loop:record_end', context=context))


def handle_no_internet():
    LOG.debug("Notifying enclosure of no internet connection")
    context = {'client_name': 'mycroft_listener',
               'source': 'audio'}
    bus.emit(Message('enclosure.notify.no_internet', context=context))


def handle_awoken():
    """Forward mycroft.awoken to the messagebus."""
    LOG.info("Listener is now Awake: ")
    context = {'client_name': 'mycroft_listener',
               'source': 'audio'}
    bus.emit(Message('mycroft.awoken', context=context))


def handle_wakeword(event):
    LOG.info("Wakeword Detected: " + event['utterance'])
    bus.emit(Message('recognizer_loop:wakeword', event))


def handle_utterance(event):
    LOG.info("Utterance: " + str(event['utterances']))
    context = {'client_name': 'mycroft_listener',
               'source': 'audio',
               'destination': ["skills"]}
    if 'ident' in event:
        ident = event.pop('ident')
        context['ident'] = ident
    bus.emit(Message('recognizer_loop:utterance', event, context))


def handle_unknown():
    context = {'client_name': 'mycroft_listener',
               'source': 'audio'}
    bus.emit(Message('mycroft.speech.recognition.unknown', context=context))


def handle_speak(event):
    """
        Forward speak message to message bus.
    """
    context = {'client_name': 'mycroft_listener',
               'source': 'audio'}
    bus.emit(Message('speak', event, context))


def handle_complete_intent_failure(event):
    """Extreme backup for answering completely unhandled intent requests."""
    LOG.info("Failed to find intent.")
    data = {'utterance': dialog.get('not.loaded')}
    context = {'client_name': 'mycroft_listener',
               'source': 'audio'}
    bus.emit(Message('speak', data, context))


def handle_sleep(event):
    """Put the recognizer loop to sleep."""
    loop.sleep()


def handle_wake_up(event):
    """Wake up the the recognize loop."""
    loop.awaken()


def handle_mic_mute(event):
    """Mute the listener system."""
    loop.mute()


def handle_mic_unmute(event):
    """Unmute the listener system."""
    loop.unmute()


def handle_mic_listen(_):
    """Handler for mycroft.mic.listen.

    Starts listening as if wakeword was spoken.
    """
    loop.responsive_recognizer.trigger_listen()


def handle_mic_get_status(event):
    """Query microphone mute status."""
    data = {'muted': loop.is_muted()}
    bus.emit(event.response(data))


def handle_paired(event):
    """Update identity information with pairing data.

    This is done here to make sure it's only done in a single place.
    TODO: Is there a reason this isn't done directly in the pairing skill?
    """
    IdentityManager.update(event.data)


def handle_audio_start(event):
    """Mute recognizer loop."""
    if config.get("listener").get("mute_during_output"):
        loop.mute()


def handle_audio_end(event):
    """Request unmute, if more sources have requested the mic to be muted
    it will remain muted.
    """
    if config.get("listener").get("mute_during_output"):
        loop.unmute()  # restore


def handle_stop(event):
    """Handler for mycroft.stop, i.e. button press."""
    loop.force_unmute()


def handle_open():
    # TODO: Move this into the Enclosure (not speech client)
    # Reset the UI to indicate ready for speech processing
    EnclosureAPI(bus).reset()

    # Subscribe to intent updates - will receive them when skills is ready
    LOG.info("Bus connected - subscribing to intent patterns from skills service")
    bus.emit(Message('realtime:subscribe_intents'))


def on_ready():
    LOG.info('Realtime client is ready.')

    # Emit mycroft.ready event when realtime client is ready
    try:
        from mycroft.messagebus.message import Message
        # Use the global bus variable
        global bus
        if bus:
            bus.emit(Message('mycroft.ready'))
            LOG.info('Emitted mycroft.ready event - Realtime service is ready!')

            # Subscribe to intent updates from skills service
            LOG.info("Subscribing to intent patterns from skills service")
            bus.emit(Message('realtime:subscribe_intents'))
    except Exception as e:
        LOG.debug('Could not emit mycroft.ready event: {}'.format(e))


def on_stopping():
    LOG.info('Realtime service is shutting down...')


def on_error(e='Unknown'):
    LOG.error('Realtime service failed to launch ({}).'.format(repr(e)))


def handle_whisper_partial(event):
    """Forward Whisper partial results to messagebus for CLI display."""
    bus.emit(Message('mycroft.debug.whisper.partial', event))


def handle_whisper_final(event):
    """Forward Whisper final results to messagebus for CLI display."""
    bus.emit(Message('mycroft.debug.whisper.final', event))


def connect_loop_events(loop):
    loop.on('recognizer_loop:utterance', handle_utterance)
    loop.on('recognizer_loop:speech.recognition.unknown', handle_unknown)
    loop.on('speak', handle_speak)
    loop.on('recognizer_loop:record_begin', handle_record_begin)
    loop.on('recognizer_loop:awoken', handle_awoken)
    loop.on('recognizer_loop:wakeword', handle_wakeword)
    loop.on('recognizer_loop:record_end', handle_record_end)
    loop.on('recognizer_loop:no_internet', handle_no_internet)
    loop.on('mycroft.debug.whisper.partial', handle_whisper_partial)
    loop.on('mycroft.debug.whisper.final', handle_whisper_final)


def load_entity_expansions(skill_path):
    """Load ENTITY_EXPANSIONS from a skill's __init__.py if it exists.

    Args:
        skill_path: Path to skill directory

    Returns:
        dict: Entity expansions or empty dict if not found
    """
    import importlib.util
    import sys

    try:
        init_path = os.path.join(skill_path, '__init__.py')
        if not os.path.exists(init_path):
            return {}

        # Load module to get ENTITY_EXPANSIONS
        spec = importlib.util.spec_from_file_location("temp_skill", init_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        return getattr(module, 'ENTITY_EXPANSIONS', {})
    except Exception as e:
        LOG.debug(f"Could not load ENTITY_EXPANSIONS from {skill_path}: {e}")
        return {}


def expand_pattern_entities(pattern, entity_expansions):
    """Expand {entity} placeholders in pattern with values from files.

    Creates separate pattern lines for each entity value, matching how Padatious
    expands patterns. This allows CommandPattern._expand_pattern() to handle
    the remaining optional groups correctly.

    Args:
        pattern: Pattern string like "color {color}" or "[open|close] {main}"
        entity_expansions: Dict mapping entity names to their config

    Returns:
        list: Multiple pattern lines, one for each entity value combination
              Example: "[open|close] {main}" with main=[gpt,terminal] returns:
                - "[open|close] gpt"
                - "[open|close] terminal"
    """
    import re
    import itertools

    # Find all {entity} placeholders
    entities = re.findall(r'\{(\w+)\}', pattern)
    if not entities:
        return [pattern]

    # For each entity, load values from files
    entity_values = {}
    for entity_name in entities:
        if entity_name not in entity_expansions:
            # No expansion defined, keep as-is
            continue

        config = entity_expansions[entity_name]
        files = config.get('files', [])
        format_type = config.get('format', 'csv')
        column = config.get('column', 0)

        values = []
        for file_path in files:
            try:
                with open(file_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue

                        if format_type == 'csv':
                            # Try semicolon first, then comma
                            if ';' in line:
                                parts = line.split(';')
                            else:
                                parts = line.split(',')
                            if len(parts) > column:
                                values.append(parts[column].strip())
                        else:
                            # Simple format - one value per line
                            values.append(line)
            except Exception as e:
                LOG.warning(f"Failed to load entity file {file_path}: {e}")

        if values:
            entity_values[entity_name] = values

    # If no entities were expanded, return original
    if not entity_values:
        return [pattern]

    # Generate Cartesian product of all entity values
    # For each combination, create a separate pattern line
    expanded_patterns = []

    # Get entity names in order they appear in pattern
    entity_order = entities

    # Get value lists in same order
    value_lists = [entity_values.get(name, [name]) for name in entity_order]

    # Generate all combinations
    for combination in itertools.product(*value_lists):
        expanded = pattern
        for entity_name, value in zip(entity_order, combination):
            placeholder = f'{{{entity_name}}}'
            expanded = expanded.replace(placeholder, value)
        expanded_patterns.append(expanded)

    return expanded_patterns


def handle_intents_ready(event):
    """Receive intent patterns from skills service when available."""
    if not loop:
        return

    intent_files = event.data.get('intent_files', {})
    LOG.info(f"Received {len(intent_files)} intent files from skills service")

    # Clear existing patterns
    loop.shared_patterns.clear()

    # Load each intent file into the command matcher
    for intent_name, file_path in intent_files.items():
        try:
            # Determine skill path from intent file path
            # Path format: /opt/mycroft/skills/skill-name/locale/en-us/file.intent
            skill_path = os.path.dirname(os.path.dirname(os.path.dirname(file_path)))

            # Load entity expansions from skill
            entity_expansions = load_entity_expansions(skill_path)

            with open(file_path, 'r') as f:
                pattern_lines = [line.strip() for line in f.readlines() if line.strip()]

            # Expand patterns with entity values
            expanded_lines = []
            for pattern in pattern_lines:
                expanded = expand_pattern_entities(pattern, entity_expansions)
                expanded_lines.extend(expanded)

            # Register expanded patterns in BOTH matchers
            # Each matcher maintains its own all_sequences cache for fast lookups
            loop.interim_matcher.register_intent(intent_name, expanded_lines)
            loop.final_matcher.register_intent(intent_name, expanded_lines)

            if entity_expansions:
                LOG.info(f"Expanded {len(pattern_lines)} patterns to {len(expanded_lines)} for {intent_name}")
        except Exception as e:
            LOG.warning(f"Failed to load intent {intent_name} from {file_path}: {e}")

    LOG.info(f"Loaded {len(loop.shared_patterns)} total patterns for streaming command matching")


def connect_bus_events(bus):
    # Register handlers for events on main Mycroft messagebus
    bus.on('open', handle_open)
    bus.on('complete_intent_failure', handle_complete_intent_failure)
    bus.on('recognizer_loop:sleep', handle_sleep)
    bus.on('recognizer_loop:wake_up', handle_wake_up)
    bus.on('mycroft.mic.mute', handle_mic_mute)
    bus.on('mycroft.mic.unmute', handle_mic_unmute)
    bus.on('mycroft.mic.get_status', handle_mic_get_status)
    bus.on('mycroft.mic.listen', handle_mic_listen)
    bus.on("mycroft.paired", handle_paired)
    bus.on('recognizer_loop:audio_output_start', handle_audio_start)
    bus.on('recognizer_loop:audio_output_end', handle_audio_end)
    bus.on('mycroft.stop', handle_stop)
    bus.on('padatious:intents_ready', handle_intents_ready)


def main(ready_hook=on_ready, error_hook=on_error, stopping_hook=on_stopping,
         watchdog=lambda: None):
    global bus
    global loop
    global config
    try:
        reset_sigint_handler()
        PIDLock("realtime")
        config = Configuration.get()

        # Initialize mic level file early so CLI can display it immediately
        _initialize_mic_level_file_early()

        bus = start_message_bus_client("REALTIME")
        connect_bus_events(bus)
        callbacks = StatusCallbackMap(on_ready=ready_hook, on_error=error_hook,
                                      on_stopping=stopping_hook)
        status = ProcessStatus('realtime', bus, callbacks)

        # Register handlers on internal RealtimeRecognizerLoop bus
        loop = RealtimeRecognizerLoop(watchdog)
        connect_loop_events(loop)
        create_daemon(loop.run)
        status.set_started()
    except Exception as e:
        error_hook(e)
    else:
        status.set_ready()
        wait_for_exit_signal()
        status.set_stopping()


if __name__ == "__main__":
    main()
