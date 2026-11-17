"""GIGACHAT_CA_BUNDLE включает проверку TLS по явному файлу, не меняя поведение по умолчанию."""
from __future__ import annotations

from app.llm.gigachat import GigaChatProvider


def _client_kwargs(monkeypatch, **provider_kwargs):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("app.llm.gigachat.httpx.AsyncClient", FakeClient)
    GigaChatProvider("key", "scope", "model", **provider_kwargs)
    return captured


def test_ca_bundle_takes_precedence_over_verify_flag(monkeypatch, tmp_path):
    bundle = str(tmp_path / "russian_trusted_root_ca.pem")
    assert _client_kwargs(monkeypatch, verify_ssl=False, ca_bundle=bundle)["verify"] == bundle


def test_without_ca_bundle_keeps_previous_verify_flag(monkeypatch):
    assert _client_kwargs(monkeypatch, verify_ssl=False)["verify"] is False
    assert _client_kwargs(monkeypatch, verify_ssl=True)["verify"] is True
