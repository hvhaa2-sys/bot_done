"""Сравнение поиска без генерации ответа: BM25, GigaChat embeddings, RRF.

Первый запуск: --prepare, затем разметить relevance/reviewer в qrels.
Сеть разрешается только явным --allow-api; эмбеддинги кешируются после каждого пакета.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import sqlite3
import statistics
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from app.config import ROOT, Settings
from app.core.facts import contains
from app.core.knowledge import KnowledgeBase, SearchHit, _VENUE_ALIASES, expand_query
from app.llm.gigachat import GigaChatProvider, RETRY_DELAYS, _retry_after

DEFAULT_OUTPUT = ROOT / "eval" / "retrieval_results"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Document:
    id: str
    chunk_id: str
    kind: str
    title: str
    text: str


@dataclass(frozen=True)
class Question:
    id: str
    dataset: str
    row: int
    question: str
    query: str
    expected: str
    must_contain: str


def load_questions(paths: list[Path]) -> list[Question]:
    result = []
    for path in paths:
        histories: dict[str, list[str]] = defaultdict(list)
        with path.open(encoding="utf-8-sig", newline="") as f:
            for number, row in enumerate(csv.DictReader(f), 2):
                question = row["question"].strip()
                dialog = row.get("dialog") or ""
                previous = histories[dialog][-3:] if dialog else []
                query = "\n".join([*previous, question])
                if dialog:
                    histories[dialog].append(question)
                # В qrels фиксируем не только формулировку, но и место в сценарии.
                qid = "q-" + sha(f"{path.name}\0{number}\0{query}")[:20]
                result.append(Question(qid, path.name, number, question, query,
                                       row["expected"].strip(), row.get("must_contain") or ""))
    if len({q.id for q in result}) != len(result):
        raise ValueError("Набор вопросов указан повторно или идентификаторы совпали")
    return result


def load_corpus(kb_dir: Path) -> tuple[KnowledgeBase, list[Document], dict[str, str]]:
    def fingerprint() -> str:
        return sha("\n".join(str(p.relative_to(kb_dir)) + ":" + hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in sorted(kb_dir.rglob("*")) if p.is_file()))

    before = fingerprint()
    kb = KnowledgeBase.load(kb_dir)
    documents, mapping, seen = [], {}, set()
    for c in kb.chunks:
        doc_id = "d-" + sha(c.kind + "\0" + c.text)[:24]
        mapping[c.id] = doc_id
        if doc_id in seen:
            continue
        seen.add(doc_id)
        documents.append(Document(doc_id, c.id, c.kind, c.title, c.text))
    if not documents:
        raise ValueError("Пустая база знаний")
    if fingerprint() != before:
        raise ValueError("База изменилась во время загрузки. Повторите запуск на стабильном снимке")
    return kb, documents, mapping


def corpus_hash(documents: list[Document]) -> str:
    return sha("\n".join(sorted(d.id for d in documents)))


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_qrels(path: Path, questions: list[Question], documents: list[Document]) -> dict[str, set[str]]:
    qids, dids = {q.id for q in questions}, {d.id for d in documents}
    relevant: dict[str, set[str]] = defaultdict(set)
    grades = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            # Черновая автоматическая подсказка не становится эталоном.
            if not (row.get("reviewer") or "").strip() or not row.get("relevance", "").strip():
                continue
            if row["query_id"] not in qids or row["document_id"] not in dids:
                raise ValueError("Qrels не соответствует корпусу или вопросам. Переразметьте устаревшую строку")
            grade = int(row["relevance"])
            if grade not in (0, 1, 2):
                raise ValueError("relevance должен быть 0, 1 или 2")
            pair = row["query_id"], row["document_id"]
            if pair in grades and grades[pair] != grade:
                raise ValueError("Противоречивые оценки одной пары в qrels")
            grades[pair] = grade
            if grade:
                relevant[row["query_id"]].add(row["document_id"])
    return dict(relevant)


def eligible(kb: KnowledgeBase, query: str) -> set[str]:
    """Одинаковые фильтры площадки и арендатора для всех трёх методов."""
    tokens = expand_query([_VENUE_ALIASES.get(t, t) for t in kb.normalize(query)])
    venues = frozenset().union(*(c.venue for c in kb.chunks))
    named = venues.intersection(tokens)
    hits = [SearchHit(c, 0.0) for c in kb.chunks if c.kind != "price" or not named or c.venue & named]
    return {h.chunk.id for h in kb._for_client_type(hits, query)}


def bm25_rank(kb: KnowledgeBase, query: str, mapping: dict[str, str]) -> list[str]:
    allowed = eligible(kb, query)
    result = []
    for hit in kb.search(query, len(kb.chunks)):
        if hit.chunk.id in allowed and mapping[hit.chunk.id] not in result:
            result.append(mapping[hit.chunk.id])
    return result


def unit(vector: list[float]) -> list[float]:
    if not vector or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
        raise ValueError("API или кеш вернул неверный вектор")
    length = math.sqrt(sum(v * v for v in vector))
    if not math.isfinite(length) or length == 0:
        raise ValueError("Нулевой или неконечный вектор")
    return [v / length for v in vector]


def dense_rank(query_vector: list[float], documents: list[Document], vectors: list[list[float]],
               allowed: set[str] | None = None) -> list[str]:
    if len(documents) != len(vectors) or any(len(v) != len(query_vector) for v in vectors):
        raise ValueError("Размерности векторов не совпали")
    scored = [(sum(a * b for a, b in zip(query_vector, v)), d.id)
              for d, v in zip(documents, vectors) if allowed is None or d.chunk_id in allowed]
    return [doc_id for _, doc_id in sorted(scored, key=lambda pair: (-pair[0], pair[1]))]


def rrf(rankings: list[list[str]], constant: int = 60, depth: int = 60) -> list[str]:
    if constant < 1 or depth < 1:
        raise ValueError("RRF constant и depth должны быть положительными")
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        # Один источник не может дважды голосовать за один документ.
        unique = list(dict.fromkeys(ranking))[:depth]
        for position, doc_id in enumerate(unique, 1):
            scores[doc_id] += 1 / (constant + position)
    return sorted(scores, key=lambda doc_id: (-scores[doc_id], doc_id))


def metrics(ranking: list[str], relevant: set[str], k: int) -> dict[str, float]:
    if not relevant or k < 1:
        raise ValueError("Нужна положительная разметка и k > 0")
    ranking = list(dict.fromkeys(ranking))
    found = set(ranking[:k]) & relevant
    first = next((i for i, doc_id in enumerate(ranking, 1) if doc_id in relevant), None)
    return {"recall_at_k": len(found) / len(relevant), "hit_at_k": float(bool(found)),
            "mrr": 1 / first if first else 0.0,
            "mrr_at_k": 1 / first if first and first <= k else 0.0}


class EmbeddingClient(GigaChatProvider):
    """Повторно используем только OAuth и HTTP-транспорт, complete() не вызывается."""
    def __init__(self, settings: Settings, model: str, **kwargs):
        super().__init__(settings.gigachat_auth_key, settings.gigachat_scope, model,
                         settings.gigachat_verify_ssl, base_url=settings.gigachat_base_url, **kwargs)
        self.url = self.api_url.removesuffix("/chat/completions") + "/embeddings"
        self.requests, self.tokens = 0, 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            token = await self._get_token()
            response = await self._post(self.url, headers={"Authorization": "Bearer " + token},
                                        json={"model": self.model, "input": texts})
            self.requests += 1
            if response.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if response.status_code not in (429, 500, 502, 503, 504):
                break
            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(_retry_after(response) or RETRY_DELAYS[attempt])
        if response.status_code != 200:
            raise ValueError(f"Embeddings API: HTTP {response.status_code}. Тело ответа и ключ не выводятся")
        data = response.json()
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", -1))
        if [x.get("index") for x in items] != list(range(len(texts))):
            raise ValueError("API вернул пропущенные или повторяющиеся индексы векторов")
        self.tokens += int((data.get("usage") or {}).get("prompt_tokens") or
                           sum((x.get("usage") or {}).get("prompt_tokens", 0) for x in items))
        return [unit(item["embedding"]) for item in items]


class VectorCache:
    def __init__(self, path: Path, namespace: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS vectors (namespace TEXT, key TEXT, vector TEXT, PRIMARY KEY(namespace,key))")

    def get(self, text: str) -> list[float] | None:
        row = self.db.execute("SELECT vector FROM vectors WHERE namespace=? AND key=?", (self.namespace, sha(text))).fetchone()
        return unit(json.loads(row[0])) if row else None

    def put(self, text: str, vector: list[float]) -> None:
        self.db.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?)", (self.namespace, sha(text), json.dumps(vector)))
        self.db.commit()

    async def vectors(self, texts: list[str], client: EmbeddingClient | None, batch_size: int) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if self.get(t) is None]
        if missing and client is None:
            raise ValueError(f"В кеше нет {len(missing)} текстов. Для первого расчёта добавьте --allow-api")
        for start in range(0, len(missing), batch_size):
            batch = missing[start:start + batch_size]
            vectors = await client.embed(batch)
            for text, vector in zip(batch, vectors):
                self.put(text, vector)
            print(f"Эмбеддинги: {min(start + batch_size, len(missing))}/{len(missing)} новых текстов", flush=True)
        return [self.get(t) for t in texts]

    def close(self) -> None:
        self.db.close()


@contextmanager
def cache_lock(path: Path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Другой процесс уже считает эмбеддинги в этом кеше") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def prepare(out: Path, questions: list[Question], documents: list[Document], kb: KnowledgeBase,
            mapping: dict[str, str]) -> None:
    write_csv(out / "corpus.csv", ["id", "chunk_id", "kind", "title", "text"], [asdict(d) for d in documents])
    write_csv(out / "queries.csv", list(Question.__dataclass_fields__), [asdict(q) for q in questions])
    by_id = {d.id: d for d in documents}
    proposals = []
    for q in questions:
        if q.expected != "answer":
            continue
        ranked = bm25_rank(kb, q.query, mapping)
        facts = [x for x in q.must_contain.split(";") if x.strip() and not x.strip().startswith("!")]
        # Подсказки для человека, не qrels для метрик. Не привязываем эталон к первой выдаче BM25.
        matching = [d.id for d in documents if facts and all(contains(d.text, fact) for fact in facts)]
        candidates = list(dict.fromkeys([*matching, *ranked[:5]]))[:15]
        for doc_id in candidates:
            proposals.append({"query_id": q.id, "document_id": doc_id, "relevance": "", "reviewer": "",
                              "dataset": q.dataset, "row": q.row, "question": q.question,
                              "source": by_id[doc_id].text, "note": "Проверить источник и область действия"})
    path = out / "qrels_draft.csv"
    if not path.exists():
        write_csv(path, ["query_id", "document_id", "relevance", "reviewer", "dataset", "row", "question", "source", "note"], proposals)


def summarize(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["method"], row["dataset"]].append(row)
        groups[row["method"], "ALL"].append(row)
    result = []
    for (method, dataset), group in sorted(groups.items()):
        judged = [r for r in group if r["judged"]]
        record = {"method": method, "dataset": dataset, "questions": len(group), "judged": len(judged)}
        for name in ["recall_at_k", "hit_at_k", "mrr", "mrr_at_k"]:
            record[name] = statistics.mean(r[name] for r in judged) if judged else None
        record["retrieval_ms_median"] = statistics.median(r["retrieval_ms"] for r in group)
        result.append(record)
    return result


async def run(args) -> dict:
    settings = Settings.load()
    kb, documents, mapping = load_corpus(args.kb_dir or settings.kb_dir)
    paths = args.questions or sorted((ROOT / "eval").glob("questions*.csv"))
    questions = load_questions(paths)
    args.out.mkdir(parents=True, exist_ok=True)
    prepare(args.out, questions, documents, kb, mapping)
    if args.prepare:
        return {"corpus_hash": corpus_hash(documents), "documents": len(documents), "questions": len(questions),
                "message": "Проверьте qrels_draft.csv. Заполненные relevance и reviewer нужны для Recall/MRR"}
    qrels = load_qrels(args.qrels, questions, documents)
    if not qrels:
        raise ValueError("В qrels нет проверенных положительных источников. Сначала выполните --prepare и разметку")
    methods = args.methods.split(",")
    if not set(methods) <= {"bm25", "embeddings", "rrf"}:
        raise ValueError("Методы: bm25,embeddings,rrf")
    wants_vectors = bool({"embeddings", "rrf"} & set(methods))
    client = EmbeddingClient(settings, args.embedding_model) if wants_vectors and args.allow_api else None
    url = (settings.gigachat_base_url or "https://gigachat.devices.sberbank.ru/api/v1").rstrip("/")
    namespace = sha(url + "\0" + args.embedding_model + "\0" + args.query_instruction)
    cache = VectorCache(args.cache, namespace) if wants_vectors else None
    rows, ranks, document_vectors, query_vectors = [], [], [], []
    started = time.perf_counter()
    try:
        if wants_vectors:
            with cache_lock(args.cache.with_suffix(".lock")):
                document_vectors = await cache.vectors([d.text for d in documents], client, args.batch_size)
                query_vectors = await cache.vectors([args.query_instruction + q.query for q in questions], client, args.batch_size)
        embedded_ms = (time.perf_counter() - started) * 1000
        for i, q in enumerate(questions):
            start = time.perf_counter()
            word_rank = bm25_rank(kb, q.query, mapping)
            bm_ms = (time.perf_counter() - start) * 1000
            dense, dense_ms = [], 0.0
            if wants_vectors:
                start = time.perf_counter()
                dense = dense_rank(query_vectors[i], documents, document_vectors, eligible(kb, q.query))
                dense_ms = (time.perf_counter() - start) * 1000
            for method in methods:
                if method == "rrf":
                    start = time.perf_counter()
                    ranking = rrf([word_rank, dense], args.rrf_constant, args.depth)
                    elapsed = bm_ms + dense_ms + (time.perf_counter() - start) * 1000
                else:
                    ranking, elapsed = (word_rank, bm_ms) if method == "bm25" else (dense, dense_ms)
                relevant = qrels.get(q.id)
                row = {"query_id": q.id, "dataset": q.dataset, "row": q.row, "method": method,
                       "judged": bool(relevant), "retrieval_ms": elapsed,
                       **(metrics(ranking, relevant, args.k) if relevant else
                          dict.fromkeys(["recall_at_k", "hit_at_k", "mrr", "mrr_at_k"]))}
                rows.append(row)
                ranks.append({"query_id": q.id, "method": method, "ranking": ranking,
                              "relevant": sorted(relevant or [])})
        report = {"corpus_hash": corpus_hash(documents), "documents": len(documents), "questions": len(questions),
                  "judged_queries": len(qrels), "k": args.k, "rrf_constant": args.rrf_constant, "rrf_depth": args.depth,
                  "embedding_model": args.embedding_model if wants_vectors else None, "endpoint": url,
                  "query_instruction": args.query_instruction, "embedding_prepare_ms": embedded_ms,
                  "api_requests": client.requests if client else 0, "embedding_tokens": client.tokens if client else 0,
                  "qrels_sha256": hashlib.sha256(args.qrels.read_bytes()).hexdigest(), "summary": summarize(rows),
                  "notes": ["Retrieval-only, не качество готовых ответов", "Клиентская история: три предыдущих реплики, без ответов из эталона",
                            "Фильтры ТЦ и арендатора одинаковы", "Без разметки вопрос не входит в Recall/MRR",
                            "retrieval_ms не включает сетевое получение эмбеддинга запроса"]}
        write_csv(args.out / "metrics.csv", list(rows[0]), rows)
        (args.out / "rankings.json").write_text(json.dumps(ranks, ensure_ascii=False), encoding="utf-8")
        (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        if cache:
            cache.close()
        if client:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", nargs="+", type=Path)
    parser.add_argument("--kb-dir", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--qrels", type=Path, default=ROOT / "eval/retrieval_qrels.csv")
    parser.add_argument("--methods", default="bm25,embeddings,rrf")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--depth", type=int, default=60)
    parser.add_argument("--rrf-constant", type=int, default=60)
    parser.add_argument("--embedding-model", default="Embeddings-2")
    parser.add_argument("--query-instruction", default="")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--cache", type=Path, default=DEFAULT_OUTPUT / "vectors.sqlite3")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-api", action="store_true")
    args = parser.parse_args()
    if min(args.k, args.depth, args.rrf_constant, args.batch_size) < 1:
        parser.error("k, depth, rrf-constant и batch-size должны быть положительными")
    try:
        report = asyncio.run(run(args))
    except (ValueError, OSError, KeyError, httpx.HTTPError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")
    if args.prepare:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("method | dataset | judged/total | Recall@k | MRR | Hit@k")
        for row in report["summary"]:
            render = lambda n: "—" if n is None else f"{n:.3f}"  # noqa: E731
            print(f"{row['method']} | {row['dataset']} | {row['judged']}/{row['questions']} | "
                  f"{render(row['recall_at_k'])} | {render(row['mrr'])} | {render(row['hit_at_k'])}")
        print(f"API-запросов: {report['api_requests']}. Токенов эмбеддингов: {report['embedding_tokens']}.")


if __name__ == "__main__":
    main()
