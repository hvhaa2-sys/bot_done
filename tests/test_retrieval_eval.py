"""Метрики считают найденные источники, черновая разметка не выдаётся за эталон."""
import asyncio
from types import SimpleNamespace
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.core.knowledge import KnowledgeBase, PriceRow
from eval.retrieval_eval import (Document, EmbeddingClient, Question, VectorCache, bm25_rank, dense_rank,
                                 eligible, load_corpus, load_qrels, load_questions, metrics, rrf, run, sha, unit)


def test_recall_and_hit_are_different_and_mrr_uses_first_relevant():
    result = metrics(["noise", "b", "a"], {"a", "b", "c"}, 2)
    assert result == {"recall_at_k": 1 / 3, "hit_at_k": 1.0, "mrr": 0.5, "mrr_at_k": 0.5}
    assert metrics(["x", "a"], {"a"}, 1)["mrr_at_k"] == 0
    assert metrics(["x"], {"a"}, 8)["mrr"] == 0


def test_rrf_combines_ranks_without_duplicate_votes():
    assert rrf([["a", "a", "b"], ["b", "c"]]) == rrf([["a", "b"], ["b", "c"]])
    assert rrf([["x", "answer"], ["y", "answer"]])[0] == "answer"


def test_cosine_uses_normalized_vectors_and_checks_dimensions():
    docs = [Document("a", "a", "faq", "a", "a"), Document("b", "b", "faq", "b", "b")]
    assert dense_rank(unit([1, 0]), docs, [unit([1, 1]), unit([-1, 0])]) == ["a", "b"]
    with pytest.raises(ValueError, match="Размерности"):
        dense_rank([1], docs, [[1, 0], [1, 0]])


@pytest.mark.parametrize("vector", [[], [0, 0], [float("nan")], [float("inf")], [True], ["1"]])
def test_invalid_vectors_rejected(vector):
    with pytest.raises(ValueError):
        unit(vector)


def test_client_history_does_not_include_expected_answers(tmp_path):
    path = tmp_path / "questions.csv"
    path.write_text("dialog,question,expected,must_contain\nx,ВЕЕР экран?,answer,секретный-эталон\nx,а в декабре?,answer,другое\n")
    rows = load_questions([path])
    assert rows[1].query == "ВЕЕР экран?\nа в декабре?"
    assert "эталон" not in rows[1].query


def test_draft_qrels_are_not_ground_truth_and_stale_gold_fails(tmp_path):
    q = Question("q", "questions.csv", 2, "?", "?", "answer", "")
    doc = Document("d", "faq-0", "faq", "title", "text")
    path = tmp_path / "qrels.csv"
    path.write_text("query_id,document_id,relevance,reviewer\nq,d,1,\n")
    assert load_qrels(path, [q], [doc]) == {}
    path.write_text("query_id,document_id,relevance,reviewer\nq,changed,1,expert\n")
    with pytest.raises(ValueError, match="не соответствует"):
        load_qrels(path, [q], [doc])


def test_content_ids_survive_faq_reordering(tmp_path):
    (tmp_path / "faq.md").write_text("## Один\nПервый\n## Два\nВторой\n")
    _, before, _ = load_corpus(tmp_path)
    (tmp_path / "faq.md").write_text("## Два\nВторой\n## Один\nПервый\n")
    _, after, _ = load_corpus(tmp_path)
    assert {d.id for d in before} == {d.id for d in after}


def test_same_venue_and_client_tier_filters_for_bm25_and_vectors():
    prices = [PriceRow("Аудиоролик", "ВЕЕР Молл, Екатеринбург", "месяц", 25000, ""),
              PriceRow("Аудиоролик", "ДЕПО, Нижний Тагил", "месяц", 15000, ""),
              PriceRow("Аудиоролик для арендаторов", "ВЕЕР Молл, Екатеринбург", "месяц", 10000, "")]
    kb = KnowledgeBase([], prices, [])
    mapping = {c.id: c.id for c in kb.chunks}
    query = "Сколько стоит аудиоролик в ВЕЕР?"
    assert eligible(kb, query) == {"price-0"}
    assert bm25_rank(kb, query, mapping) == ["price-0"]
    assert "price-2" in eligible(kb, "Мы арендаторы ВЕЕР, аудиоролик?")


def test_cache_namespaces_and_no_accidental_api(tmp_path):
    async def scenario():
        path = tmp_path / "vectors.db"
        cache = VectorCache(path, "model-a")
        try:
            cache.put("text", unit([1, 2]))
            assert (await cache.vectors(["text"], None, 10))[0] == pytest.approx(unit([1, 2]))
            with pytest.raises(ValueError, match="--allow-api"):
                await cache.vectors(["new"], None, 10)
        finally:
            cache.close()
        other = VectorCache(path, "model-b")
        assert other.get("text") is None
        other.close()
    asyncio.run(scenario())


def test_embeddings_response_reordered_by_index_without_chat_call():
    settings = replace(Settings.load(), gigachat_auth_key="test", gigachat_base_url="", gigachat_verify_ssl=True)
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if "oauth" in str(request.url):
            return httpx.Response(200, json={"access_token": "fake", "expires_at": 9999999999999})
        assert request.url.path.endswith("/embeddings")
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0.0, 2.0], "usage": {"prompt_tokens": 3}},
            {"index": 0, "embedding": [3.0, 0.0], "usage": {"prompt_tokens": 2}}]})

    async def scenario():
        client = EmbeddingClient(settings, "Embeddings-2", transport=httpx.MockTransport(handler))
        try:
            assert await client.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
            assert client.tokens == 5 and client.requests == 1
        finally:
            await client.close()
    asyncio.run(scenario())
    assert len(seen) == 2


def test_complete_comparison_from_cache_without_network(tmp_path):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    (kb_dir / "faq.md").write_text("## Макет\nНужен PDF\n## Запуск\nЗапуск согласует менеджер\n")
    questions = tmp_path / "questions.csv"
    questions.write_text("question,expected,must_contain\nКакой нужен макет?,answer,PDF\n")
    kb, docs, _ = load_corpus(kb_dir)
    q = load_questions([questions])[0]
    relevant = next(d for d in docs if "PDF" in d.text)
    qrels = tmp_path / "qrels.csv"
    qrels.write_text(f"query_id,document_id,relevance,reviewer\n{q.id},{relevant.id},1,test\n")
    settings = Settings.load()
    url = (settings.gigachat_base_url or "https://gigachat.devices.sberbank.ru/api/v1").rstrip("/")
    cache_path = tmp_path / "cache.db"
    cache = VectorCache(cache_path, sha(url + "\0Embeddings-2\0"))
    for d in docs:
        cache.put(d.text, [1, 0] if d.id == relevant.id else [0, 1])
    cache.put(q.query, [1, 0])
    cache.close()
    args = SimpleNamespace(kb_dir=kb_dir, questions=[questions], out=tmp_path / "result", prepare=False,
                           qrels=qrels, methods="bm25,embeddings,rrf", allow_api=False, embedding_model="Embeddings-2",
                           query_instruction="", cache=cache_path, batch_size=10, rrf_constant=60, depth=60, k=8)
    report = asyncio.run(run(args))
    assert report["api_requests"] == 0
    assert {r["method"] for r in report["summary"]} == {"bm25", "embeddings", "rrf"}
    assert all(r["mrr"] == 1 for r in report["summary"])
