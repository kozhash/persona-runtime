"""Тесты проктивности: ворота, желания, потребности — без сети и телеграма."""
import time
from datetime import datetime

import pytest

from kuni.proactivity import ProactivityEngine


class FakeHuman:
    def is_night_now(self) -> bool:
        return False


class FakeTelegram:
    authorized = True
    human = FakeHuman()
    chats = {100: {"title": "собеседник", "muted": False},
             200: {"title": "тихий", "muted": True}}

    async def request(self, data, timeout=60.0):
        return {"ok": True}


class FakeConfig:
    class current:
        class general:
            character_name = "Ақерке"

        class proactivity:
            enabled = True
            impulse_enabled = True
            impulse_interval_minutes = 45
            thought_stream_enabled = True
            thought_interval_minutes = 15
            needs_enabled = True
            needs_check_minutes = 10
            needs_threshold = 70.0
            miss_rate_per_hour = 60.0   # быстро для теста
            desire_threshold = 75.0
            max_actions_per_day = 6
            action_cooldown_minutes = 0


class FakeApp:
    def __init__(self):
        self.config = FakeConfig()
        from kuni.core.bus import Bus
        self.bus = Bus()
        self.bus.bind_loop(None) if False else None
        self.telegram = FakeTelegram()

    async def proactive_send(self, chat_id, brief, source="test"):
        return {"ok": True, "sent": brief, "chat_id": chat_id}


@pytest.mark.asyncio
async def test_gate_blocks_after_daily_limit():
    app = FakeApp()
    engine = ProactivityEngine(app)
    engine.actions_today = 6
    ok, why = engine._gate()
    assert not ok and "лимит" in why


@pytest.mark.asyncio
async def test_gate_blocks_disabled_and_night():
    app = FakeApp()
    engine = ProactivityEngine(app)
    engine.app.config.current.proactivity.enabled = False
    ok, why = engine._gate()
    assert not ok and "выключено" in why
    engine.app.config.current.proactivity.enabled = True
    engine.app.telegram.human.is_night_now = lambda: True
    ok, why = engine._gate()
    assert not ok and "ночь" in why
    engine.app.telegram.human.is_night_now = lambda: False   # вернуть, класс общий


@pytest.mark.asyncio
async def test_desire_accumulates_and_fires():
    app = FakeApp()
    engine = ProactivityEngine(app)
    engine._add_desire({"want": "write", "who": 100, "text": "хочу написать", "urge": 50})
    assert engine.desires[0]["urge"] == 50
    engine._add_desire({"want": "write", "who": 100, "text": "", "urge": 40})
    assert engine.desires[0]["urge"] == 70.0   # 50 + max(10, 40*0.5) = 50 + 20
    engine._add_desire({"want": "write", "who": 100, "text": "", "urge": 60})
    assert engine.desires[0]["urge"] >= 75.0   # накопилось через три мысли

    # перешло порог — run_thought должен попытаться исполнить
    engine.app.config.current.proactivity.desire_threshold = 75.0
    sent = []

    async def fake_act(kind, chat_id, brief):
        sent.append((kind, chat_id, brief))
        return {"ok": True}

    engine._act = fake_act

    async def fake_llm(*a, **k):
        return {"thought": "хочется написать собеседнику", "new_desires": []}

    engine.app._chat_resilient_json = fake_llm
    engine.app.prompts = type("P", (), {"get": staticmethod(lambda *a, **k: "")})()
    engine.app.diary_store = type("D", (), {"recent": staticmethod(lambda n: [])})()

    class FakeState:
        def describe(self):
            return "спокойствие 20%"

    engine.app.emotions = type("E", (), {"state": FakeState()})()
    await engine.run_thought()
    assert sent and sent[0][1] == 100
    assert not engine.desires   # желание исполнилось и снялось


@pytest.mark.asyncio
async def test_needs_grow_and_trigger():
    app = FakeApp()
    engine = ProactivityEngine(app)
    fired = []

    async def fake_act(kind, chat_id, brief):
        fired.append(chat_id)
        return {"ok": True}

    engine._act = fake_act
    await engine.run_needs()
    # miss_rate 60%/час * (10/60 ч) = 10% за проход — мало
    assert not fired
    # прогоняем много циклов без общения — накопится
    for _ in range(12):
        await engine.run_needs()
    assert fired and fired[0] == 100   # 200 заминчен — не пишем

    # общение сбивает потребность
    engine.bus.emit("telegram.message", chat_id=100, text="привет")
    need = engine.needs[100]
    assert need["miss"] < 70
    assert need["last_seen"] > 0


@pytest.mark.asyncio
async def test_action_blocked_for_unknown_chat():
    app = FakeApp()
    engine = ProactivityEngine(app)
    result = await engine._act("write", 999, "тест")
    assert result is None   # ворота отклонили: незнакомый чат -> blocked событие
