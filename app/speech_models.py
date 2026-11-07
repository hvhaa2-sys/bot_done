"""Проверенные веса GigaAM: загрузка при подготовке, распознавание без сети."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.request
from pathlib import Path

REVISION = '7655ad717f8122257385bb4b2f373db3697e8680'
BASE_URL = f'https://huggingface.co/ai-sage/GigaAM-v3/resolve/{REVISION}'
FILES = {
    'config.json': (1867, '02361ba9cafd6c3ec66fcdd73494c3b562a60eb2a2d1b13f3cb04ae440d93e52'),
    'tokenizer.model': (255336, '828c12c991019eef952a960661f25a92d6ad279591e2ea466b4aeddf1d20a18a'),
    'pytorch_model.bin': (448928167, 'afc6dcbae8320ea56f2cddebc0f13fbf62c9d59b6ddcad899782623c8610826a'),
}
TARGETS = {
    'modeling_gigaam.FeatureExtractor': 'gigaam.preprocess.FeatureExtractor',
    'modeling_gigaam.ConformerEncoder': 'gigaam.encoder.ConformerEncoder',
    'modeling_gigaam.RNNTHead': 'gigaam.decoder.RNNTHead',
    'modeling_gigaam.RNNTGreedyDecoding': 'gigaam.decoding.RNNTGreedyDecoding',
}


def model_directory() -> Path:
    return Path(os.environ.get('SPEECH_MODEL_DIR') or Path.home() / '.cache/gigaam-hf' / REVISION).expanduser()


def verify(path: Path, size: int, digest: str) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest() == digest


def prepare(directory: Path | None = None) -> Path:
    """Пишем через временный файл: оборванная загрузка не портит предыдущую модель."""
    directory = directory or model_directory()
    directory.mkdir(parents=True, exist_ok=True)
    for name, (size, digest) in FILES.items():
        target = directory / name
        if verify(target, size, digest):
            continue
        print(f'Загрузка {name} ({size // 1024} КБ)', flush=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
                temporary = Path(output.name)
                # URL состоит только из закреплённой ревизии и имён FILES; после загрузки проверяется SHA-256.
                with urllib.request.urlopen(f'{BASE_URL}/{name}', timeout=60) as source:  # nosec B310
                    remaining = size
                    while block := source.read(min(1024 * 1024, remaining + 1)):
                        remaining -= len(block)
                        if remaining < 0:
                            raise ValueError(f'Превышен размер файла модели: {name}')
                        output.write(block)
            if not verify(temporary, size, digest):
                raise ValueError(f'Не совпала контрольная сумма модели: {name}')
            temporary.chmod(0o644)
            os.replace(temporary, target)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return directory


def model_config(directory: Path) -> dict:
    config = json.loads((directory / 'config.json').read_text())['cfg']['model']['cfg']
    for part in ('preprocessor', 'encoder', 'head', 'decoding'):
        config[part]['_target_'] = TARGETS[config[part]['_target_']]
    config['decoding']['model_path'] = str(directory / 'tokenizer.model')
    config['encoder']['flash_attn'] = False
    return config


def load_model():
    directory = model_directory()
    for name, (size, digest) in FILES.items():
        if not verify(directory / name, size, digest):
            raise RuntimeError(f'Модель отсутствует или повреждена: {name}. Запустите python -m tools.prepare_speech')
    import torch
    from gigaam.model import GigaAMASR
    from omegaconf import OmegaConf

    # Репозиторий HF отдаёт веса обёртки с префиксом model.; удалённый Python-код не исполняется.
    state = torch.load(directory / 'pytorch_model.bin', map_location='cpu', weights_only=True, mmap=True)
    model = GigaAMASR(OmegaConf.create(model_config(directory)))
    model.load_state_dict({key.removeprefix('model.'): value for key, value in state.items()})
    return model.eval().to('cpu')
