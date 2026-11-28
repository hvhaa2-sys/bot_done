"""Ядро ассистента: поиск по базе знаний, генерация ответа, проверки и решение.

Конвейер:
1. Стоп-темы (скидки, договор, оплата...) — сразу к менеджеру, модель не вызывается.
2. Поиск BM25 по FAQ и прайсу. Нет релевантных фрагментов — к менеджеру.
3. Генерация ответа моделью строго по найденным фрагментам, ответ в JSON
   с оценкой уверенности.
4. Защита цен: суммы, которых нет в прайсе, — к менеджеру.
5. Порог уверенности: ниже порога — к менеджеру.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from ..llm.base import LLMProvider
from .conversation import MAX_CLARIFICATIONS, Conversation
from .answer_grounding import billing_periods, budget_issues, check_price_periods, price_term_suffix, property_issues
from .client_intent import active_request, is_ready_to_buy
from .knowledge import KnowledgeBase, PriceRow, SearchHit, _CITY_TOKENS, _format_words, format_rub, parse_budget
from .price_guard import (drop_bracket_totals, computed_totals, drop_numbers, extract_amounts, extract_numbers,
                          row_multipliers, ungrounded_numbers,
                          unknown_amounts,
                          unsupported_inclusions)
from .text import fix_mixed_script, from_translit, small_talk, starts_with_greeting, tokenize, translit_word

ANSWER = "answer"
ESCALATE = "escalate"
SKIP = "skip"          # отвечать не нужно: «спасибо», «ок», эмодзи

# Причины передачи менеджеру
R_STOP_TOPIC = "stop_topic"
R_NO_CONTEXT = "no_context"
R_UNGROUNDED = "ungrounded"
R_LLM_ERROR = "llm_error"
R_MODEL_UNSURE = "model_unsure"
R_LOW_CONFIDENCE = "low_confidence"
R_PRICE_MISMATCH = "price_mismatch"
R_GREETING = "greeting"
R_SMALL_TALK = "small_talk"
R_LIMIT = "limit"
R_TERMS = "terms_mismatch"
R_VAT_UNKNOWN = "vat_unknown"  # в строке прайса НДС не указан: итоговую сумму подтверждает менеджер
R_VENUE = "venue_mismatch"
R_PERIOD = "period_mismatch"
R_FORMAT = "format_mismatch"
R_ORDER = "order_talk"
R_OFF_RANGE = "off_range"
R_CATALOG = "catalog"
R_FOREIGN = "foreign_words"
R_CLARIFICATION = "clarification"
R_CLARIFICATION_LIMIT = "clarification_limit"
_LATIN_WORD_RE = re.compile(r"\b[A-Za-z]{3,}\b")
_AVAILABILITY_RE = re.compile(r"\bсвобод(?:ен|на|но|ны)\b|\bзанят(?:а|о|ы)?\b|\bзанятост\w*"
                              r"|\b(?:есть|какие|остал\w*|нужны)\b[^.!?]{0,35}\bсвободн\w*\s+"
                              r"(?:мест|экран|дат|период|витрин)", re.I)

# Явная граница периода в вопросе о занятости. Ничего не вычисляем из одной даты.
_MONTH_NAME = r"(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
_OPEN_DATE = re.compile(rf"\b(с|со|от|по|до)\s+\d{{1,2}}(?:\s+{_MONTH_NAME}(?:\s+\d{{4}})?|"
                        r"\.\d{1,2}(?:\.\d{4})?)\b(?!\.\d)", re.I)
_OTHER_DATE = re.compile(rf"\b(?:с|со|от|по|до|между|завтра|сегодня|вчера)\b|"
                         rf"\d{{1,2}}(?:\s+{_MONTH_NAME}\b|\.\d{{1,2}}\b)|(?<!\w)[–—-]\s*\d|\d\s*[–—-]|"
                         r"\b(?:на|через|\d+)\s+(?:\d+\s+)?(?:день|дня|дней|недел\w*|месяц\w*|сутк\w*)\b|"
                         r"\b(?:или|и)\s+\d", re.I)

# Длиннее в модель не отправляем: простыня текста — это лишние токены, а не лучший ответ
MAX_QUESTION_CHARS = 1500
MAX_HISTORY_CHARS = 600

FORMAT_REMINDER = ("Оформи свой предыдущий ответ строго в JSON из инструкции: "
                   '{"answer": "...", "confidence": число, "needs_manager": true/false}')

BILLBOARD_MESSAGE = ("{missing} у нас нет, но есть уличные экраны в Екатеринбурге: {street}. "
                     "Цены с НДС. Рассказать подробнее про любой?")
# «билборды, метро, ТВ — что есть?» — назвать всё, чего нет, а не только билборды
_ALSO_MISSING = ((re.compile(r"\bметро\b", re.IGNORECASE), "рекламы в метро"),
                 (re.compile(r"телевиден|\bтв\b|перв\w* канал", re.IGNORECASE), "рекламы на ТВ"),
                 (re.compile(r"таргет|вконтакт|\bвк\b|инстаграм|\bсмм\b|директ|блогер", re.IGNORECASE), "интернет-рекламы"),
                 (re.compile(r"радиостанц", re.IGNORECASE), "рекламы на радиостанциях"),
                 (re.compile(r"газет|журнал", re.IGNORECASE), "рекламы в прессе"))
_BILLBOARD_RE = re.compile(r"билборд|наружн\w* реклам|наружк|рекламн\w* щит", re.IGNORECASE)
OFF_RANGE_MESSAGE = ("Такой рекламы у нас нет — мы размещаем рекламу в торговых центрах: {places}. "
                     "Подскажите, какой город интересует, — подберу формат.")

CATALOG_MESSAGE = ("Мы размещаем рекламу в торговых центрах: {places}. "
                   "Подскажите, какой город или формат интересует, — расскажу подробнее и назову цены.")

HOLDING_MESSAGE = "Спасибо за вопрос! Уточню детали и вернусь к вам в ближайшее время."
GREETING_MESSAGE = "Здравствуйте! Подскажите, что вас интересует, — расскажу о форматах рекламы и ценах."

SYSTEM_PROMPT = """Ты — помощник менеджера по продаже рекламы в торговых центрах. \
Отвечаешь клиентам в мессенджере от имени менеджера.

Правила:
- Отвечай ТОЛЬКО по фактам из раздела КОНТЕКСТ. Ничего не придумывай.
- Цены называй только так, как они записаны в контексте. Не складывай, не умножай \
и не пересчитывай суммы, не обещай скидок. Спрашивают про несколько месяцев или итог \
с печатью и монтажом — назови цену за период из прайса и отдельно доплаты, итог не считай.
- Цены в разных ТЦ разные. Называй цену вместе с ТЦ и местом из той же строки контекста. \
Если клиент не указал ТЦ, а подходящие позиции есть в нескольких ТЦ, коротко перечисли \
варианты с ТЦ или спроси, какой ТЦ интересует.
- Свойство одного экрана не подтверждает свойства другого: звук, ориентация, длительность и разрешение \
должны относиться к указанному ТЦ, формату и месту. Если для нужного экрана сведений нет, не угадывай.
- Период цены бери из той же строки прайса: месячная цена не становится двухнедельной. \
Если тарифа на запрошенный срок нет, назови известный тариф с его настоящим периодом или передай вопрос.
- Печать, монтаж и другие суммы из примечаний оплачиваются отдельно от размещения, \
если в строке прямо не написано «включено». Сезонные наценки и коэффициенты относятся \
только к тем позициям, у которых они написаны. Сезонную цену называй, только если она записана \
в строке; если указан лишь коэффициент или процент — назови его словами, сумму не пересчитывай.
- Не обещай, что место свободно: занятость подтверждает менеджер.
- Ты пишешь от имени менеджера, поэтому не отправляй клиента «уточнить у менеджера». \
Если на сам вопрос в контексте ответа нет и его нужно уточнить или прислать позже \
(занятость мест, статистика по конкретному месту, подробные требования), верни "needs_manager": true.
- Если ответа нет в контексте, клиент спрашивает про место, формат или услугу, \
которых нет в контексте, или вопрос требует решения менеджера — не отвечай по существу \
и не советуй других подрядчиков, а верни в JSON "needs_manager": true. Исключение: если в контексте \
есть строки «Все форматы», а клиент спрашивает про рекламу или услугу, которой там нет (метро, ТВ, \
билборды, таргет, съёмка роликов), честно ответь, что такой у нас нет, и предложи рекламу в ТЦ из этих строк.
- Если в контексте есть строки «Все форматы», а клиент спрашивает, что у вас есть, или хочет рекламу, \
но не выбрал ТЦ, перечисли площадки и форматы из них через запятую (похожие объединяй) и предложи \
рассказать подробнее о любом. Цены называй только из строк «Прайс».
- Клиент называет бюджет — перечисли позиции из прайса, которые в него укладываются, с ценами.
- Пиши вежливо, коротко (2–4 предложения), без markdown, на «{address}».
- Если вариант не подходит клиенту по сроку или бюджету, не ограничивайся «нет»: назови 1–2 подходящие \
позиции из контекста с ценами.
- О пользе рекламы говори только то, что есть в контексте. Не обещай рост продаж, окупаемость и проценты.
- Если ответ полный, можно закончить одним коротким вопросом о том, чего клиент ещё не назвал \
(ТЦ, срок, бюджет, вид бизнеса), но не про занятость мест. Если клиент спросил цену конкретного места, \
вопрос не задавай.
- Сам ничего не присылай и не обещай прислать (фото мест, КП, макеты, документы): если клиент об этом \
просит, верни "needs_manager": true.

Верни ТОЛЬКО JSON без пояснений:
{{"answer": "текст для клиента", "confidence": число от 0 до 1, "needs_manager": true/false}}

КОНТЕКСТ:
{context}"""


@dataclass
class Reply:
    decision: str                 # answer | escalate
    text: str                     # текст для клиента (или черновик для менеджера)
    confidence: float
    reason: str = ""
    sources: list[str] = field(default_factory=list)
    latency_ms: int = 0
    raw: str = ""
    llm_called: bool = False      # обращались ли к модели — для учёта расхода и лимитов
    tokens: int = 0

    @property
    def is_answer(self) -> bool:
        return self.decision == ANSWER


class Assistant:
    check_requested_facts = False

    def __init__(self, kb: KnowledgeBase, llm: LLMProvider, *, min_score: float = 1.0,
                 confidence_threshold: float = 0.7, top_k: int = 8,
                 holding_message: str = HOLDING_MESSAGE, greeting_message: str = GREETING_MESSAGE,
                 address: str = "вы"):
        self.kb, self.llm = kb, llm
        self.address = address  # «вы» или «ты» — как менеджер обычно пишет клиентам
        self.holding_message, self.greeting_message = holding_message, greeting_message
        self.min_score, self.confidence_threshold, self.top_k = min_score, confidence_threshold, top_k

    async def answer(self, question: str, history: list[tuple[str, str]] | None = None,
                     allow_llm: bool = True, last_question: str = "",
                     conversation: Conversation | None = None) -> Reply:
        """allow_llm=False — дневной лимит модели исчерпан: отвечаем только тем, что не требует модели.
        last_question — прошлое сообщение клиента в любом статусе (в истории только то, что ушло клиенту)."""
        started = time.perf_counter()
        full = question.strip()
        request = active_request(full)
        # Стоп-тема в конце длинного склеенного сообщения не должна потеряться при обрезке
        topic = self.kb.matched_stop_topic(full) or self.kb.matched_stop_topic(from_translit(full))
        if _AVAILABILITY_RE.search(from_translit(request)):
            topic = topic or "занятость"
        question = clip(request, MAX_QUESTION_CHARS)
        history = [(clip(q, MAX_HISTORY_CHARS), clip(a, MAX_HISTORY_CHARS)) for q, a in history or []]
        memory = conversation is not None or bool(history) or bool(last_question)
        state = conversation if conversation is not None else Conversation.replay(self.kb, [
            {"question": q, "answer": a, "decision": ANSWER, "reason": None, "status": "sent_auto"} for q, a in history])
        if last_question and (not history or last_question != history[-1][0]) and conversation is None:
            state.observe(self.kb, clip(last_question, MAX_HISTORY_CHARS))
        state.observe(self.kb, question)
        scoped_budget = state.placement_budget is not None or state.preparation_budget is not None
        query = state.query(question) if memory or scoped_budget else from_translit(question)
        if state.changed:
            history = []  # Ответ про старый ТЦ не подтверждает цены и условия нового.
        from .comparison import comparison_reply

        reply = comparison_reply(self, state, question) if not topic and len(request) <= MAX_QUESTION_CHARS else None
        if reply is None:
            reply = await self._answer(question, history, allow_llm, topic, state, query)
        state.finish(reply.decision, reply.reason)
        reply.latency_ms = int((time.perf_counter() - started) * 1000)
        return reply

    async def _answer(self, question: str, history: list[tuple[str, str]], allow_llm: bool = True,
                      topic: str | None = None, state: Conversation | None = None, query: str = "") -> Reply:
        # Приветствие без вопроса — здороваемся; «спасибо», «ок», эмодзи — молчим.
        # Иначе на «Здравствуйте» клиент получил бы «Уточню и вернусь».
        talk = small_talk(question)
        if talk == "greeting":
            return Reply(ANSWER, self.greeting_message, 1.0, R_GREETING)
        if talk == "skip":
            return Reply(SKIP, "", 1.0, R_SMALL_TALK)

        topic = topic or self.kb.matched_stop_topic(question)
        if topic:
            reason = f"{R_STOP_TOPIC}:{topic}"
            if topic == "занятость" and (edge := _OPEN_DATE.search(question)):
                rest = question[:edge.start()] + " " + question[edge.end():]
                if not _OTHER_DATE.search(rest):
                    reason = "missing_period_end" if edge[1].lower() in ("с", "со", "от") else "missing_period_start"
            return Reply(ESCALATE, self.holding_message, 0.0, reason)
        if _BILLBOARD_RE.search(question) and not self.kb._venues_in(question):
            street = "; ".join("{} ({}) — {} за {}{}".format(
                re.sub(r" *\(.*?\)", "", r.placement.split(" — ")[0]), r.placement.split(" — ")[-1],
                format_rub(r.price_rub), r.period, _season_suffix(r)) for r in self.kb.street_screens())
            missing = ["Билбордов", *(name for rx, name in _ALSO_MISSING if rx.search(question))]
            missing_text = missing[0] if len(missing) == 1 else f"{', '.join(missing[:-1])} и {missing[-1]}"
            return Reply(ANSWER, BILLBOARD_MESSAGE.format(missing=missing_text, street=street), 1.0, R_OFF_RANGE)
        if self.kb.off_range_only(question):
            # Решение пользователя 18.10: про метро, ТВ, билборды — честное «нет» и наши площадки
            places = "; ".join(c.title for c in self.kb.catalog)
            return Reply(ANSWER, OFF_RANGE_MESSAGE.format(places=places), 1.0, R_OFF_RANGE)

        state = state if state is not None else Conversation()
        query = query or from_translit(question)
        if state.venue and "infodesk" in self.kb.normalize(question):
            venue = self.kb._venues_in(state.venue) - _CITY_TOKENS
            available = [r for r in self.kb.prices if self.kb._venues_in(r.placement) & venue]
            if available and not any("infodesk" in r.format.lower() for r in available):
                navigation = [r for r in available if re.search(r"навигац|терминал", r.format, re.I)]
                if re.search(r"навигац|терминал", question, re.I) and 1 <= len(navigation) <= 3:
                    text = f"В прайсе {state.venue} указан формат «{navigation[0].format}».\n"
                    text += "\n".join(list_item(r, with_venue=False) for r in navigation)
                    sources = [f"price-{i}" for i, r in enumerate(self.kb.prices) if r in navigation]
                    return Reply(ANSWER, text, 1.0, "navigation_format", sources)
                return self._clarify(state, ("position", f"В прайсе {state.venue} нет позиции с названием infodesk. "
                                             "Подскажите, какое место или устройство вы имеете в виду?"))
        hits = self.kb.retrieve(query, self.top_k, self.min_score)
        if self.check_requested_facts:
            if fact := self.kb.missing_requested_fact(question, hits):
                return Reply(ESCALATE, self.holding_message, 0.0, f"missing_fact:{fact}")
        if state.budget and (parse_budget(question.lower()) or re.search(
                r"бюджет|влез|уклад|покаж|сколько|стоим|цен|за\s+\d|\bдо\s+\d|не дороже|не больше", question, re.I)):
            from .price_options import fits_budget

            eligible = [h for h in hits if h.chunk.kind == "price"
                        and fits_budget(self.kb.prices[int(h.chunk.id.split("-")[1])], query, state.budget)]
            if not eligible:
                return Reply(ANSWER, f"В текущем прайсе нет подходящих вариантов под бюджет "
                             f"{format_rub(state.budget)} с указанными сроком и условиями. "
                             "Можно рассмотреть другой срок, формат или бюджет.", 1.0, "no_budget_options")
        if clarification := state.clarification(self.kb, question, hits=hits):
            return self._clarify(state, clarification)
        if len(hits) > 1 and all(h.chunk.kind == "catalog" for h in hits):
            # «Хочу рекламу» без ТЦ и формата — в контексте один каталог: модель то перечисляла, то спрашивала город
            places = "; ".join(h.chunk.title for h in hits)
            return Reply(ANSWER, CATALOG_MESSAGE.format(places=places), 1.0, R_CATALOG, [h.chunk.id for h in hits])
        if not hits:
            if starts_with_greeting(question):  # «Привет нуб» — здороваемся, а не «уточню и вернусь»
                return Reply(ANSWER, self.greeting_message, 1.0, R_GREETING)
            if clarification := state.clarification(self.kb, question, no_context=True):
                return self._clarify(state, clarification)
            return Reply(ESCALATE, self.holding_message, 0.0, R_NO_CONTEXT)

        context = render_context(hits)
        system = SYSTEM_PROMPT.format(context=context, address=self.address)
        if parameters := state.parameters():
            system += f"\n\nПараметры запроса, названные клиентом в этом разговоре: {parameters}."
        messages: list[dict] = []
        for q, a in history:
            # Прошлые ответы — в том же JSON, что мы просим: увидев их простым текстом,
            # модель продолжает разговор простым текстом, и ответ не разобрать
            reply_json = json.dumps({"answer": a, "confidence": 1, "needs_manager": False}, ensure_ascii=False)
            messages += [{"role": "user", "content": q}, {"role": "assistant", "content": reply_json}]
        messages.append({"role": "user", "content": question})

        sources = [h.chunk.id for h in hits]
        if not allow_llm:
            return Reply(ESCALATE, self.holding_message, 0.0, R_LIMIT, sources)
        tokens = 0
        try:
            raw = await self.llm.complete(system, messages)
            tokens += self._spent(system, question, raw)
            if not _JSON_RE.search(raw or ""):
                # GigaChat иногда отвечает простым текстом: без needs_manager такой ответ клиенту не отдать.
                # Один раз просим переоформить — дешевле, чем отдавать менеджеру нормальный ответ
                messages += [{"role": "assistant", "content": raw}, {"role": "user", "content": FORMAT_REMINDER}]
                raw = await self.llm.complete(system, messages)
                tokens += self._spent(system, question, raw)
        except Exception as exc:  # сеть, лимиты, ключ
            return Reply(ESCALATE, self.holding_message, 0.0, f"{R_LLM_ERROR}:{type(exc).__name__}", sources,
                         llm_called=True, tokens=tokens)
        ready = is_ready_to_buy(from_translit(question))
        reply = self._decide(raw, query, history, context, sources, hits, state, ready)
        if reply.decision == ESCALATE and (fix := retry_hint(reply.reason, reply.text, self.holding_message, query)):
            # Проверка нашла конкретную ошибку — одна попытка исправить: модель получает, что не так,
            # новый ответ проходит те же проверки. Не вышло — к менеджеру с первым черновиком
            try:
                messages += [{"role": "assistant", "content": raw}, {"role": "user", "content": fix}]
                raw2 = await self.llm.complete(system, messages)
                tokens += self._spent(system, question, raw2)
                second = self._decide(raw2, query, history, context, sources, hits, state, ready)
                unsure = reply.reason.startswith(R_MODEL_UNSURE)
                if second.decision == ANSWER and not (unsure and _NO_DATA_RE.search(second.text)):
                    reply = second
            except Exception:  # noqa: BLE001 — повтор необязателен, первый ответ уже ушёл бы менеджеру
                pass
        reply.llm_called, reply.tokens = True, tokens
        return reply

    def _clarify(self, state: Conversation, clarification: tuple[str, str]) -> Reply:
        if state.clarifications >= MAX_CLARIFICATIONS:
            return Reply(ESCALATE, self.holding_message, 0.0, R_CLARIFICATION_LIMIT)
        slot, text = clarification
        return Reply(ANSWER, text, 1.0, f"{R_CLARIFICATION}:{slot}")

    def _row_multipliers(self, hits: list[SearchHit]) -> dict[int, set[float]]:
        """Цена строки → её сезонные коэффициенты (только из той же строки: коэффициент кино к машине не применяем)."""
        out: dict[int, set[float]] = {}
        for h in hits:
            if h.chunk.kind == "price":
                row = self.kb.prices[int(h.chunk.id.split("-")[1])]
                if ms := row_multipliers(row.notes):
                    out.setdefault(row.price_rub, set()).update(ms)
        return out

    def _spent(self, system: str, question: str, raw: str) -> int:
        # Точный расход берём из ответа API, если модель его вернула; иначе грубая оценка ~3 символа на токен
        return getattr(self.llm, "last_tokens", 0) or (len(system) + len(question) + len(raw or "")) // 3

    def _decide(self, raw: str, question: str, history: list[tuple[str, str]], context: str,
                sources: list[str], hits: list[SearchHit] | None = None, state: Conversation | None = None,
                ready: bool = False) -> Reply:
        """Что делать с ответом модели: отправить, передать менеджеру или отбросить.
        ready — клиент пишет о готовности к заказу («ок берём на месяц»)."""
        text, confidence, needs_manager = parse_model_output(raw)
        # Подпись из контекста («Условия и доплаты: возможна промо-стойка») клиенту не нужна
        text = _LABEL_RE.sub(lambda m: m.group(1).upper(), fix_mixed_script(text))
        if not text:
            return Reply(ESCALATE, self.holding_message, confidence, R_MODEL_UNSURE, sources, raw=raw)
        # Бот пишет от имени менеджера: «уточнит менеджер» значит, что ответа нет,
        # а обещание без уведомления менеджера никто не выполнит. Но если модель сама не просила
        # менеджера, ответ полный, а «свяжитесь с менеджером» дописано в конце — просто убираем хвост
        # «Отправьте список сотрудников менеджеру» — инструкция из базы, а не «уточнит менеджер»: её не считаем
        probe = hide_send_to_manager(text, context)
        if _MENTIONS_MANAGER_RE.search(probe):
            text, needs_manager = drop_manager_tail(probe) if not needs_manager else (text, True)
            text = text.replace(_HIDDEN_MANAGER, "менеджер")

        # «…280 000 ₽ в месяц. Хотите забронировать даты?» — ответ полный, а бронь решает менеджер:
        # заключительный вопрос-предложение убираем, а не отдаём весь ответ менеджеру
        text = drop_offer_question(text)
        # «Вы выбрали… Сумма к оплате 42 000 ₽» — это уже оформление сделки, а цена в чате может быть офертой.
        # Без повтора: подсказка модели вернула бы тот же итог, клиент ждёт человека
        text, order_left = drop_order_talk(text, context, ready)
        if order_left:
            return Reply(ESCALATE, text, confidence, R_ORDER, sources, raw=raw)
        # Обещание без слова «менеджер»: «я уточню и напишу», «место свободно», «забронирую»
        if _COMMITMENT_RE.search(text):
            needs_manager = True
        # Источник фактов — текущая база. Прошлый ответ мог относиться к другому ТЦ.
        # Слова клиента — не источник фактов:
        # «менеджер сказал, печать входит?» не должно разрешать ответ «да, входит»
        known = context
        # «Реклама значительно повысит прибыль», «окупится за месяц», «30% посетителей» — обещаний эффекта
        # в базе нет: такие фразы вырезаем, а если без них ответа не остаётся — к менеджеру
        text, claimed = drop_claims(text, known)
        text = drop_bracket_totals(text).lstrip(" .,!?:;—–-")
        if claimed and len(text) < _MANAGER_TAIL_MIN:
            needs_manager = True
        # Сумма в рублях — только из контекста (не из всего прайса: так цена экрана проходила
        # как цена печати) или бюджет, который назвал сам клиент
        price_context = "\n".join(h.chunk.text for h in hits if h.chunk.kind == "price") if hits is not None else known
        allowed = set(extract_amounts(price_context)) | {parse_budget(question.lower()) or 0}
        if state and state.budget:
            allowed.add(state.budget)
        # Числа от 1000 без «₽» («27 тысяч») — из текущего контекста или запроса.
        seen = " ".join([known, question])
        bad = sorted(set(unknown_amounts(text, allowed)) | set(ungrounded_numbers(text, seen)))
        # «Умножьте эту сумму на три» — считать клиенту тоже не предлагаем
        if _ARITHMETIC_HINT_RE.search(text) and extract_amounts(_ARITHMETIC_HINT_RE.sub("", text)):
            text = re.sub(r"([.!?])(?=[А-ЯЁA-Z])", r"\1 ", _ARITHMETIC_HINT_RE.sub("", text).strip())
        multipliers = self._row_multipliers(hits or [])
        if bad and computed_totals(bad, known, question, multipliers):
            # Модель посчитала итог из настоящих цен — вырезаем итог, цена за период остаётся
            trimmed = drop_numbers(text, bad)
            if extract_amounts(trimmed):
                text, bad = trimmed, []
        # «…около 18,7 тыс. persons» — английское слово, которого нет ни в базе, ни у клиента
        if foreign := sorted({w for w in _LATIN_WORD_RE.findall(text) if not _known_word(w, seen)}):
            return Reply(ESCALATE, text, confidence, f"{R_FOREIGN}:{foreign}", sources, raw=raw)
        if bad:
            # Черновик сохраняем, чтобы менеджер видел, что предлагала модель
            return Reply(ESCALATE, text, confidence, f"{R_PRICE_MISMATCH}:{bad}", sources, raw=raw)
        if extra := unsupported_inclusions(text, re.sub(r"Вопрос: [^\n]*", "", known)):
            return Reply(ESCALATE, text, confidence, f"{R_TERMS}:{extra}", sources, raw=raw)
        if hits and (wrong := self.kb.mismatched_amounts(text, hits, question)):
            # «Экран в ДЕПО — 31 500 ₽»: сумма настоящая, но из строки другого ТЦ
            return Reply(ESCALATE, text, confidence, f"{R_VENUE}:{wrong}", sources, raw=raw)
        if hits and (wrong := self.kb.mismatched_formats(text, hits, parse_budget(question.lower()))):
            # «Экран на фуд-корте — 25 000 ₽»: ТЦ тот же, но сумма из строки баннера
            return Reply(ESCALATE, text, confidence, f"{R_FORMAT}:{wrong}", sources, raw=raw)
        if hits and (wrong := self.kb.wrong_tier_amounts(text, hits, question) or self.kb.wrong_day_amounts(text, hits)):
            return Reply(ESCALATE, text, confidence, f"{R_PERIOD}:{wrong}", sources, raw=raw)
        if hits:
            priced_text, wrong = check_price_periods(self.kb, text, hits, question)
            if wrong:
                return Reply(ESCALATE, text, confidence, f"{R_PERIOD}:{wrong}", sources, raw=raw)
        if hits and state and (wrong := budget_issues(self.kb, text, hits, total=state.budget,
                                                    placement=state.placement_budget, preparation=state.preparation_budget)):
            return Reply(ESCALATE, text, confidence, f"{R_TERMS}:{wrong}", sources, raw=raw)
        if needs_manager:
            return Reply(ESCALATE, text, confidence, R_MODEL_UNSURE, sources, raw=raw)
        # «Есть ли звук на экранах?» — «Звук присутствует», хотя в базе про звук ни слова: обещание без опоры
        if missing := self.kb.ungrounded_focus(question, text, known) or self.kb.absent_topic_claims(question, text, known):
            return Reply(ESCALATE, text, confidence, f"{R_UNGROUNDED}:{missing}", sources, raw=raw)
        if hits and (missing := property_issues(self.kb, text, hits, question)):
            return Reply(ESCALATE, text, confidence, f"{R_UNGROUNDED}:{missing}", sources, raw=raw)
        if confidence < self.confidence_threshold:
            return Reply(ESCALATE, text, confidence, R_LOW_CONFIDENCE, sources, raw=raw)
        if hits:
            rows = self.kb.rows_in_answer(text, hits)
            if problem := wrong_conditions(text, rows) or vat_claim_mismatch(text, hits, self.kb.prices):
                # «С учётом сезонной наценки» у машины, «учесть НДС» сверх цены — к менеджеру
                return Reply(ESCALATE, text, confidence, f"{R_TERMS}:{problem}", sources, raw=raw)
            text = add_missing_conditions(drop_foreign_extras(priced_text, rows), rows)
            # Сезонные цены сверяем с текстом до перечня: дописанные строки несут их сами
            text = add_season_prices(complete_partial_list(text, question, hits, self.kb.prices), rows, said=text)
            if unknown := vat_unknown_rows(text, hits, self.kb):
                # Сумма без НДС в источнике — не оферта клиенту: черновик с оговоркой уходит менеджеру
                return Reply(ESCALATE, add_vat_unknown(text, unknown, self.kb.rows_in_answer(text, hits)),
                             confidence, R_VAT_UNKNOWN, sources, raw=raw)
        return Reply(ANSWER, text, confidence, "", sources, raw=raw)


# Менеджер что-то сделает сам — это обещание клиенту. «Свяжитесь с менеджером» — просто вежливость
_MANAGER_PROMISE_RE = re.compile(r"пришл|уточнит\b|свяж[еу]тся|подтверд|расскаж|сообщ|ответит|верн[её]тся"
                                 r"|подготов|рассчита|подбер[её]т", re.IGNORECASE)
# «Подробнее расскажет менеджер», «Если появятся вопросы, менеджер ответит» — вежливость целой фразой, а не обещание
# конкретного действия: ответ полный, хвост убираем. «Подробнее о свободных датах расскажет менеджер», «Стоимость
# печати сообщит менеджер», «Остальное расскажет менеджер» — обещание или неполный ответ: к менеджеру, как раньше
_TAIL_VERB = r"(?:расскаж|сообщ|подскаж|ответ|проконсультир)\w*"
_TAIL_MANAGER = r"(?:(?:наш|ваш)\w*\s+)?менеджер\w*"
_TAIL_FILL = r"(?:\s+(?:вам|с радостью|охотно|обязательно|всё|все))*"
_GENERIC_TAIL_RE = re.compile(
    rf"\W*(?:(?:а\s+)?(?:подробн\w*|детал\w*|дополнительн\w*){_TAIL_FILL}\s+{_TAIL_VERB}{_TAIL_FILL}\s+{_TAIL_MANAGER}"
    rf"|{_TAIL_MANAGER}{_TAIL_FILL}\s+{_TAIL_VERB}{_TAIL_FILL}\s+(?:подробн\w*|детал\w*)"
    rf"|если\s+(?:у вас\s+)?(?:появ|оста|возник)\w*\s+(?:ещ[её]\s+|дополнительн\w*\s+)?вопрос\w*,\s*{_TAIL_MANAGER}"
    rf"{_TAIL_FILL}\s+{_TAIL_VERB}{_TAIL_FILL}"
    rf"|по\s+(?:любым|всем|другим)\s+вопросам{_TAIL_FILL}\s+{_TAIL_VERB}{_TAIL_FILL}\s+{_TAIL_MANAGER})\W*",
    re.IGNORECASE)
_ARITHMETIC_HINT_RE = re.compile(r"[^.!?\n]*(?:умнож|сложите|сложив)[^.!?\n]*[.!?]?\s*", re.IGNORECASE)
_MANAGER_TAIL_MIN = 40  # без хвоста про менеджера должен остаться настоящий ответ, а не одно «Да.»


def drop_offer_question(text: str) -> str:
    """Убрать заключительный вопрос с обещанием («Хотите забронировать даты?», «…или пришлю варианты?»),
    если обещание только в нём и без него остаётся настоящий ответ."""
    sentences = re.findall(r"[^.!?\n]+[.!?]*\s*", text.rstrip())
    last = sentences[-1].strip() if sentences else ""
    # предложение-концовка: вопрос («Хотите забронировать?») или «Если нужно, пришлю требования»
    offer = last.endswith("?") or re.match(r"(?:если|при необходимости|могу|готов[аы]?)\b", last, re.IGNORECASE)
    if len(sentences) < 2 or not offer or not _COMMITMENT_RE.search(last):
        return text
    rest = "".join(sentences[:-1]).strip()
    return rest if len(rest) >= _MANAGER_TAIL_MIN and not _COMMITMENT_RE.search(rest) else text


_SEND_TO_MANAGER_RE = re.compile(r"\b(?:отправ|присл|переда|направ|сда)\w*[^.!?\n]{0,80}?\bменеджер(?=(?:у|ам)\b)",
                                 re.IGNORECASE)
_HIDDEN_MANAGER = "\x00"


def hide_send_to_manager(text: str, context: str) -> str:
    """Спрятать «менеджер» в инструкции клиенту «отправьте … менеджеру», если и в базе так написано
    («прислать список сотрудников — менеджер оформит разрешение»). В фразе с обещанием («отправьте макет
    менеджеру, он уточнит сроки») не прячем: такой ответ по-прежнему уходит менеджеру."""
    if "менеджер" not in context.lower():
        return text
    out = []
    for sentence in re.findall(r"[^.!?\n]+[.!?]*\s*|\n", text):
        if not _MANAGER_PROMISE_RE.search(sentence):
            sentence = _SEND_TO_MANAGER_RE.sub(lambda m: m.group(0)[:-len("менеджер")] + _HIDDEN_MANAGER, sentence)
        out.append(sentence)
    return "".join(out)


def drop_manager_tail(text: str) -> tuple[str, bool]:
    """(текст, нужен ли менеджер). «…Перекрытие — 25 000 ₽. Подробнее расскажет менеджер.» → без
    последней фразы. Если менеджер упомянут не только в конце, ответа без него нет."""
    sentences = re.findall(r"[^.!?\n]+[.!?]*\s*", text)
    while sentences and _MENTIONS_MANAGER_RE.search(sentences[-1]):
        if _MANAGER_PROMISE_RE.search(sentences[-1]) and not _GENERIC_TAIL_RE.fullmatch(sentences[-1]):
            return text, True  # «цифры менеджер пришлёт» — обещание: без менеджера его никто не выполнит
        sentences.pop()
    rest = "".join(sentences).strip()
    if len(rest) < _MANAGER_TAIL_MIN or _MENTIONS_MANAGER_RE.search(rest):
        return text, True
    return rest, False


# Модель обещает сама: перезвонить, уточнить, забронировать, гарантировать, скидку, свободное место
_COMMITMENT_RE = re.compile(r"\b(?:уточню|пришлю|напишу|свяжусь|перезвоню|забронирую|бронирую|гарантирую|подтверждаю"
                            r"|свяжемся|пришл[её]м|вышл\w*|отправим|отправлю|подготовим|подготовлю|закрепим|закреплю"
                            r"|уточним|напишем|перезвоним|забронируем|сообщу|сообщим|предоставим|предоставлю)\b"
                            r"|гарантир\w*|забронир\w*|скидк\w*|скидо\w*|мест\w* свободн\w*|свободн\w* мест\w*"
                            r"|\bсвобод(?:ен|на|но|ны)\b|обязательств\w*|обязу(?:юсь|емся)"
                            # сроки, которых нет в базе: «реклама выйдет уже на следующей неделе»
                            r"|на следующей неделе|\bзавтра\b|\bсегодня\b|уже через|в течение \d+ (?:дн|час|рабоч)"
                            # встречи и звонки бот не назначает: «могу сообщить при личной встрече или по телефону»
                            r"|при (?:личной )?встрече|встретимся|созвон\w*|(?:могу|можем) сообщить"
                            # «для трёх экранов специальных условий нет» — особые условия решает менеджер
                            r"|специальн\w* (?:услови|предложени|цен)\w*|индивидуальн\w* (?:услови|цен)\w*"
                            r"|обсуд\w*[^.!?\n]{0,30}по телефону",  # «экран свободен», но не «свободных витрин»
                            re.IGNORECASE)
# Бот подводит итог или оформляет сделку: это делает только менеджер. «Оформление зоны», «заявку подаёт менеджер»,
# «к оплате отдельно: монтаж…», «если вы выбрали…», «какой экран вы выбрали?» — не оно
_ORDER_RE = re.compile(r"\b(?:итого|всего|сумм\w*|стоимост\w*)\s+к\s+оплате\b"
                       r"|(?<!дополнительно )(?<!отдельно )\bк\s+оплате\b(?![^.!?\n]*\b(?:отдельно|дополнительно)\b)"
                       r"|\bподлежит\s+оплате\b|\bсч[её]т\s+на\s+оплату\b|\bвыстав\w*\s+сч[её]т"
                       r"|(?<!если )(?<!когда )\bвы\s+(?:уже\s+)?(?:выбрали|заказали)\b(?![^.!?\n]*\?)"
                       r"|\bоформ(?:ил\w*|лю|ляю)\b"
                       r"|\bоформ(?:им|ляем)\b(?!\s+(?:\w+\s+)?(?:зон|витрин|стенд|[её]лк|фасад|каток|катк))"
                       r"|\bоформ\w*\s+(?:ваш\w*\s+|эт\w+\s+)?(?:заказ(?:а|у|ом)?\b|заявк|брон|размещени|сделк)"
                       r"|\b(?:заказ|заявк\w*|брон\w*|размещени\w*|сделк\w*)\s+(?:ваш\w*\s+|уже\s+)?"
                       r"(?:принят\w*|принял\w*|оформлен\w*|подтвержд[её]н\w*|создан\w*|зарегистрирован\w*)"
                       r"|\bпринял\w*\s+(?:ваш\w*\s+)?(?:заказ(?:а|у|ом)?\b|заявк)"
                       # живой прогон 31.10: «Для завершения бронирования мне нужно уточнить детали», «Мы зафиксируем место»
                       r"|\bзавершени\w*\s+(?:брон|заказ|оформлени|заявк|сделк)|\bдля\s+брон|\bзафиксиру(?:ем|ю)\b", re.IGNORECASE)
# На «берём» и обычная по виду сумма звучит как итог сделки: «Общая стоимость 42 000 ₽», «Итого 42 000 ₽». На обычный
# вопрос такие итоги разбирает защита цен («итого» из цены и доплаты вырезается, цены остаются)
_READY_TOTAL_RE = re.compile(r"\b(?:итого|всего)\b|\b(?:общ|итогов)\w+\s+(?:сумм|стоимост|цен)\w*"
                             r"|\bсумм\w+\s+(?:заказа|состав\w+|будет)|\bс вас\b", re.IGNORECASE)


def drop_order_talk(text: str, context: str, ready: bool = False) -> tuple[str, bool]:
    """Убрать фразы итога и оформления. (текст, остался ли он без ответа): нет настоящего ответа без них,
    потеряна цена из базы или на заказ не осталось ни одной цены — такой ответ решает менеджер, текст не меняем."""
    kept, dropped = [], []
    for sentence in re.findall(r"[^.!?\n]+[.!?]*\s*", text):
        total = ready and _READY_TOTAL_RE.search(sentence) and extract_amounts(sentence)
        (dropped if total or _ORDER_RE.search(sentence) else kept).append(sentence)
    if not dropped:
        return text, False
    rest = "".join(kept).strip()
    named = set(extract_amounts(rest))
    lost = set(extract_amounts("".join(dropped))) & set(extract_amounts(context)) - named
    statement = "".join(s for s in kept if not s.rstrip().endswith("?")).strip()
    if lost or len(statement) < _MANAGER_TAIL_MIN or (ready and not named):
        return text, True
    return rest, False


_LABEL_RE = re.compile(r"(?:Условия и доплаты|Условия|Подробности|Дополнительная информация|Требования|Включено"
                       r"|В условиях указано|Оплачивается отдельно, разово|Не входит в цену, доплата):\s*(\w)")
FOLLOW_UP_WORDS = 4


REFERS_BACK_WORDS = 15
_REFERS_BACK_RE = re.compile(r"^\s*(?:а|и|ну|ок,?)\s|\b(?:там|к нему|к ней|него|неё|нее|этот|эта|это|этого|второй|"
                             r"первый|третий|тот же|такой же|туда)\b", re.IGNORECASE)


def _known_word(word: str, seen: str) -> bool:
    """Латинское слово есть в базе или у клиента — так или транслитом: «Ozon» при «мы продаём на Озоне»."""
    low, cyr = seen.lower().replace("ё", "е"), translit_word(word)
    return word.lower() in low or cyr[:max(3, len(cyr) - 1)] in low


_CLAIM_RE = re.compile(r"(?:повыс|увелич|рост)\w*[^.!?\n]{0,25}(?:продаж|прибыл|выручк|клиент|узнаваем)|окуп\w*"
                       r"|мощн\w* инструмент|немедленн\w* покупк|гарантированн\w* (?:эффект|результат|рост)"
                       r"|\bв \d+(?:[,.]\d+)? раза?\b", re.IGNORECASE)
_PERCENT_RE = re.compile(r"\d+(?:[,.]\d+)?\s*%")


def drop_claims(text: str, known: str) -> tuple[str, bool]:
    """Убрать фразы с обещанием эффекта и процентами, которых нет в базе. (текст, было ли что убрать)."""
    flat = re.sub(r"\s", "", known.lower())
    kept, dropped = [], False
    for sentence in re.findall(r"[^.!?\n]+[.!?]*\s*", text):
        made_up = _CLAIM_RE.search(sentence) and not _CLAIM_RE.search(known)
        odd_percent = any(re.sub(r"\s", "", p) not in flat for p in _PERCENT_RE.findall(sentence))
        if made_up or odd_percent:
            dropped = True
            continue
        kept.append(sentence)
    return ("".join(kept).strip() if dropped else text), dropped


_RETRY_HINTS = {
    R_PRICE_MISMATCH: "В ответе есть число {detail}, которого нет в строках контекста. Назови цены только так, "
                      "как они записаны в строках «Прайс», без пересчёта, итогов и сумм за несколько месяцев.",
    R_VENUE: "Сумма {detail} взята из строки другого торгового центра. Называй цену из строки того ТЦ, о котором спрашивают.",
    R_PERIOD: "Сумма {detail} связана с другой строкой, периодом или днями размещения (будни, выходные). Сохрани ТЦ, "
              "формат и единицу цены из одной строки. Не выдавай месячную цену за двухнедельную. Если нужного тарифа "
              "нет, назови известную цену с её настоящим периодом или поставь needs_manager = true.",
    R_UNGROUNDED: "Свойства {detail} не подтверждены для этой площадки и формата. Не переноси сведения другого "
                  "экрана и не угадывай. Если без них нельзя ответить на вопрос, поставь needs_manager = true.",
    R_FORMAT: "Сумма {detail} — цена другого формата из контекста. Называй цену из строки того формата, о котором пишешь.",
    R_TERMS: "Неверно об условиях ({detail}): если в строке «с НДС, уже в цене» — НДС не добавляется, если «без НДС» — "
             "не пиши, что он включён; печать, монтаж и производство входят в цену, только если так написано в строке, "
             "иначе назови их отдельно. Для бюджета проверяй общий предел и отдельные пределы размещения/подготовки: "
             "дорогой вариант нельзя называть подходящим. Назови цены по отдельности или передай вопрос, итог не считай.",
    R_FOREIGN: "В ответе есть слова латиницей ({detail}). Пиши только по-русски.",
}


# Модель сама поставила needs_manager, а черновик похож на полный ответ: «…280 000 ₽ в месяц. Свободные даты —
# у менеджера. Уточнить детали?» — одно «второе мнение» вместо передачи менеджеру (у Ultra так было 27 из 34 передач)
_UNSURE_HINT = ("Ты отметил needs_manager = true. Если ответ на вопрос клиента целиком есть в контексте, верни его "
                "с needs_manager = false — без предложений уточнить, забронировать или прислать и без упоминания "
                "менеджера. Если вопрос про занятость мест, скидки, договор, оплату или того, о чём спрашивают, "
                "в контексте нет — оставь needs_manager = true.")
_UNSURE_MIN_DRAFT = 60


# «Можно забронировать место на ноябрь?» — бронь, свободные места и даты решает менеджер: второго мнения нет
_BOOKING_RE = re.compile(r"брон|свободн|занят|на какие даты|\bдаты\b|успеем|когда можно"
                         r"|фестивал|мероприят|праздник|концерт|аренд\w* атриум", re.IGNORECASE)
# Во втором ответе модель сама признаёт, что данных нет («тираж не указан») — значит, нужен менеджер
_NO_DATA_RE = re.compile(r"не указан\w*|нет (?:данных|информации|сведений)|в (?:доступных|имеющихся) данных|неизвестн",
                         re.IGNORECASE)


def retry_hint(reason: str, draft: str = "", holding: str = "", question: str = "") -> str:
    """Подсказка для повторной попытки по причине отказа проверки, или пусто, если повтор не поможет."""
    kind, _, detail = reason.partition(":")
    if kind == R_MODEL_UNSURE:
        substantial = len(draft) >= _UNSURE_MIN_DRAFT and draft != holding and "Уточню детали" not in draft \
            and not _BOOKING_RE.search(question)
        return f"{_UNSURE_HINT} Верни ответ в том же JSON." if substantial else ""
    hint = _RETRY_HINTS.get(kind)
    return f"{hint.format(detail=detail.strip('[]'))} Исправь ответ и верни его в том же JSON." if hint else ""


def search_query(question: str, previous: str) -> str:
    """Продолжение («а печать?», «в депо», «а печать и монтаж к нему сколько?») само по себе находит
    не то: ищем его вместе с прошлым сообщением клиента."""
    words = len(re.findall(r"\w+", question))
    # «а второй вариант где именно висит и сколько раз в день показывают ролик?» — 13 слов, но это продолжение
    if previous and (words <= FOLLOW_UP_WORDS or (words <= REFERS_BACK_WORDS and _REFERS_BACK_RE.search(question))):
        return f"{previous} {question}"
    return question


def wrong_conditions(text: str, rows: list) -> str:
    """Условие, которого у названной строки нет: сезонная наценка там, где её нет; НДС как доплата."""
    if not rows:
        return ""
    low = text.lower()
    seasonal_rows = any(r.parts()["season"] for r in rows)
    if re.search(r"наценк|сезонн|в (?:ноябре|декабре|январе)[^.!?\n]*(?:коэффициент|дороже|выше)", low) and not seasonal_rows:
        return "сезон"
    if re.search(r"учесть ндс|плюс ндс|\+\s*ндс|ндс(?:\s*\d+\s*%)?\s+(?:оплачива|не включ|отдельно|сверху|добавля|начисля)|без учета ндс"
                 r"|(?:добав|начисл)\w*\s+(?:\w+\s+)?ндс|не включа\w*\s+(?:\w+\s+)?ндс", low.replace("ё", "е")) \
            and any(r.parts()["vat"] for r in rows):
        return "ндс"
    # Обратное: «все суммы с НДС» про пакет, у которого «без НДС, НДС 5% начисляется сверху» или НДС не указан
    if re.search(r"с (?:учетом )?ндс|ндс (?:уже )?(?:включ|в цен)|включ\w* ндс", low.replace("ё", "е")) \
            and any("без ндс" in r.notes.lower() or vat_unknown(r) for r in rows):
        return "ндс"
    return ""


_VAT_UNKNOWN_RE = re.compile(r"ндс[^;.]*не указан", re.IGNORECASE)


def vat_unknown(row) -> bool:
    """В примечании строки: «НДС в прайсе не указан»."""
    return bool(_VAT_UNKNOWN_RE.search(row.notes))


def vat_unknown_rows(text: str, hits: list[SearchHit], kb: KnowledgeBase) -> list:
    """Строки без указанного НДС, чью цену называет ответ. Если формат не стоит рядом с суммой
    («2 выхода — 25 200 ₽»), строку узнаём по самой сумме, кроме сумм, уже объяснённых другими строками."""
    named = kb.rows_in_answer(text, hits)
    if found := [r for r in named if vat_unknown(r)]:
        return found
    explained = {a for r in named for a in (r.price_rub, *extract_amounts(r.notes))}
    amounts = set(extract_amounts(text)) - explained
    found = []
    for row in (kb.prices[int(h.chunk.id.split("-")[1])] for h in hits if h.chunk.kind == "price"):
        if vat_unknown(row) and row.price_rub in amounts and row not in found:
            found.append(row)
    return found


def add_vat_unknown(text: str, unknown: list, named: list) -> str:
    """Оговорка про НДС, которую модель или обрезка хвоста про менеджера потеряли."""
    if _VAT_UNKNOWN_RE.search(text):
        return text
    if all(r in unknown for r in named):
        return _before_question(text, "НДС в прайсе не указан.")
    venues = dict.fromkeys(r.placement.split(" — ")[0].split(",")[0].strip() for r in unknown)
    return _before_question(text, f"НДС в прайсе не указан для позиций: {', '.join(venues)}.")


_EXTRAS_CLAIM_RE = re.compile(r"[^.!?\n]*(?:печат|монтаж)[^.!?\n]*(?:оплачива|отдельно|дополнительн|доплат)[^.!?\n]*[.!?]?\s*",
                              re.IGNORECASE)


def drop_foreign_extras(text: str, rows: list) -> str:
    """«Печать и монтаж оплачиваются отдельно» про экран или про талоны, где производство включено —
    этой фразы у строки нет, убираем её."""
    if rows and not any(r.parts()["extra"] for r in rows):
        trimmed = _EXTRAS_CLAIM_RE.sub("", text).strip()
        if extract_amounts(trimmed):
            return trimmed
    # «Монтаж 14 000 ₽ за 1 месяц» — монтаж разовый
    return re.sub(r"((?:монтаж|печат)\w*[^.!?\n,;]{0,40}?\d[\d\s]*(?:₽|руб\w*))\s*(?:за|в) (?:1 |один )?месяц", r"\1", text,
                  flags=re.IGNORECASE)


def vat_claim_mismatch(text: str, hits: list[SearchHit], prices: list) -> str:
    """«…224 400 ₽ … Все суммы указаны с НДС 5%» про пакет «без НДС». Формат может стоять в другой фразе,
    поэтому строки ищем по суммам: все строки контекста с названными суммами — «без НДС»."""
    if not re.search(r"с (?:учетом )?ндс|ндс (?:уже )?(?:включ|в цен)|включ\w* ндс", text.lower().replace("ё", "е")):
        return ""
    named = set(extract_amounts(text))
    rows = [prices[int(h.chunk.id.split("-")[1])] for h in hits if h.chunk.kind == "price" and h.chunk.price in named]
    return "ндс" if rows and all("без ндс" in r.notes.lower() for r in rows) else ""


def add_missing_conditions(text: str, rows: list) -> str:
    """Ответ назвал цену, но потерял условие, от которого она зависит: минимальный срок или доплаты."""
    if not 1 <= len(rows) <= 2:
        return text
    low, extra = text.lower(), []
    for row in rows:
        notes = row.parts()
        if notes["term"] and not re.search(r"минимальн|при размещении|от \d+ (?:месяц|дн)|не менее|минимум", low):
            term = notes["term"][0]
            extra.append(f"Цена действует {term}." if term.startswith("при размещении")
                         else f"Минимальный срок — {term.replace('минимальный срок ', '')}.")
        priced = [p for p in notes["extra"] if extract_amounts(p)]
        if priced and not re.search(r"печат|монтаж|изготовлен|производств", low):
            extra.append(f"Отдельно оплачивается: {'; '.join(priced)}.")
    if not extra:
        return text
    # «…Какая форма рекламы вам интересна?» — доплату ставим перед заключительным вопросом
    return _before_question(text, " ".join(dict.fromkeys(extra)))


def add_season_prices(text: str, rows: list, said: str | None = None) -> str:
    """Цена, названная в чате, может быть признана офертой, поэтому сезонную цену строки дописываем кодом,
    если ответ её не назвал. said — текст, в котором ищем сезонную цену (по умолчанию весь ответ)."""
    # Обычная цена другой строки — не сезонная этой: 31 500 ₽ у экрана ВЕЕР и в ноябре у экрана ДЕПО
    named = set(extract_numbers(text if said is None else said)) - {row.price_rub for row in rows}
    missing = {id(row): [p for p in _season_prices(row) if not set(extract_amounts(p)) & named] for row in rows}
    rows = [row for row in rows if missing[id(row)]]
    if not rows:
        return text
    own = {rows[0].price_rub, *extract_amounts(rows[0].notes)}
    if len(rows) == 1 and all(n in own or n < 1000 for n in extract_numbers(text)):
        part = "; ".join(missing[id(rows[0])])
        return _before_question(text, f"{part[0].upper()}{part[1:]}.")
    # В ответе есть другие цены: без названия позиции клиент отнесёт сезонную к последней из них
    with_venue = len({_venue_head(row.placement) for row in rows}) > 1
    named_rows: dict[str, list[str]] = {}
    for row in rows:
        named_rows.setdefault(_row_title(row, with_venue), []).extend(missing[id(row)])
    return _before_question(text, " ".join(f"{title}: {'; '.join(parts)}." for title, parts in named_rows.items()))


def _season_prices(row: PriceRow) -> list[str]:
    """Сезонные цены строки: только с суммой, «в декабре коэффициент 2» без неё клиенту ничего не называет."""
    return [part if billing_periods(part) else part.rstrip(".") + price_term_suffix(row.period)
            for part in row.parts()["season"] if extract_amounts(part)]


def _season_suffix(row: PriceRow) -> str:
    """Сезонная цена рядом с обычной, когда строку пишет сам код: « (в ноябре–декабре 37 800 ₽ в месяц)»."""
    return f" ({'; '.join(parts)})" if (parts := _season_prices(row)) else ""


_HEDGE_RE =re.compile(r"например|в том числе|среди них|и другие|есть и друг|не все|часть", re.IGNORECASE)
LIST_APPEND_LIMIT = 8  # больше строк кодом не дописываем: простыня в чате не читается, остальное — по вопросу
PARTIAL_LIST_NOTE = "Это часть вариантов — могу рассказать и про остальные."


def complete_partial_list(text: str, question: str, hits: list[SearchHit], prices: list[PriceRow]) -> str:
    """«Какие баннеры есть?» — модель перечисляет 5 из 13 под «доступны следующие». Если клиент просил
    перечень, а названа меньшая часть найденных цен, недостающие строки спрошенного формата дописываем кодом
    прямо из прайса (модель их не пересказывает, поэтому цены и сроки точные). Не больше LIST_APPEND_LIMIT,
    остальное — «это часть вариантов»; без названного формата не знаем, что дописывать, — только эта фраза."""
    rows = [h for h in hits if h.chunk.kind == "price"]
    # Считаем только строки того формата, о котором спросили: у «Радуги» два экрана — оба названы,
    # а промо и радио того же ТЦ в контексте не повод писать «это часть вариантов»
    asked = set(tokenize(question))
    of_format = [h for h in rows if _format_words(h.chunk.fmt) & asked]
    # «Какие экраны на фуд-корте?» — экраны атриума дописывать не нужно
    of_format = [h for h in of_format if h.chunk.place & asked] or of_format
    rows = of_format or rows
    amounts = {h.chunk.price for h in rows}
    named = set(extract_amounts(text)) & amounts
    if not _LIST_QUESTION_RE.search(question) or len(amounts) < 5 or len(named) >= len(amounts) or _HEDGE_RE.search(text):
        return text
    if len(named) < 2:
        return text
    missing = list({h.chunk.id: h for h in rows if h.chunk.price not in named}.values())
    if not of_format:
        return _before_question(text, PARTIAL_LIST_NOTE)
    listed = [prices[int(h.chunk.id.split("-")[1])] for h in missing[:LIST_APPEND_LIMIT]]
    # Одна площадка у всех строк — называем её один раз: «Ещё варианты в ДЕПО, Нижний Тагил: …»
    venues = {_venue_head(r.placement) for r in listed}
    where = f" в {venues.pop()}" if len(venues) == 1 else ""
    added = f"Ещё варианты{where}: " + "; ".join(list_item(r, with_venue=not where) for r in listed) + "."
    if len(missing) > LIST_APPEND_LIMIT:
        added += " " + PARTIAL_LIST_NOTE
    return _before_question(text, added)


def list_item(row: PriceRow, with_venue: bool = True) -> str:
    """Строка прайса для перечня клиенту: «Баннер № 2, ВЕЕР Молл, Екатеринбург — атриум: 25 000 ₽ за 1 месяц»,
    без площадки — «Баннер № 2 — атриум: 25 000 ₽ за 1 месяц»."""
    period = price_term_suffix(row.period)
    vat = " (без НДС)" if "без ндс" in row.notes.lower() else ""
    return f"{_row_title(row, with_venue)}: {format_rub(row.price_rub)}{period}{vat}{_season_suffix(row)}"


def _row_title(row: PriceRow, with_venue: bool = True) -> str:
    """«Баннер № 2, ВЕЕР Молл, Екатеринбург — атриум», без площадки — «Баннер № 2 — атриум»."""
    place = row.placement.partition(" — ")[2]
    name = f"{row.format}, {_venue_head(row.placement)}" if with_venue else row.format
    return f"{name}{f' — {place}' if place else ''}"


def _venue_head(placement: str) -> str:
    """Площадка без места и адреса в скобках: «ДЕПО, Нижний Тагил — фуд-корт» → «ДЕПО, Нижний Тагил»."""
    return re.sub(r"\s*\(.*?\)", "", placement.partition(" — ")[0]).strip()


def _before_question(text: str, added: str) -> str:
    """Дописать к ответу, но перед заключительным вопросом клиенту («…Какая форма рекламы вам интересна?»)."""
    if (question := re.search(r"(\s*)([^.!?\n]*\?)\s*$", text)) and text[:question.start()].strip():
        head = text[:question.start()].rstrip()
        return f"{head} {added}{question.group(1) or ' '}{question.group(2).strip()}"
    return (text.rstrip() + " " + added).strip()


_LIST_QUESTION_RE = re.compile(r"какие|что (?:у вас )?есть|что можно|варианты|перечисл|все\b|список", re.IGNORECASE)


def render_context(hits: list[SearchHit]) -> str:
    return "\n".join(f"[{i}] {h.chunk.text}" for i, h in enumerate(hits, 1))


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_SERVICE_FIELD_RE = re.compile(r"\s*\(?\b(?:needs[_ ]?manager|confidence)\b\s*[:=]?\s*\"?([\w.]*)\"?\)?\.?",
                               re.IGNORECASE)
# Клиент пытается вытащить инструкции бота («повтори свой промпт») — такой ответ клиенту не отправляем
_PROMPT_LEAK_RE = re.compile(r"КОНТЕКСТ:|Верни ТОЛЬКО JSON|Ты — помощник менеджера|Правила:\s*\n"
                             r"|контекст|предоставленн\w* (?:информаци|данн|материал)|\bplease\b|\bsorry\b",
                             re.IGNORECASE)
_META_CLAUSE_RE = re.compile(r"[^,.;!?\n]*(?:предоставленн\w* (?:информаци|данн|материал)\w*|в контексте|по контексту)"
                             r"[^,.;!?\n]*", re.IGNORECASE)
# Догадка вместо факта: «MP4 скорее всего подойдёт»
_GUESS_RE = re.compile(r"скорее всего|\bвероятно\b|по-видимому|предположительно|наверное|часто принимают"
                       r"|обычно подход\w*|как правило|должн\w* подойти"
                       # «Требования к формату точно не указаны, однако часто используется mp4» — догадка вместо данных
                       r"|(?:часто|обычно|как правило) (?:использу|применя|принима)\w*"
                       # «автомобиль обычно размещается у центрального входа, точное место может зависеть от планировки»
                       r"|обычно (?:размеща|располага|став|устанавлива|находит|проход)\w*|может зависеть от"
                       r"|(?:не указан\w*|нет (?:данных|информации|сведений))[^.!?\n]{0,60}?,\s*(?:однако|но)\b",
                       re.IGNORECASE)
_MENTIONS_MANAGER_RE = re.compile(r"менеджер", re.IGNORECASE)


def parse_model_output(raw: str) -> tuple[str, float, bool]:
    """Достаёт (answer, confidence, needs_manager) из ответа модели.

    Модели иногда оборачивают JSON в текст или ```json```, поэтому ищем первый
    объект. Если JSON не найден, считаем весь текст ответом с низкой уверенностью.
    Если служебное поле попало в сам текст ответа (GigaChat пишет
    {"answer": "needs_manager = true"}), ответа нет — передаём менеджеру.
    """
    answer, confidence, needs_manager = (raw or "").strip(), 0.5, False
    match = _JSON_RE.search(raw or "")
    if not match and '"answer"' in (raw or ""):
        # JSON оборвался без закрывающей скобки — тоже разбираем по полям, а не отдаём клиенту как текст
        answer, confidence, needs_manager = _loose_fields(raw, answer)
    if match:
        try:
            data = json.loads(match.group(0))
            value = data.get("answer", "")
            answer = value.strip() if isinstance(value, str) else ""  # null или словарь — клиенту не отдаём
            try:
                # Поле пропущено — модель не сомневается (GigaChat иногда его не пишет); решают needs_manager и проверки
                confidence = max(0.0, min(1.0, float(data.get("confidence", 1.0))))
            except (TypeError, ValueError):
                confidence = 0.5
            needs = data.get("needs_manager", False)
            needs_manager = needs if isinstance(needs, bool) else str(needs).strip().lower() in ("true", "1", "yes", "да")
        except json.JSONDecodeError:
            # GigaChat иногда ломает собственный JSON: «(с НДС).\\", "confidence": 1» — достаём поля по одному
            answer, confidence, needs_manager = _loose_fields(match.group(0), answer)
    # Служебное поле попало в текст: «needs_manager = true» — модель зовёт менеджера,
    # «Needs Manager: false.» в конце ответа — просто вырезаем
    for field_match in _SERVICE_FIELD_RE.finditer(answer):
        value = field_match.group(1).strip(".").lower()
        if value not in ("false", "0") and not value.replace(".", "").isdigit():
            needs_manager = True
    answer = _tidy(_SERVICE_FIELD_RE.sub("", answer))
    if _META_CLAUSE_RE.search(answer):
        trimmed = re.sub(r"\s+([,.;!?])", r"\1", _META_CLAUSE_RE.sub("", answer)).strip(" ,;")
        trimmed = re.sub(r",\s*\.", ".", trimmed)
        if len(trimmed) >= 40:
            answer = trimmed[:1].upper() + trimmed[1:]
    if _GUESS_RE.search(answer):
        needs_manager = True
    if _PROMPT_LEAK_RE.search(answer) or answer.startswith("{") or '"answer"' in answer:
        answer, needs_manager = "", True  # утечка инструкций или JSON внутри JSON
    if not answer:
        needs_manager = True
    return answer, confidence, needs_manager


_LOOSE_ANSWER_RE = re.compile(r'"answer"\s*:\s*"(.*?)\\?["\']\s*(?:,\s*\\?"[\w ]*\\?"\s*:|\})', re.DOTALL)
_LOOSE_CONFIDENCE_RE = re.compile(r'"confidence"\s*:\s*"?([0-9.]+)')
_LOOSE_NEEDS_RE = re.compile(r'"needs_manager"\s*:\s*"?(true|false)', re.IGNORECASE)


# Хвост, где GigaChat потерял имена полей: «…размерам.: false}», «…ткань. ": false}»
_LOOSE_TAIL_RE = re.compile(r'(?:\\?["\']?\s*[,:]\s*(?:\\?"[\w ]*\\?"\s*:\s*)?"?(?:true|false|[\d.]+)"?\s*)+\}?\s*$',
                            re.IGNORECASE)


def _loose_fields(blob: str, fallback: str) -> tuple[str, float, bool]:
    found = _LOOSE_ANSWER_RE.search(blob)
    if not found:
        start = re.search(r'"answer"\s*:\s*"', blob)
        # Хвост без имён полей разбираем, только если JSON закрыт: «…места № 2, 3» — обрыв, а не поле
        tail = _LOOSE_TAIL_RE.search(blob, start.end()) if start and blob.rstrip().endswith("}") else None
        if not tail:
            # Не разобрать: сырой JSON клиенту не показываем — пусть решает менеджер
            return ("", 0.5, True) if start else (fallback, 0.5, False)
        # Во хвосте «true» — не знаем, чьё это поле, поэтому осторожно: к менеджеру
        return (blob[start.end():tail.start()].replace('\\"', '"').strip(), 1.0,
                "true" in tail.group(0).lower())
    answer = found.group(1).replace('\\"', '"').replace("\\n", "\n").strip()
    conf = _LOOSE_CONFIDENCE_RE.search(blob)
    needs = _LOOSE_NEEDS_RE.search(blob)
    try:
        confidence = max(0.0, min(1.0, float(conf.group(1)))) if conf else 1.0  # как и в целом JSON
    except ValueError:
        confidence = 0.5
    return answer, confidence, bool(needs and needs.group(1).lower() == "true")


def _tidy(text: str) -> str:
    """Непарная кавычка от сломанного JSON: «размер по запросу).". Хотите…»."""
    if text.count('"') % 2:
        text = re.sub(r'"(?=[.,!?\s]|$)', "", text, count=1)
        text = re.sub(r"(?<!\.)\.\.(?!\.)", ".", text)  # «).». » → «).» — но многоточие не трогаем
    # Хвосты сломанного JSON: «(с НДС).).).», «7 000 ₽., уточните», «37 800 ₽)..»
    text = re.sub(r"(?:\)\.){2,}", ").", text)
    text = re.sub(r"\.,", ",", text)
    text = re.sub(r"(?<!\.)\.\.(?!\.)", ".", text)
    return text.strip()


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
