import hashlib
import io
import json

import pytest

from app import speech_models as models


def assets(monkeypatch):
    content = b'checked-model'
    monkeypatch.setattr(models, 'FILES', {'weights.bin': (len(content), hashlib.sha256(content).hexdigest())})
    return content


def test_prepare_reuses_verified_cache_without_network(tmp_path, monkeypatch):
    content = assets(monkeypatch)
    (tmp_path / 'weights.bin').write_bytes(content)
    def offline(*args, **kwargs):
        pytest.fail('Проверенная модель не должна обращаться в сеть')
    monkeypatch.setattr(models.urllib.request, 'urlopen', offline)
    assert models.prepare(tmp_path) == tmp_path


@pytest.mark.parametrize('download', [b'bad', b'x' * 100])
def test_bad_download_keeps_previous_file_and_removes_temporary(tmp_path, monkeypatch, download):
    assets(monkeypatch)
    (tmp_path / 'weights.bin').write_bytes(b'previous')
    monkeypatch.setattr(models.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(download))
    with pytest.raises(ValueError):
        models.prepare(tmp_path)
    assert (tmp_path / 'weights.bin').read_bytes() == b'previous'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['weights.bin']


def test_prepare_replaces_corrupt_cache_only_after_verification(tmp_path, monkeypatch):
    content = assets(monkeypatch)
    (tmp_path / 'weights.bin').write_bytes(b'corrupt')
    monkeypatch.setattr(models.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(content))
    models.prepare(tmp_path)
    assert (tmp_path / 'weights.bin').read_bytes() == content
    assert sorted(p.name for p in tmp_path.iterdir()) == ['weights.bin']


def test_missing_model_fails_before_torch_import_or_network(tmp_path, monkeypatch):
    assets(monkeypatch)
    monkeypatch.setenv('SPEECH_MODEL_DIR', str(tmp_path))
    with pytest.raises(RuntimeError, match='prepare_speech'):
        models.load_model()


def test_config_only_allows_installed_gigaam_classes(tmp_path):
    parts = dict(zip(('preprocessor', 'encoder', 'head', 'decoding'), models.TARGETS))
    cfg = {key: {'_target_': value} for key, value in parts.items()}
    source = tmp_path / 'config.json'
    source.write_text(json.dumps({'cfg': {'model': {'cfg': cfg}}}))
    loaded = models.model_config(tmp_path)
    assert loaded['decoding']['model_path'] == str(tmp_path / 'tokenizer.model')
    assert all(loaded[key]['_target_'].startswith('gigaam.') for key in parts)
    cfg['encoder']['_target_'] = 'os.system'
    source.write_text(json.dumps({'cfg': {'model': {'cfg': cfg}}}))
    with pytest.raises(KeyError):
        models.model_config(tmp_path)
