"""Worker GigaAM. Запускать интерпретатором с requirements-speech.txt."""
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import wave
from pathlib import Path

if __package__:
    from .speech_models import load_model
else:
    from speech_models import load_model


def transcribe(source: Path) -> str:
    wav = source.with_suffix('.wav')
    # Декодер читает только локальный файл, длительность и размер результата ограничены.
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                    '-format_whitelist', 'ogg,mp3,wav,mov,matroska,webm,aac,flac',
                    '-i', str(source), '-t', '121', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
                    '-y', str(wav)], check=True, timeout=30, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    with wave.open(str(wav), 'rb') as audio:
        if audio.getnframes() > 120 * audio.getframerate():
            raise ValueError('audio too long')
        model = load_model()
        pieces = []
        while frames := audio.readframes(20 * audio.getframerate()):
            segment = source.with_name('segment.wav')
            with wave.open(str(segment), 'wb') as chunk:
                chunk.setparams(audio.getparams())
                chunk.writeframes(frames)
            pieces.append(model.transcribe(str(segment)).text)
    return ' '.join(pieces)


if __name__ == '__main__':
    # Загрузчик модели печатает служебные сообщения. stdout — только протокол JSON.
    with contextlib.redirect_stdout(sys.stderr):
        text = transcribe(Path(sys.argv[1]))
    print(json.dumps({'text': text}, ensure_ascii=False))
