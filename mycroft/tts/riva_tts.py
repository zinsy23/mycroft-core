import wave
import grpc
import riva.client
import riva.client.proto.riva_tts_pb2 as rtts
import riva.client.proto.riva_tts_pb2_grpc as rtts_grpc
import riva.client.proto.riva_audio_pb2 as raudio

from .tts import TTS, TTSValidator
from .remote_tts import RemoteTTSException

# Riva's container can take well over 5s to finish initializing its models
# after the gRPC port starts accepting connections, so a single short-timeout
# check at startup isn't enough -- retry with backoff before giving up.
CONNECTION_CHECK_RETRIES = 4
CONNECTION_CHECK_TIMEOUT = 5


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
        last_error = None
        for attempt in range(CONNECTION_CHECK_RETRIES):
            try:
                channel = grpc.insecure_channel(self.tts.server_uri)
                rtts_grpc.RivaSpeechSynthesisStub(channel)
                grpc.channel_ready_future(channel).result(
                    timeout=CONNECTION_CHECK_TIMEOUT)
                return
            except Exception as e:
                last_error = e

        raise Exception(
            f'Cannot connect to RIVA TTS at {self.tts.server_uri} after '
            f'{CONNECTION_CHECK_RETRIES} attempts. Make sure the RIVA '
            f'container is running and finished initializing.'
        ) from last_error

    def get_tts_class(self):
        return RivaTTS
