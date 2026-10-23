"""Нормализация русского текста для поиска: токенизация и стемминг.

Стемминг — Snowball (чистый Python, без словарей). Самодельный стеммер,
который отрезал окончания по списку, путал существительные с глаголами:
«макет» → «мак», а «макета» → «макет», и поиск не находил строку прайса.
Для исследовательского блока Snowball можно сравнить с эмбеддингами или pymorphy3.
"""
from __future__ import annotations

import re
from functools import lru_cache

import snowballstemmer

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_STEMMER = snowballstemmer.stemmer("russian")

_STOPWORDS = set(
    """и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только
    ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть
    был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где есть надо
    ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот
    того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда
    зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас
    про всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть
    том нельзя такой им более всегда конечно всю между здравствуйте добрый день привет подскажите
    скажите пожалуйста вопрос сколько реклама рекламу рекламы рекламе рекламой
    две обе оба четыре пять шесть семь восемь девять десять""".split()
)


# Реплики без вопроса. «Да» и «нет» сюда не входят: это ответ на вопрос бота, его разбирает менеджер.
_GREETINGS = set("""здравствуйте здравствуй здрасте здрасьте привет приветствую добрый доброе
    доброго день утро вечер времени суток hello hi""".split())
# «Договорились» сюда не входит: это согласие на сделку, его должен увидеть менеджер
_CLOSINGS = set("""спасибо спс благодарю благодарим ок окей ok хорошо понял поняла поняли ясно
    понятно отлично супер класс до свидания всего доброго пока принято принял приняла
    ооо ого вау круто здорово огонь""".split())
_FILLERS = set("и а вам вас тебе большое огромное очень еще раз всем".split())


def small_talk(text: str) -> str | None:
    """«greeting» — только приветствие; «skip» — спасибо, «ок», эмодзи: отвечать по существу не на что."""
    words = [w.replace("ё", "е") for w in _TOKEN_RE.findall(text.lower())]
    if not words:
        # «你好，广告多少钱？» — вопрос на другом языке, не эмодзи: пусть увидит менеджер
        return None if re.search(r"[^\W\d_]", text) or "?" in text else "skip"
    if set(words) <= {"алло", "ау", "вы", "тут", "здесь", "есть", "кто", "на", "связи"}:
        return "greeting"
    if len(words) > 6:
        return None
    if all(w in _GREETINGS | _FILLERS for w in words) and any(w in _GREETINGS for w in words):
        return "greeting"
    if all(w in _CLOSINGS | _FILLERS for w in words):
        return "skip"
    return None


def starts_with_greeting(text: str) -> bool:
    """«Привет нуб», «Добрый вечер, Глеб» — короткое приветствие с лишним словом, без вопроса."""
    words = [w.replace("ё", "е") for w in _TOKEN_RE.findall(text.lower())]
    return bool(words) and words[0] in _GREETINGS and len(words) <= 4 and "?" not in text


def is_short_reaction(text: str) -> bool:
    """«Гениально 👏», «Ого, давайте» — реакция, а не вопрос: «Уточню и вернусь» на неё звучит странно."""
    return len(_TOKEN_RE.findall(text)) <= 3 and "?" not in text


# Модель иногда пишет русское слово с латинскими буквами-двойниками: «ВЕER Молл»
_LATIN_TO_CYRILLIC = str.maketrans("AaBEeKMHOoPpCcTXxYyR", "АаВЕеКМНОоРрСсТХхУуР")
_MIXED_WORD_RE = re.compile(r"\b(?=\w*[а-яё])(?=\w*[a-z])\w+\b", re.IGNORECASE)


# Буквы без двойника: «Минimаlный» — i, m, l меняем по звучанию, раз слово всё равно русское
_LATIN_SOUND = str.maketrans("abdefgiklmnorstuvzBDFGILNSUVZ", "абдефгиклмнорстувзБДФГИЛНСУВЗ")


def fix_mixed_script(text: str) -> str:
    return _MIXED_WORD_RE.sub(lambda m: m.group(0).translate(_LATIN_TO_CYRILLIC).translate(_LATIN_SOUND), text)


_TRANSLIT = [("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"),
             ("yu", "ю"), ("ya", "я"), ("yo", "ё"), ("ye", "е")] + list(zip("abvgdezijklmnoprstufhcyx", "абвгдезийклмнопрстуфхцых"))
_TRANSLIT_WORDS = set("privet nuzhna nuzhno nuzhen reklama reklamu ekran ekrane ekranah ekrany skolko stoit "
                       "stoimost tsena cena nedeli nedelyu mesyats mesyac rolik rolika sdelat".split())
_LATIN_NAMES = set("ozon wildberries telegram youtube instagram whatsapp".split())


def translit_word(word: str) -> str:
    """Одно слово латиницей → кириллицей, без проверок from_translit: «Ozon» → «озон»."""
    out = word.lower()
    for latin, cyr in _TRANSLIT:
        out = out.replace(latin, cyr)
    return out


def from_translit(text: str) -> str:
    """Русский транслит, включая смешанные сообщения с двумя узнаваемыми словами.
    Английские названия и короткие фразы вроде «screen?» сами по себе не переводим."""
    words = re.findall(r"[a-z]{2,}", text, re.IGNORECASE)
    if len(words) < 3:
        return text
    if re.search(r"[а-яё]", text, re.IGNORECASE):
        if sum(word.lower() in _TRANSLIT_WORDS for word in words) < 2:
            return text

        def convert(match: re.Match) -> str:
            word = match.group()
            if word.lower() in _LATIN_NAMES or word.isupper():
                return word
            return translit_word(word)

        return re.sub(r"\b[a-z]+\b", convert, text, flags=re.IGNORECASE) \
            .replace("сколко", "сколько").replace("екран", "экран")
    out = text.lower()
    for latin, cyr in _TRANSLIT:
        out = out.replace(latin, cyr)
    return out.replace("сколко", "сколько").replace("екран", "экран")


STOPWORDS = _STOPWORDS  # для нормализации запросов в knowledge.py


# Snowball режет формы одного слова по-разному: «экран», «экраны» → экра, а «экранов» → экран.
# Вопрос «экраны» не находил текст «экранов» — сводим к одной основе
_STEM_FIX = {"экра": "экран", "площадок": "площадк", "каток": "катк", "дня": "ден", "дне": "ден"}


@lru_cache(maxsize=50_000)
def stem(word: str) -> str:
    token = _STEMMER.stemWord(word.lower().replace("ё", "е"))
    return _STEM_FIX.get(token, token)


def tokenize(text: str) -> list[str]:
    tokens = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw in _STOPWORDS:
            continue
        tokens.append(stem(raw))
    return tokens
