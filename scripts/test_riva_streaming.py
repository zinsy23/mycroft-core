#!/home/joseph/mycroft-core/.venv/bin/python3
"""
Live STT test using Riva streaming ASR.
Watches word-by-word output as speech comes in.

Usage:
    ./scripts/test_riva_streaming.py [--server SERVER]
"""

import argparse
import sys
import queue

import pyaudio
import riva.client

SAMPLE_RATE = 16000
CHUNK_SIZE = 1600  # 0.1 seconds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="localhost:50051", help="Riva server URI (default: localhost:50051)")
    args = parser.parse_args()

    print(f"Connecting to Riva server at {args.server}...", flush=True)

    try:
        auth = riva.client.Auth(uri=args.server)
        asr_service = riva.client.ASRService(auth)
    except Exception as e:
        print(f"Failed to connect to Riva server: {e}")
        sys.exit(1)

    print("Connected! Starting audio stream...")

    # Configure streaming recognition
    config = riva.client.StreamingRecognitionConfig(
        config=riva.client.RecognitionConfig(
            encoding=riva.client.AudioEncoding.LINEAR_PCM,
            language_code="en-US",
            sample_rate_hertz=SAMPLE_RATE,
            max_alternatives=1,
            profanity_filter=False,
            enable_automatic_punctuation=False,
            verbatim_transcripts=True,
            model="conformer-xl-en-US-asr-streaming-asr-bls-ensemble",
        ),
        interim_results=True,
    )

    p = pyaudio.PyAudio()
    audio_queue = queue.Queue()

    def audio_generator():
        """Generator that yields audio chunks from the queue."""
        while True:
            chunk = audio_queue.get()
            if chunk is None:  # Sentinel to stop
                break
            yield chunk

    try:
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
    except Exception as e:
        print(f"Failed to open audio stream: {e}")
        p.terminate()
        sys.exit(1)

    print(f"Listening... (Press Ctrl+C to stop)\n")
    print("-" * 60)

    try:
        # Start streaming recognition in a separate function
        def stream_audio():
            while True:
                try:
                    data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                    audio_queue.put(data)
                except KeyboardInterrupt:
                    audio_queue.put(None)
                    break

        # Start audio capture in background
        import threading
        audio_thread = threading.Thread(target=stream_audio, daemon=True)
        audio_thread.start()

        # Process streaming responses
        responses = asr_service.streaming_response_generator(
            audio_chunks=audio_generator(),
            streaming_config=config,
        )

        for response in responses:
            if not response.results:
                continue

            for result in response.results:
                if result.alternatives:
                    transcript = result.alternatives[0].transcript

                    # Print final results on new line, interim results inline
                    if result.is_final:
                        print(f"\r{' ' * 80}\r{transcript}")  # Clear interim, print final
                        print("-" * 60)
                    else:
                        # Interim result - show what's being recognized in real-time
                        print(f"\r{transcript:<80}", end="", flush=True)

    except KeyboardInterrupt:
        print("\n\nStopped.")
    except Exception as e:
        print(f"\nError during streaming: {e}")
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()


if __name__ == "__main__":
    main()
