import re
import wave
import grpc
import riva.client
import riva.client.proto.riva_tts_pb2 as rtts
import riva.client.proto.riva_tts_pb2_grpc as rtts_grpc
import riva.client.proto.riva_audio_pb2 as raudio

from .tts import TTS, TTSValidator
from .remote_tts import RemoteTTSException

# Conservative character limit per chunk — stays well under the 400-token
# preprocessor limit (average English word ~5 tokens including phonemes).
_CHUNK_CHAR_LIMIT = 200

# Split priority: sentence endings first, then clauses, then words.
_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')
_CLAUSE_RE = re.compile(r'(?<=[,;:])\s+')


def split_for_tts(text, limit=_CHUNK_CHAR_LIMIT):
    """Split text into chunks that fit within RIVA's token limit.

    Splits on sentence boundaries first, then clause boundaries, then
    words as a last resort. Callers are agnostic to this happening.
    """
    if len(text) <= limit:
        return [text]

    chunks = []
    for sentence in _SENTENCE_RE.split(text):
        if len(sentence) <= limit:
            chunks.append(sentence)
        else:
            for clause in _CLAUSE_RE.split(sentence):
                if len(clause) <= limit:
                    chunks.append(clause)
                else:
                    # Fall back to word-boundary splitting
                    words = clause.split()
                    current = ''
                    for word in words:
                        candidate = (current + ' ' + word).strip()
                        if len(candidate) <= limit:
                            current = candidate
                        else:
                            if current:
                                chunks.append(current)
                            current = word
                    if current:
                        chunks.append(current)
    return [c for c in chunks if c.strip()]


class RivaSpeechSynthesisService:
    """Drop-in wrapper around riva.client.SpeechSynthesisService that
    transparently splits text and retries when the model's token limit
    is exceeded. Models that don't have this constraint are unaffected —
    the split only triggers on the specific RIVA sequence-length error."""

    _SEQ_LEN_ERR = "longer than maximum sequence length"

    def __init__(self, auth):
        self._service = riva.client.SpeechSynthesisService(auth)

    def synthesize(self, text, **kwargs):
        try:
            return self._service.synthesize(text, **kwargs)
        except grpc.RpcError as e:
            if self._SEQ_LEN_ERR not in e.details():
                raise
        # Token limit hit — split and merge
        chunks = split_for_tts(text)
        audio = b''
        for chunk in chunks:
            resp = self._service.synthesize(chunk, **kwargs)
            audio += resp.audio
        class _Resp:
            pass
        r = _Resp()
        r.audio = audio
        return r


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

            chunks = split_for_tts(sentence)
            audio_data = b''
            for chunk in chunks:
                resp = tts_service.synthesize(
                    chunk,
                    voice_name=self.voice,
                    language_code='en-US',
                    encoding=raudio.AudioEncoding.LINEAR_PCM,
                    sample_rate_hz=self.sample_rate
                )
                audio_data += resp.audio
        except grpc.RpcError as e:
            raise RemoteTTSException(
                f'Cannot reach RIVA TTS at {self.server_uri}: {e}'
            )

        with wave.open(wav_file, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio_data)

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
