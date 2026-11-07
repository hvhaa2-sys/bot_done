"""Эталон экзаменов для CI: ухудшение в любом наборе роняет проверку, улучшение — нет."""
from eval.ci_gate import worse


def base():
    return {"exams": {"questions.csv": {"decision_accuracy": 0.9, "unsafe_answers": 1, "fact_errors": 0,
                                        "content_hit": 0.8, "questions": 37}},
            "learning": {"fact_found": 19, "personal_leaked": 3, "invented_amounts": 0, "after_ok": 18, "pairs": 35}}


def test_same_or_better_passes():
    now = base()
    now["exams"]["questions.csv"] |= {"decision_accuracy": 0.95, "unsafe_answers": 0}
    now["learning"] |= {"fact_found": 20, "personal_leaked": 2}
    assert worse(now, base()) == [] and worse(base(), base()) == []


def test_any_drop_is_reported():
    now = base()
    now["exams"]["questions.csv"] |= {"decision_accuracy": 0.89, "unsafe_answers": 2}
    now["learning"] |= {"personal_leaked": 4}
    problems = worse(now, base())
    assert len(problems) == 3 and any("unsafe_answers 1 → 2" in p for p in problems)
    assert worse({"exams": {}, "learning": base()["learning"]}, base()) == ["questions.csv: набора больше нет"]
