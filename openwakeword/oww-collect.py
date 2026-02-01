#!/usr/bin/env python3
"""
OpenWakeWord Collect - Record audio samples with auto-numbering
Mimics precise-collect behavior

Usage:
    python oww-collect.py [file_label] [--rate 16000] [--channels 1] [--width 2]

    Press space to start/stop recording
    Press esc to exit
"""

import argparse
import pyaudio
import wave
from select import select
from sys import stdin
from termios import tcsetattr, tcgetattr, TCSADRAIN
import tty
from os.path import isfile


orig_settings = None
RECORD_KEY = ' '
EXIT_KEY_CODE = 27


def key_pressed():
    return select([stdin], [], [], 0) == ([stdin], [], [])


def show_input():
    tcsetattr(stdin, TCSADRAIN, orig_settings)


def hide_input():
    tty.setcbreak(stdin.fileno())


def record_until(p, should_return, args):
    chunk_size = 1024
    stream = p.open(format=p.get_format_from_width(args.width), channels=args.channels,
                    rate=args.rate, input=True, frames_per_buffer=chunk_size)

    frames = []
    while not should_return():
        frames.append(stream.read(chunk_size))

    stream.stop_stream()
    stream.close()

    return b''.join(frames)


def save_audio(name, data, args):
    wf = wave.open(name, 'wb')
    wf.setnchannels(args.channels)
    wf.setsampwidth(args.width)
    wf.setframerate(args.rate)
    wf.writeframes(data)
    wf.close()


def next_name(name):
    name += '.wav'
    pos, num_digits = None, None
    try:
        pos = name.index('#')
        num_digits = name.count('#')
    except ValueError:
        print("Name must contain at least one # to indicate where to put the number.")
        raise

    def get_name(i):
        nonlocal name, pos
        return name[:pos] + str(i).zfill(num_digits) + name[pos + num_digits:]

    i = 0
    while True:
        if not isfile(get_name(i)):
            break
        i += 1

    return get_name(i)


def wait_to_continue():
    while True:
        c = stdin.read(1)
        if c == RECORD_KEY:
            return True
        elif ord(c) == EXIT_KEY_CODE:
            return False


def record_until_key(p, args):
    def should_return():
        return key_pressed() and stdin.read(1) == RECORD_KEY

    return record_until(p, should_return, args)


def termios_wrapper(main):
    global orig_settings
    orig_settings = tcgetattr(stdin)
    try:
        hide_input()
        main()
    finally:
        tcsetattr(stdin, TCSADRAIN, orig_settings)


def _main():
    parser = argparse.ArgumentParser(description='Record audio samples for use with OpenWakeWord')
    parser.add_argument('file_label', nargs='?', help='File label (Ex. recording-##)')
    parser.add_argument('-w', '--width', type=int, default=2, help='Sample width of audio (default: 2)')
    parser.add_argument('-r', '--rate', type=int, default=16000, help='Sample rate of audio (default: 16000)')
    parser.add_argument('-c', '--channels', type=int, default=1, help='Number of audio channels (default: 1)')

    args = parser.parse_args()
    show_input()
    args.file_label = args.file_label or input("File label (Ex. recording-##): ")
    args.file_label = args.file_label + ('' if '#' in args.file_label else '-##')
    hide_input()

    p = pyaudio.PyAudio()

    while True:
        print('Press space to record (esc to exit)...')

        if not wait_to_continue():
            break

        print('Recording...')
        d = record_until_key(p, args)
        name = next_name(args.file_label)
        save_audio(name, d, args)
        print('Saved as ' + name)

    p.terminate()


def main():
    termios_wrapper(_main)


if __name__ == '__main__':
    main()
