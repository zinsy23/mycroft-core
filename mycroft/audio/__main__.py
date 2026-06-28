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
"""Mycroft audio service.

    This handles playback of audio and speech
"""
import sys

from mycroft.util import (
    check_for_signal,
    reset_sigint_handler,
    start_message_bus_client,
    wait_for_exit_signal
)
from mycroft.util.log import LOG
from mycroft.util.process_utils import ProcessStatus, StatusCallbackMap

import mycroft.audio.speech as speech
from .audioservice import AudioService


def on_ready():
    LOG.info('Audio service is ready.')


def on_error(e='Unknown'):
    LOG.error('Audio service failed to launch ({}).'.format(repr(e)))


def on_stopping():
    LOG.info('Audio service is shutting down...')


def main(ready_hook=on_ready, error_hook=on_error, stopping_hook=on_stopping):
    """Start the Audio Service and connect to the Message Bus"""
    LOG.info("Starting Audio Service")
    try:
        reset_sigint_handler()
        check_for_signal("isSpeaking")
        whitelist = ['mycroft.audio.service']
        bus = start_message_bus_client("AUDIO", whitelist=whitelist)
        callbacks = StatusCallbackMap(on_ready=ready_hook, on_error=error_hook,
                                      on_stopping=stopping_hook)
        status = ProcessStatus('audio', bus, callbacks)

        speech.init(bus)

        # Connect audio service instance to message bus
        audio = AudioService(bus)
        status.set_started()
    except Exception as e:
        status.set_error(e)
        # A startup failure here leaves daemon threads (e.g. the bus client's
        # background run_forever loop, started in start_message_bus_client)
        # alive while main() falls off the end without ever reaching
        # wait_for_exit_signal()/clean shutdown. That lets CPython's
        # interpreter-shutdown atexit hook tear down every ThreadPoolExecutor
        # in the process -- including the bus client's internal emitter --
        # while pgrep still sees this process as "running", permanently
        # breaking its message bus dispatch for the rest of its life. Exit
        # explicitly instead, so start-mycroft.sh's orphan detection can spot
        # the dead process and a later launch_background can replace it with
        # a healthy one.
        LOG.error('Audio service failed to start, exiting.')
        sys.exit(1)
    else:
        if audio.wait_for_load() and len(audio.service) > 0:
            # If at least one service exists, report ready
            status.set_ready()
            wait_for_exit_signal()
            status.set_stopping()
        else:
            status.set_error('No audio services loaded')

        speech.shutdown()
        audio.shutdown()


if __name__ == '__main__':
    main()
