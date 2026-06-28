import wave
import grpc
import riva.client
import riva.client.proto.riva_tts_pb2 as rtts
import riva.client.proto.riva_tts_pb2_grpc as rtts_grpc
import riva.client.proto.riva_audio_pb2 as raudio

from .tts import TTS, TTSValidator
from .remote_tts import RemoteTTSException


class RivaTTS(TTS):
    def __init__(self, lang, config):
        super(RivaTTS, self).__init__(lang, config, RivaTTSValidator(self),
                                     audio_ext='wav')
        self.server_uri = config.get('server_uri', 'localhost:50051')
        self.voice = config.get('voice', 'English-US')
        self.sample_rate = config.get('sample_rate', 44100)

    def get_tts(self, sentence, wav_file):
        try:
            auth = riva.client.Auth(uri=self.server_uri)
            tts_service = riva.client.SpeechSynthesisService(auth)

            resp = tts_service.synthesize(
                sentence,
                voice_name=self.voice,
                language_code='en-US',
                encoding=raudio.AudioEncoding.LINEAR_PCM,
                sample_rate_hz=self.sample_rate
            )
        except grpc.RpcError as e:
            raise RemoteTTSException(
                f'Cannot reach RIVA TTS at {self.server_uri}: {e}'
            )

        with wave.open(wav_file, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self.sample_rate)
            wf.writeframes(resp.audio)

        return wav_file, None


class RivaTTSValidator(TTSValidator):
    def __init__(self, tts):
        super(RivaTTSValidator, self).__init__(tts)

    def validate_lang(self):
        pass

    def validate_connection(self):
        # Unlike most TTS backends, Riva's container can take well over 5s
        # to finish initializing its models after the gRPC port opens, and
        # it may legitimately be offline/restarting at any point during
        # normal operation, not just at audio-service startup. Gating
        # startup on a synchronous connection check here means a slow or
        # momentarily-down Riva falls through TTSFactory.create() to Mimic
        # (which isn't installed) and crashes the whole audio service.
        # Mirroring how STTFactory.create() treats remote STT backends
        # (instantiate without probing the network), skip the eager check
        # and let connection failures surface lazily per-request in
        # RivaTTS.get_tts() via RemoteTTSException, with self-healing retry
        # already handled in mycroft/audio/speech.py's mute_and_speak().
        pass

    def get_tts_class(self):
        return RivaTTS
