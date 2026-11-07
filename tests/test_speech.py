import asyncio
import json
import wave
from pathlib import Path

import pytest

from app.speech import AudioBuffer, MAX_BYTES, SpeechError, SpeechRecognizer, download_audio


def test_audio_buffer_bounds():
    with AudioBuffer() as output:
        output.write(b'x')
        with pytest.raises(SpeechError):
            output.write(b'x' * MAX_BYTES)


@pytest.mark.parametrize('enabled,data,duration', [(False,b'a',1),(True,b'',1),(True,b'a',121)])
def test_invalid_audio_never_starts_worker(enabled, data, duration):
    with pytest.raises(SpeechError):
        asyncio.run(SpeechRecognizer(enabled).transcribe(data, duration))


def test_worker_result_masking_and_temporary_file_cleanup(monkeypatch):
    paths = []
    async def create(*args, **kwargs):
        paths.append(Path(args[-1]))
        assert paths[-1].read_bytes() == b'audio'
        class Process:
            returncode = 0
            async def communicate(self):
                return json.dumps({'text':'Позвоните +7 912 345-67-89'}).encode(), b''
        return Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    assert asyncio.run(SpeechRecognizer(True).transcribe(b'audio')) == 'Позвоните [телефон]'
    assert not paths[0].exists()


def test_timeout_kills_worker_before_cleanup(monkeypatch):
    killed, paths = [], []
    async def create(*args, **kwargs):
        paths.append(Path(args[-1]))
        class Process:
            returncode = None
            pid = 123456
            async def communicate(self):
                await asyncio.sleep(10)
            async def wait(self):
                self.returncode = -9
        return Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    monkeypatch.setattr('app.speech.os.killpg', lambda pid, sig: killed.append(pid))
    with pytest.raises(SpeechError):
        asyncio.run(SpeechRecognizer(True, timeout=.01).transcribe(b'audio'))
    assert killed == [123456] and not paths[0].exists()


@pytest.mark.parametrize('url', ['http://example.org/a', 'https://127.0.0.1/a', 'https://u:p@example.org/a',
                                'https://169.254.169.254/a', 'https://example.org:8000/a'])
def test_download_rejects_unsafe_urls(url):
    with pytest.raises(SpeechError):
        asyncio.run(download_audio(url))


def test_worker_chunks_long_audio_without_losing_frames(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.speech_worker import transcribe
    source = tmp_path / 'input.bin'
    source.write_bytes(b'input')
    with wave.open(str(source.with_suffix('.wav')), 'wb') as f:
        f.setparams((1,2,16000,0,'NONE','not compressed'))
        f.writeframes(b'\0\0' * 16000 * 47)
    durations = []
    def recognize(path):
        with wave.open(path) as f:
            durations.append(f.getnframes()/f.getframerate())
        return SimpleNamespace(text='фрагмент')
    monkeypatch.setattr('app.speech_worker.subprocess.run', lambda *a, **kw: None)
    monkeypatch.setattr('app.speech_worker.load_model', lambda: SimpleNamespace(transcribe=recognize))
    assert transcribe(source) == 'фрагмент фрагмент фрагмент'
    assert durations == [20,20,7]


def test_speech_word_error_distance():
    from eval.speech_eval import distance, words
    assert distance(words('ВЕЕР ролик пятнадцать секунд'), words('веер ролик двадцать секунд')) == 1
    assert distance(words('два слова'), []) == 2


def test_actual_decoder_checks_duration_without_model(tmp_path):
    import shutil
    from app.speech_worker import transcribe
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg — необязательная зависимость голосовых')
    source = tmp_path / 'long.bin'
    with wave.open(str(source), 'wb') as f:
        f.setparams((1,2,16000,0,'NONE','not compressed'))
        f.writeframes(b'\0\0' * 16000 * 121)
    with pytest.raises(ValueError, match='audio too long'):
        transcribe(source)
