"""Границы справочных ответов на сообщения действующих клиентов.

Отдельный слой поверх ядра: текущее размещение, письма и согласования ведёт
менеджер. Его включают и приложение, и стенд оценки.
"""
from __future__ import annotations

import re
from dataclasses import replace

from .assistant import Assistant, ESCALATE, SKIP, Reply
from .knowledge import KnowledgeBase, SearchHit, parse_budget
from .conversation import Conversation

OPERATIONS = "business_operation"
_THANKS = re.compile(
    r"^(?:[А-ЯЁ][а-яё]+[,! ]+)?(?:спасибо|благодарю|принято)"
    r"(?:[,! ]+(?:спасибо|благодарю|принято|[А-ЯЁ][а-яё]+))*[.! 🙏👍❤️]*$"
)
_OPERATIONS = re.compile(
    r"\b(?:поставь|поставьте|запустите|запусти|продлеваем|по денежке|порешать|дошло письмо|"
    r"получили письмо|договор согласован|все согласовано|всё согласовано|выставьте сч[её]т|"
    r"добавить в сч[её]т|добавьте в сч[её]т|отправил[аи]? (?:все\s+)?(?:ролик|материал|письмо)\w*|"
    r"в почту отправил\w*|файлы в полном разрешении|лайтбоксы свободные)\b|"
    r"https?://|\b(?:эти|оба|баннеры|места|флаги|лайтбоксы)\b.{0,55}\bсвободн|"
    r"\b(?:стоимость готовы обсуждать|вместо денег|бартера|бартером)\b|"
    r"\b(?:сократ\w*|сниз\w*|пересчита\w*)\b.{0,60}\b(?:стоимост\w*|цен\w*|час\w*)|"
    r"\b(?:напечатать|печатать|изготовить|производить)\b.{0,25}\b(?:сами|самостоятельно)\b|"
    r"\b(?:сами|самостоятельно)\b.{0,25}\b(?:напечатать|печатать|изготовить)\b|"
    r"\bконструкци\w*\b.{0,35}\bвилку\b.{0,25}\bвходить\b|"
    r"\b(?:стать|выступить|быть)\s+партн[её]р\w*\b",
    re.I,
)
_REFUSAL = re.compile(
    r"\b(?:не могу (?:предоставить|проверить|подтвердить|ответить)|не располагаю|"
    r"ваш запрос принят|приступим к обработке|чтобы я мог проверить)\b", re.I,
)
_CAMPAIGN_ACTION = re.compile(
    r"\b(?:не\s+)?(?:меняйте|менять|поменяйте|замените|заменить|оставьте|оставить|отмените|отменить)\b"
    r"[^.!?]{0,65}\b(?:ролик\w*|заявк\w*|размещени\w*|кампани\w*|реклам\w*)\b|"
    r"\bстар\w* ролик\w*[^.!?]{0,35}(?:продолжал|продолжит|остав|идти)|"
    r"\bролик\w*[^.!?]{0,90}\b(?:замени\w*|заменить|поменя\w*|не\s+меня\w*)\b|"
    r"\bничего\s+не\s+(?:меняйте|поменяете|менять)\b", re.I,
)
_ACTION_CONFIRMED = re.compile(
    r"\b(?:оставим|оставили|оставляем)\b[^.!?]{0,40}без изменений|"
    r"\b(?:заменили|отменили|запустили|забронировали|изменили)\b|"
    r"\bничего\s+не\s+(?:меняем|поменяем|изменим)\b|"
    r"\b(?:организуем|выполним)\s+(?:замен\w*|запуск\w*)\b", re.I,
)
_PRORATE_REQUEST = re.compile(r"\b(?:раздели\w*|умнож\w*|пропорциональн\w*)\b", re.I)
_PRORATE_CLAIM = re.compile(r"(?:чуть|немного)\s+(?:меньше|менее|дешевле)|"
                            r"(?:возьм[её]м|потребуется|заплатите)[^.!?]{0,25}(?:меньше|менее)", re.I)


# Контакты: клиент не может связаться по выданному номеру или просит ник — это к менеджеру.
# «Место для машины номер 1» под шаблон не попадает: рядом нет слов про связь и недоступность.
_CONTACT_TROUBLE = re.compile(
    r"\b(?:номер|телефон)\w*\b[^.!?]{0,60}\bне\s*(?:доступен|доступна|отвечает|работает|дозвониться|"
    r"могу\s+(?:дозвониться|найти|связаться)|получается\s+(?:дозвониться|связаться|найти))\b|"
    r"\b(?:по\s+)?номеру\s+телефона\b[^.!?]{0,60}\bне\s+(?:могу|получается|доступен|отвечает)\b|"
    r"\bне\s+(?:могу|получается)\s+(?:дозвониться|связаться|найти)\b[^.!?]{0,60}\b(?:номер|телефон)\w*|"
    r"\bсообщите\s+(?:его\s+|е[её]\s+)?ник\b|"
    r"\bкак\b[^.!?]{0,40}\bещ[её]\b[^.!?]{0,40}\bсвязаться\b",
    re.I,
)
# Жалоба на показы: «наш ролик вообще не крутили», «экран не работает» — разбирает менеджер.
_COMPLAINT = re.compile(
    r"\b(?:ролик|реклам\w*|баннер|экран|размещени\w*)\b[^.!?]{0,90}\bне\s+"
    r"(?:крутил\w*|показыва\w*|выходил\w*|запустил\w*|разместил\w*|работа\w*|был\w*)\b",
    re.I,
)
# Обещание скидки: «за полцены», «в два раза дешевле». Обычное «дешевле» — не скидка.
_DISCOUNT_PROMISE = re.compile(
    r"\bполцен\w*|\bполовин\w*\s+(?:цен|стоимост)\w*|"
    r"\bв\s+два\s+раза\s+дешевле\b|\bдешевле\s+в\s+два\s+раза\b|\bвдвое\s+дешевле\b|"
    r"\bдешевле\s+на\s+\d+\s*%",
    re.I,
)
# Просьба показать визуал: «покажите, как выглядит экран». «Покажите цены» — обычный вопрос.
_VISUAL_REQUEST = re.compile(
    r"\b(?:покаж\w*|глянуть|посмотреть|увидеть|продемонстрир\w*)\b[^.!?]{0,60}\b"
    r"(?:как\s+выглядит|фото|фотк|картинк|изображени|визуал)\w*|"
    r"\bкак\s+выглядит\b",
    re.I,
)
# Чужие рекламодатели и соседство: «кто ещё крутится на экранах», «назовите бренды».
_FOREIGN_BRANDS = re.compile(
    r"\bкто\b[^.!?]{0,60}\b(?:крутится|размещается|рекламируется|арендует|снимает)\b|"
    r"\b(?:назовите|перечислите|список|какие)\b[^.!?]{0,30}\bбренд\w*|"
    r"\bчужи\w*\s+бренд\w*|\bсоседств\w*",
    re.I,
)
# Риск и ответственность за имущество: царапины, повреждения, ущерб, страховка.
_RISK = re.compile(
    r"\b(?:поцарап\w*|царапин\w*|повред\w*|испорт\w*|ущерб\w*|вмят\w*|разбил\w*|"
    r"угон\w*|украл\w*|украдут|пропаж\w*|пропад\w*|страхов\w*)\b",
    re.I,
)
# Участие в мероприятиях и розыгрышах: расписание и условия участия решает менеджер.
# «Какие мероприятия будут в декабре?» без участия — обычный справочный вопрос.
_EVENT_PARTICIPATION = re.compile(
    r"\b(?:по)?участв\w*\b[^.!?]{0,50}\b(?:розыгрыш|мероприят|фестивал|праздник|концерт|ярмарк)\w*|"
    r"\b(?:розыгрыш|мероприят|фестивал|праздник|концерт|ярмарк)\w*\b[^.!?]{0,50}\b(?:по)?участв\w*",
    re.I,
)
# Расписание на конкретные даты: «будут ли мероприятия с 14 ноября по 20 декабря» — даты в календаре
# могут меняться, подтверждает менеджер. «Какие мероприятия будут в декабре?» — обычный справочный вопрос.
_EVENT_SCHEDULE = re.compile(
    r"\bмероприят\w*\b[^.!?]{0,70}\bс\s+\d{1,2}\s+[а-яё]+\s+по\s+\d{1,2}\s+[а-яё]+|"
    r"\bс\s+\d{1,2}\s+[а-яё]+\s+по\s+\d{1,2}\s+[а-яё]+\b[^.!?]{0,70}\bмероприят\w*|"
    r"\bмероприят\w*\b[^.!?]{0,70}\b\d{1,2}\s+[а-яё]+\s*[—–-]\s*\d{1,2}\s+[а-яё]+",
    re.I,
)


def named_thanks(text: str) -> bool:
    return bool(_THANKS.fullmatch(text.strip()))


def business_operation(text: str) -> bool:
    return bool(_OPERATIONS.search(text) or _CAMPAIGN_ACTION.search(text)
                or re.search(r"возьм[её]те[^.!?]{0,20}(?:меньше|дешевле)|"
                             r"подтверди\w*[^.!?]{0,20}(?:цену|сумму)", text, re.I))


def manager_decision(text: str) -> str:
    """Причина передачи менеджеру для сообщений, где ответ бота опасен, или пустая строка.

    Проверки точечные: контакты, жалобы, обещания скидок, просьбы показать визуал, чужие бренды,
    риск повреждения имущества и участие в мероприятиях. Обычные вопросы о цене, меню и размещении
    под них не попадают.
    """
    if _CONTACT_TROUBLE.search(text):
        return "client_contact"
    if _COMPLAINT.search(text):
        return "client_complaint"
    if _DISCOUNT_PROMISE.search(text):
        return "discount_promise"
    if _VISUAL_REQUEST.search(text):
        return "visual_request"
    if _FOREIGN_BRANDS.search(text):
        return "foreign_brands"
    if _RISK.search(text):
        return "risk_or_liability"
    if _EVENT_PARTICIPATION.search(text):
        return "event_participation"
    if _EVENT_SCHEDULE.search(text):
        return "event_schedule"
    return ""


_RADIO_CHANGE = re.compile(
    r"\b(?:заменили|поменяли|замените|поменяйте)\b[^.!?;\n]{0,45}\b(?:радио|аудио)ролик\b|"
    r"\b(?:радио|аудио)ролик\b[^.!?;\n]{0,45}\b(?:заменили|поменяли|замените|поменяйте)\b|"
    r"\bна радио (?:уже )?(?:заменили|поменяли) (?:наш )?ролик\b", re.I,
)
_SHORT_CUBE_SPEC = re.compile(
    r"(?:можно (?:ли )?)?(?:тт|технические требования) (?:на|к|для) (?:медиа)?куб[ау]? (?:в )?веер"
)
_CUBE_WORD = re.compile(r"\b(?:медиа)?куб(?:[аеу]|ом)?\b")
_SPEC_WORD = re.compile(r"\b(?:тт|технич\w*|требован\w*|параметр\w*|размер\w*|разрешени\w*|макет\w*)\b")
_VEER_CUBE_TITLE = "какие требования к ролику для медиакуба веер"


def radio_change_request(text: str) -> bool:
    return bool(_RADIO_CHANGE.search(text))


def _spec_title(text: str) -> str:
    title = " ".join(text.lower().strip(" .?!;").split()).replace("медиа-куб", "медиакуб")
    return re.sub(r"\bвеер(?:[аеу]|ом)?(?: молл(?:[аеу]|ом)?)?\b", "веер", title)


def mixed_request_hint(kb: KnowledgeBase, text: str) -> str:
    """Две самостоятельные фразы и однозначный технический FAQ с явной площадкой."""
    parts = [p.strip() for p in re.findall(r"[^.!?;\n]+[.!?;]*", text) if p.strip()]
    if len(parts) != 2:
        return ""
    question, request = parts
    if business_operation(question) or radio_change_request(question):
        question, request = request, question
    key = _spec_title(question)
    if kb.matched_stop_topic(question) or not (business_operation(request) or radio_change_request(request)):
        return ""
    if _SHORT_CUBE_SPEC.fullmatch(key):
        # «Куб» бывает и напольным. При конкурирующих техтребованиях выбирает менеджер.
        matches = [c for c in kb.faq if (not c.venue or "веер" in c.venue)
                   and _CUBE_WORD.search(_spec_title(c.title)) and _SPEC_WORD.search(c.title.lower())]
        key = _VEER_CUBE_TITLE
    elif key.startswith("какие требования к ролику "):
        matches = [c for c in kb.faq if _spec_title(c.title) == key]
    else:
        return ""
    if len(matches) != 1 or _spec_title(matches[0].title) != key:
        return ""
    source = matches[0]
    if not source.venue or not source.venue.issubset(kb.normalize(question)):
        return ""
    answer = source.text.partition("\nОтвет: ")[2].strip()
    if not answer or _ACTION_CONFIRMED.search(answer) or business_operation(answer) or radio_change_request(answer):
        return ""
    return f"\n\nМенеджеру: «{request}»\nСправка из базы («{source.title}»):\n{answer}"


class BusinessAssistant(Assistant):
    check_requested_facts = True

    async def answer(self, question: str, history: list[tuple[str, str]] | None = None,
                     allow_llm: bool = True, last_question: str = "",
                     conversation: Conversation | None = None) -> Reply:
        if named_thanks(question):
            return Reply(SKIP, "", 1.0, "small_talk")
        if business_operation(question):
            if conversation is not None:
                conversation.observe(self.kb, question)
                conversation.finish(ESCALATE, OPERATIONS)
            return Reply(ESCALATE, self.holding_message, 0.0, OPERATIONS)
        if reason := manager_decision(question):
            if conversation is not None:
                conversation.observe(self.kb, question)
                conversation.finish(ESCALATE, reason)
            return Reply(ESCALATE, self.holding_message, 0.0, reason)
        if _PRORATE_REQUEST.search(question):
            return Reply(ESCALATE, self.holding_message, 0.0, "period_calculation")
        reply = await super().answer(question, history, allow_llm, last_question, conversation)
        if reply.is_answer:
            prices = [SearchHit(c, 0.0) for c in self.kb.chunks if c.kind == "price"]
            # Бюджет клиента («в Бутово до 40 тысяч» → «за 40 000 ₽ доступны…») — его слова, а не цена чужого ТЦ
            if set(self.kb.mismatched_amounts(reply.text, prices, question)) - {parse_budget(question.lower())}:
                reply = replace(reply, decision=ESCALATE, text=self.holding_message, reason="venue_mismatch")
        if reply.is_answer and _REFUSAL.search(reply.text):
            reply = replace(reply, decision=ESCALATE, text=self.holding_message, reason="model_unsure")
        if reply.is_answer and (_ACTION_CONFIRMED.search(reply.text) or _PRORATE_CLAIM.search(reply.text)):
            reply = replace(reply, decision=ESCALATE, text=self.holding_message, reason="unsupported_action_or_terms")
        if conversation is not None and reply.decision == ESCALATE:
            conversation.finish(reply.decision, reply.reason)
        return reply
