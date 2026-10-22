import csv
import json

from tools.import_telegram import (cluster_pairs, detect_owner, extract_pairs, load_chats, mask_pd,
                                   write_outputs)

OWNER = "user1"


def msg(i, author_id, name, date, text):
    return {"id": i, "type": "message", "date": date, "from": name, "from_id": author_id, "text": text}


def full_export(tmp_path):
    chats = [
        {"name": "Клиент А", "type": "personal_chat", "id": 10, "messages": [
            msg(1, "user10", "Клиент А", "2026-09-01T10:00:00", "Здравствуйте! Сколько стоит экран в атриуме?"),
            msg(2, OWNER, "Анна", "2026-09-01T10:05:00", "Добрый день! Экран в атриуме — 30 000 ₽ в месяц."),
            msg(3, "user10", "Клиент А", "2026-09-01T10:06:00", "Спасибо"),
            msg(4, OWNER, "Анна", "2026-09-01T10:07:00", "Обращайтесь!"),
        ]},
        {"name": "Клиент Б", "type": "personal_chat", "id": 11, "messages": [
            msg(1, "user11", "Клиент Б", "2026-09-02T12:00:00", "Сколько стоит экран в атриуме на месяц?"),
            msg(2, OWNER, "Анна", "2026-09-02T12:01:00", ["Экран в атриуме стоит ", {"type": "bold", "text": "30 000 ₽"}, " за месяц."]),
            msg(3, "user11", "Клиент Б", "2026-09-02T12:02:00", "Какие требования к макету? Мой номер +7 912 345-67-89"),
            msg(4, OWNER, "Анна", "2026-09-04T12:00:00", "Слишком поздний ответ не считается"),
        ]},
    ]
    data = {"personal_information": {"user_id": 1}, "chats": {"list": chats}}
    path = tmp_path / "result.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_full_export_pipeline(tmp_path):
    chats, owner = load_chats([full_export(tmp_path)])
    owners = detect_owner(chats, None, owner)
    assert owners == {OWNER}
    pairs = extract_pairs(chats, owners)
    # «Спасибо» отброшено, ответ через 2 суток не считается
    assert len(pairs) == 2
    assert all("30 000" in p.answer for p in pairs)
    clusters = cluster_pairs(pairs)
    assert len(clusters) == 1 and len(clusters[0].pairs) == 2

    write_outputs(pairs, clusters, tmp_path / "out")
    faq = (tmp_path / "out" / "faq_draft.md").read_text(encoding="utf-8")
    assert "встречался 2 раз" in faq and "## " in faq
    rows = list(csv.DictReader((tmp_path / "out" / "questions_real.csv").open(encoding="utf-8")))
    assert rows[0]["must_contain"] == "30 000"


def test_detect_owner_by_name(tmp_path):
    chats, _ = load_chats([full_export(tmp_path)])
    assert detect_owner(chats, "Анна", None) == {OWNER}


def test_mask_personal_data():
    text = mask_pd("Звоните +7 912 345-67-89 или 8(912)3456789, почта anna@mail.ru")
    assert "912" not in text and "@" not in text and text.count("[телефон]") == 2


def test_mask_phone_without_country_code_but_not_prices():
    text = mask_pd("мой 912 345-67-89, бюджет 1 000 000 ₽, экран 42 000 ₽")
    assert "[телефон]" in text and "1 000 000 ₽" in text and "42 000 ₽" in text


def test_response_stats_measure_wait_before_bot(tmp_path):
    from tools.import_telegram import response_stats, run_import
    chats, owner = load_chats([full_export(tmp_path)])
    stats = response_stats(chats, detect_owner(chats, None, owner))
    # Клиент А ждал 5 мин (вторник 10:00) и 1 мин; Клиент Б — 1 мин и 2 суток
    assert stats["answered"] == 4 and stats["unanswered"] == 0
    assert stats["median_wait_min"] == 3.0
    assert stats["within_5_min"] == 0.75 and stats["over_1_hour"] == 0.25
    r = run_import([full_export(tmp_path)], out_dir=tmp_path / "out")
    assert r["pairs"] == 2 and r["repeated_share"] == 1.0
    assert json.loads((tmp_path / "out" / "stats.json").read_text(encoding="utf-8"))["requests"] == 4
