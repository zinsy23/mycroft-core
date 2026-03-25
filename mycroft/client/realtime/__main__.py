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
from threading import Lock, Thread
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
    intent = event.pop('intent', None)
    if intent:
        # Realtime already matched — emit directly to the skill, bypassing Padatious
        utterance = event['utterances'][0]
        bus.emit(Message(intent, {
            'utterance': utterance,
            'utterances': event['utterances']
        }))
    else:
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
    _emit_async('mycroft.debug.whisper.partial', event)


def handle_whisper_final(event):
    _emit_async('mycroft.debug.whisper.final', event)


def _emit_async(msg_type, data):
    """Emit a display-only bus message off the loop thread to avoid blocking."""
    Thread(target=bus.emit, args=(Message(msg_type, data),), daemon=True).start()


def handle_realtime_session_start(event):
    _emit_async('mycroft.realtime.session_start', event)


def handle_realtime_session_end(event):
    _emit_async('mycroft.realtime.session_end', event)


def handle_realtime_command_matched(event):
    _emit_async('mycroft.realtime.command_matched', event)


def handle_realtime_mode_changed(event):
    _emit_async('mycroft.realtime.mode_changed', event)


def handle_realtime_manage(event):
    _emit_async('mycroft.realtime.manage', event)


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
    loop.on('mycroft.realtime.session_start', handle_realtime_session_start)
    loop.on('mycroft.realtime.session_end', handle_realtime_session_end)
    loop.on('mycroft.realtime.command_matched', handle_realtime_command_matched)
    loop.on('mycroft.realtime.mode_changed', handle_realtime_mode_changed)
    loop.on('mycroft.realtime.manage', handle_realtime_manage)


def handle_intents_ready(event):
    """Receive intent patterns from skills service when available."""
    from mycroft.client.realtime.pattern_loader import (
        load_entity_expansions, expand_pattern_entities,
        _split_free_text_pattern, load_quantifier_patterns,
        build_management_patterns,
        FREE_TEXT_TAG, REPEAT_TAG
    )
    from mycroft.configuration import Configuration

    if not loop:
        return

    intent_files = event.data.get('intent_files', {})
    LOG.info(f"Received {len(intent_files)} intent files from skills service")

    realtime_config = Configuration.get().get('realtime', {})
    free_text_entity = realtime_config.get('free_text', {}).get('entity_name', 'free_text')
    repeat_config = realtime_config.get('repeat', {})
    command_groups = realtime_config.get('command_groups', [])

    # Clear all pattern state for reload
    loop.shared_patterns.clear()
    loop.skill_pattern_groups.clear()
    loop.protected_patterns.clear()
    loop.interim_matcher.patterns = loop.shared_patterns
    loop.final_matcher.patterns = loop.shared_patterns
    loop.interim_matcher.all_sequences = []
    loop.final_matcher.all_sequences = []

    det_count = 0
    ft_count = 0

    # Helper: find skill_prefix for an intent_name
    def _get_skill_prefix(intent_name):
        for g in command_groups:
            if intent_name.startswith(g['skill_prefix']):
                return g['skill_prefix']
        return None

    # Load each intent file — register into skill_pattern_groups (not shared_patterns directly)
    for intent_name, file_path in intent_files.items():
        try:
            skill_path = os.path.dirname(os.path.dirname(os.path.dirname(file_path)))
            entity_expansions = load_entity_expansions(skill_path)

            with open(file_path, 'r') as f:
                pattern_lines = [line.strip() for line in f.readlines() if line.strip()]

            det_lines = []
            ft_lines = []

            for pattern in pattern_lines:
                expanded = expand_pattern_entities(pattern, entity_expansions)
                for exp_pattern in expanded:
                    prefix = _split_free_text_pattern(exp_pattern, free_text_entity)
                    if prefix is not None:
                        ft_lines.append(prefix)
                    else:
                        det_lines.append(exp_pattern)

            skill_prefix = _get_skill_prefix(intent_name)

            if det_lines:
                # Register into a temporary matcher to get CommandPattern objects,
                # then move them to skill_pattern_groups
                loop.interim_matcher.register_intent(intent_name, det_lines)
                loop.final_matcher.register_intent(intent_name, det_lines)
                det_count += len(det_lines)

            if ft_lines:
                tagged = f'{FREE_TEXT_TAG}{intent_name}'
                loop.interim_matcher.register_intent(tagged, ft_lines)
                loop.final_matcher.register_intent(tagged, ft_lines)
                ft_count += len(ft_lines)
                LOG.info(f"  {intent_name}: {len(ft_lines)} free-text trigger prefix(es)")

        except Exception as e:
            LOG.warning(f"Failed to load intent {intent_name} from {file_path}: {e}")

    # Register repeat patterns (always active — go into protected layer)
    repeat_patterns = load_quantifier_patterns(repeat_config)
    for repeat_intent, patterns in repeat_patterns.items():
        loop.interim_matcher.register_intent(repeat_intent, patterns)
        loop.final_matcher.register_intent(repeat_intent, patterns)

    # Build and register management patterns (always active — protected layer)
    mgmt_patterns = build_management_patterns(command_groups)
    for mgmt_intent, patterns in mgmt_patterns.items():
        loop.interim_matcher.register_intent(mgmt_intent, patterns)
        loop.final_matcher.register_intent(mgmt_intent, patterns)

    # Now partition all_sequences into skill_pattern_groups vs protected_patterns
    # by checking each pattern's intent name against command_groups
    loop.protected_patterns.clear()
    loop.skill_pattern_groups.clear()

    for pattern in loop.shared_patterns:
        intent_name = pattern.intent_name
        skill_prefix = _get_skill_prefix(intent_name)
        if skill_prefix:
            if skill_prefix not in loop.skill_pattern_groups:
                loop.skill_pattern_groups[skill_prefix] = []
            loop.skill_pattern_groups[skill_prefix].append(pattern)
        else:
            loop.protected_patterns.append(pattern)

    # Rebuild shared_patterns from the partitioned groups respecting current mute/disabled state
    loop._rebuild_shared_patterns()

    LOG.info(f"Loaded {det_count} deterministic + {ft_count} free-text trigger patterns; "
             f"{len(repeat_patterns)} repeat intent variants; "
             f"{len(mgmt_patterns)} management patterns; "
             f"{len(loop.skill_pattern_groups)} skill groups")


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
