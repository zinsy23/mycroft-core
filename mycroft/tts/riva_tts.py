import wave
import grpc
import riva.client
import riva.client.proto.riva_tts_pb2 as rtts
import riva.client.proto.riva_tts_pb2_grpc as rtts_grpc
import riva.client.proto.riva_audio_pb2 as raudio

from .tts import TTS, TTSValidator


class RivaTTS(TTS):
    def __init__(self, lang, config):
        super(RivaTTS, self).__init__(lang, config, RivaTTSValidator(self),
                                     audio_ext='wav')
        self.server_uri = config.get('server_uri', 'localhost:50051')
        self.voice = config.get('voice', 'English-US')
        self.sample_rate = config.get('sample_rate', 44100)

    def get_tts(self, sentence, wav_file):
        auth = riva.client.Auth(uri=self.server_uri)
        tts_service = riva.client.SpeechSynthesisService(auth)

        resp = tts_service.synthesize(
            sentence,
            voice_name=self.voice,
            language_code='en-US',
            encoding=raudio.AudioEncoding.LINEAR_PCM,
            sample_rate_hz=self.sample_rate
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
        try:
            channel = grpc.insecure_channel(self.tts.server_uri)
            stub = rtts_grpc.RivaSpeechSynthesisStub(channel)
            grpc.channel_ready_future(channel).result(timeout=5)
        except Exception:
            raise Exception(
                f'Cannot connect to RIVA TTS at {self.tts.server_uri}. '
                'Make sure the RIVA container is running.'
            )

    def get_tts_class(self):
        return RivaTTS
