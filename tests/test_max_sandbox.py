"""Песочница MAX: настоящий MaxApi и MaxBot против поддельного MAX по HTTP — проверка всего пути без токена."""
import asyncio
import shutil
from dataclasses import replace

import httpx
import pytest

from app.channels.max_api import MaxApi, MaxError
from app.channels.max_bot import MaxBot
from app.config import ROOT, Settings
from app.services import Services
from tools.max_sandbox import BOT, PEOPLE, TOKEN, FakeMax, create_app


class Sandbox:
    def __init__(self, tmp_path):
        shutil.copytree(ROOT / "tests" / "fixtures" / "kb", tmp_path / "kb")
        settings = replace(Settings.load(), db_path=tmp_path / "t.db", kb_dir=tmp_path / "kb", llm_provider="mock")
        self.services = Services(settings)
        self.fake = FakeMax()
        self.transport = httpx.ASGITransport(app=create_app(self.fake))
        self.marker = None

    async def __aenter__(self):
        await self.services.update_settings({"merge_seconds": "0", "default_mode": "draft"})
        self.api = MaxApi(TOKEN, "http://max.test", transport=self.transport)
        self.ui = httpx.AsyncClient(transport=self.transport, base_url="http://max.test")
        self.bot = MaxBot(self.api, self.services, (await self.api.me())["user_id"])
        return self

    async def __aexit__(self, *_):
        await self.api.close()
        await self.ui.aclose()

    async def say(self, **body):
        """Человек на странице песочницы что-то сделал — бот забирает события так же, как из MAX."""
        assert (await self.ui.post("/ui/send", json=body)).status_code == 200
        data = await self.api.updates(self.marker, timeout=0)
        self.marker = data["marker"]
        for update in data["updates"]:
            await self.bot.handle(update)
            if body.get("who") == "manager" and body.get("text") == "/manager":
                self.services.db.approve_connection(f"max:{PEOPLE['manager']['user_id']}")
                await self.bot.on_command('/start', PEOPLE['manager'], PEOPLE['manager']['chat_id'],
                                           self.services.db.get_connection(f"max:{PEOPLE['manager']['user_id']}"))
        return (await self.ui.get("/ui/state")).json()


def test_whole_path_through_fake_max(tmp_path):
    async def scenario():
        async with Sandbox(tmp_path) as s:
            assert BOT["user_id"] == s.bot.me_id
            state = await s.say(who="manager", text="/manager")
            assert "Команды менеджера" in state["chats"]["manager"][-1]["text"]
            state = await s.say(who="anna", start=True)
            assert state["chats"]["anna"][-1]["text"] == s.services.assistant.greeting_message

            state = await s.say(who="anna", text="Сколько стоит экран в атриуме?")
            card = state["chats"]["manager"][-1]
            assert "Черновик #" in card["text"] and [b["text"] for b in card["buttons"][0]] == ["Отправить", "Пропустить"]
            state = await s.say(who="manager", mid=card["mid"], payload=card["buttons"][0][0]["payload"])
            assert "30 000" in state["chats"]["anna"][-1]["text"]
            edited = next(m for m in state["chats"]["manager"] if m["mid"] == card["mid"])
            assert "Отправлено клиенту." in edited["text"] and edited["buttons"] == [], "кнопки убраны"
            assert state["notes"] == [{"who": "manager", "text": "Отправлено клиенту.", "at": state["notes"][0]["at"]}]

            s.services.privacy.set_allowed(f"max:{PEOPLE['manager']['user_id']}", PEOPLE["ildar"]["chat_id"], True)
            await s.say(who="manager", text="/mode observe")
            state = await s.say(who="ildar", media="image")
            forwarded, relay = state["chats"]["manager"][-2:]
            assert forwarded["link"] == {"type": "forward", "text": "[фото]", "from": "Ильдар"}
            assert "прислал(а) фото" in relay["text"] and not any(m["bot"] for m in state["chats"]["ildar"]), \
                "бот клиенту не пишет"
            state = await s.say(who="manager", text="Макет подходит, запускаем с понедельника.", reply_to=relay["mid"])
            assert state["chats"]["ildar"][-1]["text"] == "Макет подходит, запускаем с понедельника."
            assert s.services.db.observed_chat(f"max:{PEOPLE['manager']['user_id']}", PEOPLE["ildar"]["chat_id"])

    asyncio.run(scenario())


def test_fake_max_rejects_bad_token_and_long_text(tmp_path):
    async def scenario():
        fake = FakeMax()
        transport = httpx.ASGITransport(app=create_app(fake))
        bad = MaxApi("wrong-token", "http://max.test", transport=transport)
        with pytest.raises(MaxError) as err:
            await bad.me()
        assert err.value.status == 401 and err.value.code == "verify.token"
        api = MaxApi(TOKEN, "http://max.test", transport=transport)
        sent = await api.send("x" * 5000, chat_id=PEOPLE["anna"]["chat_id"])
        assert len(sent["message"]["body"]["text"]) < 4000, "MaxApi сам режет текст до лимита MAX"
        with pytest.raises(MaxError):
            await api.send("кому?", chat_id=1)
        assert (await api.updates(None, timeout=0)) == {"updates": [], "marker": 0}
        await bad.close()
        await api.close()

    asyncio.run(scenario())


def test_webhook_delivery_with_secret_and_no_polling():
    """Поддельный MAX при подписке шлёт события на адрес с секретом, опрос в это время пуст."""
    async def scenario():
        fake = FakeMax()
        api = MaxApi(TOKEN, "http://max.test", transport=httpx.ASGITransport(app=create_app(fake)))
        assert await api.subscriptions() == []
        got = []

        async def receiver(request):
            got.append((request.headers["X-Max-Bot-Api-Secret"], request.read()))
            return httpx.Response(200)

        real = httpx.AsyncClient
        httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(receiver), **kw)
        try:
            await api.subscribe("https://bot.test/max/webhook", "s3cret", ("message_created",))
            assert (await api.subscriptions())[0]["url"] == "https://bot.test/max/webhook"
            await fake.user_writes("anna", "вопрос")
        finally:
            httpx.AsyncClient = real
        assert got[0][0] == "s3cret" and "вопрос".encode() in got[0][1]
        assert (await api.updates(None, timeout=0))["updates"] == []
        await api.unsubscribe("https://bot.test/max/webhook")
        assert await api.subscriptions() == []
        await api.close()

    asyncio.run(scenario())



def test_long_admin_reply_is_delivered_in_full_over_http(tmp_path):
    async def scenario():
        async with Sandbox(tmp_path) as s:
            await s.say(who='manager', text='/manager')
            await s.say(who='anna', text='Дадите скидку?')
            s.services.client_senders['max'] = lambda _conn, chat, text: s.bot.send(chat, text)
            dialog_id = s.services.db.recent_dialogs(1)[0]['id']
            text = 'Условия: ' + 'пункт договора; ' * 250 + 'ИТОГ: скидка 10%'
            assert await s.services.decide_draft(dialog_id, 'edit', text) == 'sent'
            state = (await s.ui.get('/ui/state')).json()
            sent = [m for m in state['chats']['anna'] if m['bot']]
            assert ''.join(m['text'] for m in sent) == text
            assert all(len(m['text']) < 4000 for m in sent)
    asyncio.run(scenario())
